"""微信 ↔ 电脑端 Claude Code 实时多轮遥控中继：
  - 服务器侧（fuxi-bridge）保存活跃会话与待发任务队列，提供长轮询接口给本机守护进程（Mac / Win）秒级领取；
  - 主人通过私聊或群里 @浮汐 发起任务（如「把最新的 PR 看下，把看法私发我」），结果统一私发给主人；
  - 会话活跃期间，主人在私聊里的后续回复自动透传给同一 claude session（--resume）继续执行；
  - 支持运行中排队、随时打断（停一下 / /stop）、手机发图透传、2 小时空闲自动退出、退出时精准关闭 Ghostty 窗。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
from typing import TYPE_CHECKING

from .sending import SendJob, _h

if TYPE_CHECKING:
    from .app import Bridge

log = logging.getLogger("fuxi.remote")

IDLE_TIMEOUT = 2 * 3600        # 2 小时无互动自动关闭，防忘关影响正常聊天
TURN_WARN_SEC = 15 * 60        # 单轮跑超 15 分钟提醒一次
NODE_ONLINE_SEC = 45           # 45 秒内有长轮询/心跳视为在线
CHUNK_LIMIT = 1200             # 微信单条舒适分段上限

EXIT_RE = re.compile(
    r"^(?:退出\s*agent|关闭\s*agent|结束\s*agent|退出远程|关闭终端|关掉终端|结束会话|退出会话|/exit|/agent\s+(?:exit|close|quit))[。！!~～]*$",
    re.I,
)
STOP_RE = re.compile(
    r"^(?:停一下|停下来|暂停一下|打断|中止|停止这轮|别跑了|/stop|/agent\s+stop)[。！!~～]*$",
    re.I,
)


def split_chunks(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """按段落/换行把长回复拆成适合微信阅读的若干段（最多 4 段，超出则截断并提示看本地日志）。"""
    s = (text or "").strip()
    if len(s) <= limit:
        return [s] if s else []
    chunks: list[str] = []
    cur = ""
    for para in s.split("\n\n"):
        cand = f"{cur}\n\n{para}" if cur else para
        if len(cand) <= limit:
            cur = cand
            continue
        if cur:
            chunks.append(cur)
            cur = ""
        while len(para) > limit:
            cut = para.rfind("\n", 0, limit)
            if cut < limit // 2:
                cut = limit
            chunks.append(para[:cut].rstrip())
            para = para[cut:].lstrip()
        cur = para
    if cur:
        chunks.append(cur)
    if len(chunks) > 4:
        chunks = chunks[:3] + [chunks[3][:limit - 40].rstrip() + "\n…（输出过长已截断，完整内容见电脑端会话日志）"]
    return chunks


class RemoteAgent:
    def __init__(self, bridge: "Bridge", root: str):
        self.b = bridge
        self.root = root
        os.makedirs(root, mode=0o700, exist_ok=True)
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self.nodes: dict[str, dict] = {}         # target("mac"|"win") -> {last_seen, host}
        self.active: dict | None = None          # 当前唯一活跃远程会话
        self.commands: dict[str, list[dict]] = {"mac": [], "win": []}
        self._next_id = self._load_next_id()
        self._seq = 0

    def _meta_path(self) -> str:
        return os.path.join(self.root, "meta.json")

    def _load_next_id(self) -> int:
        try:
            with open(self._meta_path(), encoding="utf-8") as f:
                return max(1, int(json.load(f).get("next_id", 1)))
        except (OSError, ValueError, TypeError):
            return 1

    def _save_next_id(self) -> None:
        try:
            tmp = self._meta_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"next_id": self._next_id}, f)
            os.replace(tmp, self._meta_path())
        except OSError:
            pass

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def is_node_online(self, target: str, now: float | None = None) -> bool:
        now = now or time.time()
        info = self.nodes.get(target)
        return bool(info and (now - float(info.get("last_seen") or 0)) <= NODE_ONLINE_SEC)

    def _offline_note(self, target: str, now: float | None = None) -> str:
        now = now or time.time()
        if self.is_node_online(target, now):
            return ""
        info = self.nodes.get(target)
        if not info:
            return f"\n⚠️ {target} 端守护进程尚未连线（请确认已运行 scripts/fuxi-agent），连上后会自动领走，或回「退出 agent」取消。"
        mins = max(1, int((now - float(info["last_seen"])) // 60))
        return f"\n⚠️ {target} 当前离线/休眠（上次在线约 {mins} 分钟前），任务已暂存，醒来会自动继续，或回「退出 agent」取消。"

    def _send_chat(self, to: str, texts: list[str]) -> None:
        ch = getattr(self.b, "_control_hashes", None)

        def go():
            for t in texts:
                if not t:
                    continue
                if ch is not None:
                    ch.append(_h(t))
                job = self.b.send(SendJob(chat=to, text=t, source="control"))
                if not job.ok:
                    log.warning("远程 Agent 回复发送失败（%s）：%s", to, job.error)
        threading.Thread(target=go, name="remote-send", daemon=True).start()

    def _send_owner(self, texts: list[str]) -> None:
        to = self.b.owner_chat() if hasattr(self.b, "owner_chat") else "filehelper"
        self._send_chat(to, texts)

    # ---- 主人侧：启动 / 续接 / 打断 / 退出 ----
    def start_session(self, target: str, repo: str, prompt: str, readonly: bool = False,
                      images: list[dict] | None = None, reply_chat: str = "") -> tuple[dict, str]:
        target = target.lower() if target in ("mac", "win") else "mac"
        repo = (repo or self.b.cfg.get("knowledge_repo") or "TixXin/Fuyu").strip()
        now = time.time()
        with self._cond:
            # 若已有旧活跃会话，先发关闭指令
            if self.active and self.active.get("status") != "closed":
                old = self.active
                old["status"] = "closed"
                self.commands.setdefault(old["target"], []).append({"op": "close", "sid": old["id"]})
            sid = f"R{self._next_id}"
            self._next_id += 1
            self._save_next_id()
            seq = self._next_seq()
            sess = {
                "id": sid,
                "target": target,
                "repo": repo,
                "readonly": bool(readonly),
                "reply_chat": reply_chat or "",
                "status": "active",
                "claude_session_id": "",
                "turns": 0,
                "in_flight": True,
                "running_since": now,
                "warned_long": False,
                "created_at": now,
                "last_at": now,
                "queue": [],
            }
            self.active = sess
            self.commands.setdefault(target, []).append({
                "op": "run",
                "sid": sid,
                "seq": seq,
                "repo": repo,
                "readonly": bool(readonly),
                "prompt": prompt,
                "claude_session_id": "",
                "images": images or [],
            })
            self._cond.notify_all()

        mode_label = "只读审查模式" if readonly else "读写协作模式"
        offline = self._offline_note(target, now)
        dm = (f"已启动 {target.upper()} 远程会话 #{sid}（仓库 {repo}，{mode_label}），正在执行…\n"
              f"后续在私聊里直接回复都会转发给电脑上的 Claude 接着做；发「停一下」打断当前轮，发「退出 agent」或「关闭终端」结束。{offline}")
        return dict(sess), dm

    def has_active(self) -> bool:
        with self._lock:
            return bool(self.active and self.active.get("status") == "active")

    def handle_private_message(self, text: str, images: list[dict] | None = None) -> str | None:
        """私聊直通入口：仅在存在活跃会话时接管主人私聊。"""
        clean = (text or "").strip()
        if not self.has_active():
            return None
        first = clean.splitlines()[0].strip() if clean else ""
        if first and EXIT_RE.match(first):
            return self.close_active()
        if first and STOP_RE.match(first):
            return self.interrupt_active()
        return self.send_followup(clean or "（请查看我刚发的截图）", images=images)

    def send_followup(self, prompt: str, images: list[dict] | None = None) -> str:
        now = time.time()
        with self._cond:
            s = self.active
            if not s or s.get("status") != "active":
                return "当前没有进行中的远程会话。"
            s["last_at"] = now
            target = s["target"]
            sid = s["id"]
            item = {"prompt": prompt, "images": images or []}
            if s.get("in_flight"):
                s["queue"].append(item)
                elapsed = max(1, int(now - float(s.get("running_since") or now)))
                return f"上一轮还在跑（已运行 {elapsed}s），这句已排进 #{sid} 队列（待执行 {len(s['queue'])} 条），跑完自动接着做；要立刻打断回「停一下」。"
            seq = self._next_seq()
            s["in_flight"] = True
            s["running_since"] = now
            s["warned_long"] = False
            self.commands.setdefault(target, []).append({
                "op": "run",
                "sid": sid,
                "seq": seq,
                "repo": s["repo"],
                "readonly": s["readonly"],
                "prompt": prompt,
                "claude_session_id": s.get("claude_session_id") or "",
                "images": images or [],
            })
            self._cond.notify_all()
        offline = self._offline_note(target, now)
        img_tip = f"（附 {len(images)} 张图）" if images else ""
        return f"已发给 {target.upper()}（#{sid}）继续执行{img_tip}…{offline}"

    def interrupt_active(self) -> str:
        with self._cond:
            s = self.active
            if not s or s.get("status") != "active":
                return "当前没有运行中的远程会话。"
            sid, target = s["id"], s["target"]
            s["in_flight"] = False
            s["queue"].clear()
            s["last_at"] = time.time()
            self.commands.setdefault(target, []).append({"op": "cancel", "sid": sid})
            self._cond.notify_all()
        return f"已向 {target.upper()}（#{sid}）发送打断信号并清空排队任务。你可以发新指令继续，或回「退出 agent」结束。"

    def close_active(self, reason: str = "") -> str:
        with self._cond:
            s = self.active
            if not s or s.get("status") != "active":
                return "当前没有开启中的远程会话。"
            sid, target = s["id"], s["target"]
            s["status"] = "closed"
            s["in_flight"] = False
            s["queue"].clear()
            self.commands.setdefault(target, []).append({"op": "close", "sid": sid})
            self._cond.notify_all()
        why = f"（{reason}）" if reason else ""
        return f"已退出 {target.upper()} 远程会话 #{sid}{why}并通知关闭终端窗口，私聊已恢复和浮汐正常聊天。"

    # ---- 电脑端 API：心跳 / 长轮询 / 回传结果 ----
    def heartbeat(self, target: str, host: str = "") -> dict:
        target = target.lower() if target in ("mac", "win") else target
        now = time.time()
        with self._lock:
            self.nodes[target] = {"last_seen": now, "host": host or target}
        return {"ok": True, "target": target, "ts": now}

    def poll(self, target: str, wait: float = 25.0, host: str = "") -> list[dict]:
        target = target.lower()
        wait = max(0.0, min(float(wait), 30.0))
        deadline = time.monotonic() + wait
        with self._cond:
            self.nodes[target] = {"last_seen": time.time(), "host": host or target}
            while True:
                q = self.commands.setdefault(target, [])
                if q:
                    out = list(q)
                    q.clear()
                    self.nodes[target]["last_seen"] = time.time()
                    return out
                left = deadline - time.monotonic()
                if left <= 0:
                    self.nodes[target]["last_seen"] = time.time()
                    return []
                self._cond.wait(timeout=min(left, 5.0))

    def deliver_reply(self, sid: str, seq: int, ok: bool, text: str, claude_session_id: str = "",
                      duration_s: float = 0.0, cost_usd: float = 0.0, error: str = "",
                      log_path: str = "") -> dict:
        now = time.time()
        next_cmd = None
        target = "mac"
        reply_chat = ""
        with self._cond:
            s = self.active
            if not s or s.get("id") != sid:
                return {"ok": True, "ignored": True}
            target = s["target"]
            reply_chat = s.get("reply_chat") or ""
            if reply_chat:
                s["reply_chat"] = ""   # 只把本轮明确要求的结论发到群里，后续私聊多轮不刷群
            if claude_session_id:
                s["claude_session_id"] = claude_session_id
            s["turns"] = int(s.get("turns") or 0) + 1
            s["last_at"] = now
            s["in_flight"] = False
            s["warned_long"] = False
            if s.get("status") == "active" and s["queue"]:
                nxt = s["queue"].pop(0)
                s["in_flight"] = True
                s["running_since"] = now
                next_cmd = {
                    "op": "run",
                    "sid": sid,
                    "seq": self._next_seq(),
                    "repo": s["repo"],
                    "readonly": s["readonly"],
                    "prompt": nxt["prompt"],
                    "claude_session_id": s.get("claude_session_id") or "",
                    "images": nxt.get("images") or [],
                }
                self.commands.setdefault(target, []).append(next_cmd)
                self._cond.notify_all()

        body = (text or "").strip() if ok else f"执行出错：{error or text or '未知错误'}"
        meta_bits = [f"{target.upper()} #{sid}"]
        if duration_s > 0:
            meta_bits.append(f"{int(round(duration_s))}s")
        if cost_usd > 0:
            meta_bits.append(f"${cost_usd:.3f}")
        meta_bits.append("回「退出 agent」结束")
        footer = f"\n\n[{' · '.join(meta_bits)}]"
        if not ok and log_path:
            body += f"\n本地日志：{log_path}"
        chunks = split_chunks(body)
        if reply_chat and ok and chunks:
            # 发给群的结论不带主人控制尾注
            self._send_chat(reply_chat, chunks)
            self._send_owner([f"已把 {target.upper()} #{sid} 的核实结果发到「{self.b.name_of(reply_chat)}」。{footer.strip()}"])
            return {"ok": True, "queued_next": bool(next_cmd), "delivered_group": True}
        if chunks:
            chunks[-1] = chunks[-1] + footer
        else:
            chunks = [f"（本轮无文本输出）{footer}"]
        self._send_owner(chunks)
        return {"ok": True, "queued_next": bool(next_cmd)}

    # ---- 定时巡检（空闲超时 & 单轮超时提醒） ----
    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        notify: list[str] = []
        with self._cond:
            s = self.active
            if not s or s.get("status") != "active":
                return
            if s.get("in_flight") and not s.get("warned_long") and now - float(s.get("running_since") or now) >= TURN_WARN_SEC:
                s["warned_long"] = True
                mins = int((now - float(s["running_since"])) // 60)
                notify.append(f"【远程 Agent 提示】{s['target'].upper()} 会话 #{s['id']} 当前这轮已运行 {mins} 分钟，如需打断请回「停一下」或「退出 agent」。")
            elif not s.get("in_flight") and now - float(s.get("last_at") or now) >= IDLE_TIMEOUT:
                sid, target = s["id"], s["target"]
                s["status"] = "closed"
                self.commands.setdefault(target, []).append({"op": "close", "sid": sid})
                self._cond.notify_all()
                notify.append(f"{target.upper()} 远程会话 #{sid} 已 2 小时无互动，已自动关闭并通知关窗，私聊恢复和浮汐正常聊天。")
        if notify:
            self._send_owner(notify)

    # ---- 状态摘要 ----
    def nodes_snapshot(self, now: float | None = None) -> dict[str, dict]:
        now = now or time.time()
        with self._lock:
            out = {}
            for k, v in self.nodes.items():
                age = int(max(0, now - float(v.get("last_seen") or 0)))
                out[k] = {"online": age <= NODE_ONLINE_SEC, "age_s": age, "host": v.get("host") or k}
            return out

    def brief(self) -> str:
        nodes = self.nodes_snapshot()
        parts = [f"{k}:在线" if v["online"] else f"{k}:离线({v['age_s']}s前)" for k, v in sorted(nodes.items())]
        nstr = "、".join(parts) or "暂无连线节点"
        with self._lock:
            s = self.active
            if s and s.get("status") == "active":
                st = "运行中" if s.get("in_flight") else "等待你回复"
                return f"节点 {nstr}；活跃会话 #{s['id']}（{s['target']}/{s['repo']}，第 {s['turns']} 轮，{st}）"
        return f"节点 {nstr}；无活跃会话"
