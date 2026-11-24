"""对外 API（api.py）和管理台（admin.py）共用的处理逻辑。两边只管认证和路由，干活都在这里。"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import Bridge

log = logging.getLogger("fuxi.handlers")


class HandlerError(Exception):
    def __init__(self, msg: str, code: int = 400, **extra):
        super().__init__(msg)
        self.code = code
        self.extra = extra


def version() -> str:
    return os.environ.get("FUXI_BUILD", "dev")


def health(b: "Bridge") -> dict:
    probs = b.problems()
    return {"healthy": not probs, "problems": probs, "wechat": b.last_send_ok, "halted": b.halted,
            "paused": b.paused, "open_chat": b.sender.open_stats(), "latency": b.latency.summary(),
            "version": version()}


def settings_items(b: "Bridge") -> list[dict]:
    from . import settings as st
    from .kernel import KernelError
    items = []
    for it in st.ITEMS:
        try:
            v = b.get_setting(it.key)
        except KernelError:
            v = None
        row = {"key": it.key, "label": it.label, "type": it.type, "value": v,
               "default": it.default, "desc": it.desc, "choices": it.choices,
               "kernel": bool(it.kernel), "min": it.min, "max": it.max}
        if it.type == "list" and isinstance(v, list):   # wxid / chat_id 旁边显示名字
            row["labels"] = {x: b.name_of(x) for x in v if _is_id(x)}
        items.append(row)
    return items


def _is_id(x: str) -> bool:
    return x.startswith(("wxid_", "gh_")) or x.endswith(("@chatroom", "@openim")) or x == "filehelper"


def set_settings(b: "Bridge", body: dict, who: str = "API") -> list[dict]:
    from . import settings as st
    from .kernel import KernelError
    for k in body:
        if k not in st.BY_KEY:
            raise HandlerError(f"没有设置项 {k}")
    for k, v in body.items():
        try:
            b.set_setting(k, v)
        except (ValueError, KernelError) as e:
            raise HandlerError(str(e), key=k) from None
    log.warning("设置经%s修改：%s", who, "、".join(body))
    if hasattr(b, "events"):
        b.events.publish("settings", {"keys": list(body)})
    return settings_items(b)


def resume(b: "Bridge", who: str = "API") -> str:
    was, b.halted = b.halted, ""
    log.warning("人工解除熔断（%s；之前：%s）", who, was or "未熔断")
    return was


def overview(b: "Bridge") -> dict:
    """管理台概览：总状态、三个服务、今天的数字、需要主人看一眼的事。"""
    probs = b.problems()
    now = time.time()
    if b.halted:
        state, text = "bad", "已熔断，暂停一切发送"
    elif b.paused:
        state, text = "warn", "已暂停回复"
    elif probs:
        state, text = "warn", probs[0]
    elif b.in_quiet_hours():
        state, text = "idle", "静默时段中，暂不回复"
    else:
        state, text = "ok", "正常回复中"
    window = b.sender.window_state()
    wechat = {"state": "ok" if window == "ok" and len(b.keyring.keys) > 0 else "bad",
              "text": {"ok": "已登录", "login": "待登录（需在手机上确认）", "missing": "找不到微信窗口",
                       "tray": "主窗口被隐藏到托盘"}.get(window, window),
              "detail": f"{len(b.keyring.keys)} 个库已解密"}
    kernel_ok = now - b.kernel_ok_at <= 90
    kernel = {"state": "ok" if kernel_ok else "bad", "text": "在线" if kernel_ok else "不可达",
              "detail": f"{int(now - b.kernel_ok_at)} 秒前心跳"}
    poll_ok = now - b.last_poll_ok_at <= 120
    bridge = {"state": "bad" if b.halted or not poll_ok else "ok",
              "text": "已熔断" if b.halted else ("轮询停滞" if not poll_ok else "正常"),
              "detail": b.halted or f"白名单 {len(b.allowlist.items)} 个会话"}
    lat = b.latency.summary()
    todo = []
    for it in b.announcer.pending():
        todo.append({"kind": "announce", "code": it["code"], "title": f"发版公告等确认 #{it['code']}",
                     "desc": f"发到「{it['to']}」", "created": it["created"]})
    for ev in b.events.recent(limit=5, kind="handoff"):
        if now - ev["ts"] < 24 * 3600:
            todo.append({"kind": "handoff", "title": f"群里有人问「{ev.get('question', '')[:40]}」",
                         "desc": f"资料里没找到 · {ev.get('chat_name', '')} · {ev.get('sender', '')}",
                         "created": ev["ts"]})
    return {
        "state": state, "text": text, "problems": probs,
        "paused": b.paused, "halted": b.halted, "quiet": b.quiet, "in_quiet": b.in_quiet_hours(),
        "allow_count": len(b.allowlist.items), "version": version(), "uptime": int(now - b.started),
        "services": {"wechat": wechat, "kernel": kernel, "bridge": bridge},
        "today": b.events.counts(),
        "latency": {"n": lat.get("n", 0), "first": lat.get("first"), "settle": lat.get("settle"),
                    "kernel": lat.get("kernel"), "send": round(sum(lat.get(k, 0) or 0 for k in
                                                                   ("queue", "open", "paste", "verify")), 2)
                    if lat.get("n") else None},
        "todo": todo,
    }


def pause(b: "Bridge", on: bool, who: str = "管理台") -> dict:
    """暂停 / 恢复。恢复时和遥控 /恢复 一样：顺带解除熔断、忘掉当前会话（下次重新进入核对）。"""
    was = ""
    if on:
        b.set_paused(True)
    else:
        was = resume(b, who)
        b.set_paused(False)
        b.sender.forget_chat()
    log.warning("%s：%s", who, "暂停回复" if on else "恢复回复")
    b.events.publish("status", {"paused": b.paused, "halted": b.halted})
    return {"paused": b.paused, "halted": b.halted, "was": was}


# ---- 资料库（产品助手的浮屿文档） ----
def knowledge_info(b: "Bridge") -> dict:
    k = b.knowledge
    m = k.meta
    sources: dict[str, int] = {}
    for c in k.index.chunks:
        kind = ("版本说明" if c.source.startswith("docs/releases/") else
                "功能说明" if c.source.startswith("docs/features/") else "其他")
        sources[kind] = sources.get(kind, 0) + 1
    return {"repo": m.get("repo") or b.cfg.get("knowledge_repo"), "synced_at": m.get("synced_at"),
            "files": m.get("files", 0), "fetched": m.get("fetched", 0), "chunks": len(k.index.chunks),
            "sources": sources, "error": m.get("error", ""), "failed_at": m.get("failed_at"),
            "syncing": getattr(k, "syncing", False), "enabled": bool(b.cfg.get("assistant_groups")),
            "groups": b.cfg.get("assistant_groups") or []}


def knowledge_sync(b: "Bridge") -> None:
    """后台同步一次，完成后推送 knowledge 事件；正在同步时抛错。"""
    import threading
    k = b.knowledge
    if getattr(k, "syncing", False):
        raise HandlerError("正在同步，稍等", 409)
    k.syncing = True

    def go():
        result = ""
        try:
            result = k.sync(b.cfg.get("knowledge_repo"))
        except Exception as e:
            result = f"同步失败：{e}"
        finally:
            k.syncing = False
            b.events.publish("knowledge", {"result": result})
    threading.Thread(target=go, name="knowledge-sync", daemon=True).start()


def knowledge_search(b: "Bridge", q: str, judge: bool = False) -> dict:
    """试搜：粗筛候选的分数、覆盖率，最终会附给内核的段落；judge=True 再让模型筛一遍（和群里一样）。"""
    from . import knowledge as kn
    k = b.knowledge
    q = q.strip()[:200]
    if not q:
        raise HandlerError("要搜什么？")
    cands = k.candidates(q, 6)
    picked = k.search(q)
    judged = None
    if judge and picked:
        judged = [c.title for c in b._judge(q, picked)]
    cq = kn.clean(q)
    return {"query": q, "question": kn.is_question(q), "floor": k.floor, "cover": k.cover,
            "picked": [c.title for c in picked], "judged": judged,
            "candidates": [{"score": round(s, 1), "coverage": round(k.index.coverage(cq, c), 2),
                            "source": c.source, "title": c.title, "text": c.text[:400]} for s, c in cands]}
