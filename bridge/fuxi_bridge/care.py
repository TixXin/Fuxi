"""主动搭话：没人说话一阵子后，bot 自己找主人说一句「在干啥呢」。

该不该发、发给谁全由内核决定（时间窗 / 间隔 / 每日上限 / 只找主人，见内核 services/care.js），
桥接只负责：定时问一次 → 生成 → 发 → 记账。

  GET  /api/care/due?last=<ms>&sentToday=<n>&active=<wxid,...>   该发吗、发给谁
  GET  /api/care?chat=<wxid>                                       生成那句话（带最近聊天上下文）
  POST /api/care/sent {chat, text, group, id}                      记进会话历史，下次不重复

桥接这边再加几道闸：暂停 / 熔断 / 静默时段不发；目标必须在桥接白名单里；最近
quietAfterMinutes 分钟内有过聊天的会话算「正在聊」，不打扰；生成期间对方来了消息也放弃。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import TYPE_CHECKING

from .kernel import KernelError

if TYPE_CHECKING:
    from .app import Bridge

log = logging.getLogger("fuxi.care")


REASONS = {
    "disabled": "内核关闭了主动搭话",
    "out_of_window": "不在允许的时间段",
    "daily_cap": "今天已达上限",
    "too_soon": "离上次太近",
    "whitelist_filtered": "没有可找的人（主人没设置？见 FUXI_OWNER_WXIDS）",
    "whitelist_empty": "没有可找的人（白名单为空）",
    "all_busy": "对方正在聊天，不打扰",
}


class Care:
    def __init__(self, bridge: "Bridge", state_path: str, interval: float = 60):
        self.b = bridge
        self.path = state_path
        self.interval = interval
        self.last_ms = 0       # 上次主动搭话（毫秒）
        self.day = ""
        self.sent_today = 0
        self.last_reason = ""  # 最近一次「不发」的原因，/状态 里看
        self._lock = threading.Lock()
        self._load()

    # ---- 状态 ----
    def _load(self) -> None:
        try:
            with open(self.path) as f:
                d = json.load(f)
            self.last_ms, self.day, self.sent_today = int(d.get("last", 0)), d.get("day", ""), int(d.get("sent", 0))
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"last": self.last_ms, "day": self.day, "sent": self.sent_today}, f)
        os.replace(tmp, self.path)

    def _today(self) -> int:
        today = time.strftime("%Y-%m-%d")
        if self.day != today:
            self.day, self.sent_today = today, 0
        return self.sent_today

    def brief(self) -> str:
        last = time.strftime("%H:%M", time.localtime(self.last_ms / 1000)) if self.last_ms else "从未"
        why = f"；当前不发：{self.last_reason}" if self.last_reason else ""
        return f"今天 {self._today()} 次，上次 {last}{why}"

    # ---- 主循环 ----
    def loop(self) -> None:
        while True:
            time.sleep(self.interval)
            try:
                self.tick()
            except Exception:
                log.exception("主动搭话出错")

    def active_chats(self, quiet_min: float) -> list[str]:
        """最近 quiet_min 分钟内有动静（任一方发言）或正在处理的会话。"""
        since = time.time() - quiet_min * 60
        out = {s["username"] for s in self.b.reader.sessions(50) if (s.get("last_timestamp") or 0) >= since}
        out |= set(self.b.pending)
        return sorted(out)

    def quiet_minutes(self) -> float:
        try:
            v = self.b.kernel.config().get("care", {}).get("quietAfterMinutes")
            return float(v) if v is not None else 20
        except KernelError:
            return 20

    def tick(self) -> str:
        """检查一次；返回结果说明（测试和 /状态 用）。"""
        with self._lock:
            r = self._tick()
            self.last_reason = "" if r == "sent" else r
            return r

    def _tick(self) -> str:
        b = self.b
        if b.paused:
            return "已暂停"
        if b.halted:
            return "已熔断"
        if b.in_quiet_hours():
            return "静默时段"
        quiet = self.quiet_minutes()
        active = self.active_chats(quiet)
        due = b.kernel.care_due(self.last_ms, self._today(), active)
        if not due.get("due"):
            why = REASONS.get(due.get("reason") or "", due.get("reason") or "不到时候")
            if due.get("reason") == "too_soon" and due.get("waitMinutes"):
                why += f"（还要 {due['waitMinutes']} 分钟）"
            return why
        target = due.get("target") or {}
        chat = target.get("wxid") or ""
        if not chat:
            return "内核没给目标"
        if not b.allowlist.allows(chat):
            return f"{b.name_of(chat)} 不在白名单"
        allowed, reason = b.kernel.send_permission(chat, b.name_of(chat))
        if not allowed:
            return f"内核不允许：{reason}"
        text = (b.kernel.care_text(chat).get("text") or "").strip()
        if not text:
            return "没生成出内容"
        if chat in self.active_chats(quiet):
            return "生成期间对方来了消息"
        if b.paused or b.halted:
            return "已暂停"
        log.info("主动搭话 → %s：%s", b.name_of(chat), text)
        from .app import SendJob
        job = b.send(SendJob(chat=chat, text=text, source="care"))
        if not job.ok:
            return f"发送失败：{job.error}"
        self.last_ms = int(time.time() * 1000)
        self._today()
        self.sent_today += 1
        self._save()
        try:
            b.kernel.care_sent(chat, text, chat.endswith("@chatroom"), uuid.uuid4().hex[:16])
        except KernelError as e:
            log.warning("搭话已发出，但内核记录失败：%s", e)
        return "sent"
