"""浮汐桥接主程序：全会话轮询 → 内核 webhook → 发送；提醒拉取；心跳；对外发送 API。

  python -m fuxi_bridge.app

环境变量（见 deploy/.env.example）：
  FUXI_WX_CONFIG / FUXI_DB_ROOT   微信数据（只读）
  FUXI_STATE_DIR / FUXI_WORK_DIR  持久状态 / tmpfs
  FUXI_KERNEL_URL                 默认 http://127.0.0.1:8787
  FUXI_KERNEL_TOKEN               与内核 gateway.wechat.inboundToken 一致
  FUXI_API_TOKEN / FUXI_API_PORT  对外发送 API（不设 token 则不开 API）
  FUXI_SELF_NAMES                 额外的「叫她」名字，逗号分隔（群里 @ 识别用）
  FUXI_SETTLE_MS                  同一会话连发合并窗口，默认 1500
  FUXI_STALE_SEC                  早于此秒数的消息不回复，默认 120
  FUXI_REPLY_SELF                 1 = 文件传输助手里自己的消息也交给内核（自测）
  FUXI_ALLOW                      只响应这些会话（chat_id 或显示名，逗号分隔）；空 = 全部
"""

from __future__ import annotations

import collections
import concurrent.futures as cf
import hashlib
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field

from . import messages, store
from .kernel import Kernel, KernelError
from .messages import Message
from .sender import Sender, SendError

log = logging.getLogger("fuxi")


def _h(text: str) -> str:
    return hashlib.sha1(text.strip().encode()).hexdigest()


@dataclass
class SendJob:
    chat: str               # username
    text: str | None = None
    image: str | None = None
    verify: bool = True
    done: threading.Event = field(default_factory=threading.Event)
    ok: bool = False
    verified: bool | None = None
    error: str = ""


class Bridge:
    def __init__(self) -> None:
        env = os.environ
        self.db_root = env.get("FUXI_DB_ROOT") or store.find_db_root(env.get("FUXI_WX_CONFIG", "/wxconfig"))
        state = env.get("FUXI_STATE_DIR", "/state")
        work = env.get("FUXI_WORK_DIR", "/work")
        self.work = work
        self.keyring = store.KeyRing(self.db_root, os.path.join(work, "keys.json"))
        self.cache = store.DbCache(self.db_root, os.path.join(work, "db"), self.keyring)
        self.self_wxid = messages.self_wxid_from_root(self.db_root)
        self.reader = messages.MessageReader(self.cache, os.path.join(state, "watermarks.json"), self.self_wxid)
        self.sender = Sender(display=env.get("DISPLAY", ":1"))
        self.kernel = Kernel(env.get("FUXI_KERNEL_URL", "http://127.0.0.1:8787"), env.get("FUXI_KERNEL_TOKEN", ""))
        self.settle = int(env.get("FUXI_SETTLE_MS", "1500")) / 1000
        self.stale = int(env.get("FUXI_STALE_SEC", "120"))
        self.reply_self = env.get("FUXI_REPLY_SELF") == "1"
        self.extra_names = [n.strip() for n in env.get("FUXI_SELF_NAMES", "").split(",") if n.strip()]
        self.interval = float(env.get("FUXI_POLL_INTERVAL", "0.5"))
        # 只响应这些会话（chat_id 或显示名，逗号分隔）；空 = 全部
        self.allow = {a.strip() for a in env.get("FUXI_ALLOW", "").split(",") if a.strip()}

        self.sendq: "queue.Queue[SendJob]" = queue.Queue()
        self.sent_hashes: collections.deque[str] = collections.deque(maxlen=500)
        self.pending: dict[str, list[Message]] = {}
        self.pending_at: dict[str, float] = {}
        self.chat_locks: dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
        self.pool = cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="chat")
        self.session_seen: dict[str, int] = {}
        self.names_cache: dict[str, str] = {}
        self.started = time.time()
        self.last_send_ok = True
        # 熔断：发送后核对失败（疑似发错会话）就停止一切发送，直到人工 POST /resume 或重启
        self.halted: str = ""

    # ---------- 名字 ----------
    def name_of(self, username: str) -> str:
        if username not in self.names_cache:
            self.names_cache[username] = self.reader.display_name(username)
        return self.names_cache[username]

    def self_names(self) -> list[str]:
        return list(dict.fromkeys(self.extra_names + self.reader.self_names()))

    # ---------- 发送（单线程串行） ----------
    def send(self, job: SendJob, timeout: float = 90) -> SendJob:
        self.sendq.put(job)
        job.done.wait(timeout)
        if not job.done.is_set():
            job.error = "发送排队超时"
        return job

    def _send_worker(self) -> None:
        while True:
            job = self.sendq.get()
            if self.halted:
                job.error = f"发送已熔断：{self.halted}"
                job.done.set()
                continue
            try:
                t0 = int(time.time()) - 2
                name = self.name_of(job.chat)
                if job.text is not None:
                    self.sent_hashes.append(_h(job.text))
                    self.sender.send_text(name, job.text)
                if job.image:
                    self.sender.send_image(name, job.image)
                job.ok = True
                if job.verify:
                    job.verified = self._verify(job, t0)
                    if not job.verified:
                        self.halted = f"{time.strftime('%H:%M:%S')} 发往「{name}」后核对失败，可能发错会话"
                        log.critical("已熔断，停止一切发送：%s。确认无误后 POST /resume 恢复", self.halted)
                self.last_send_ok = job.verified is not False
            except (SendError, OSError) as e:
                job.error = str(e)
                # 发送中途出错时界面状态未知（可能已点开了别的会话），保守起见同样熔断
                if "放弃发送" not in str(e):
                    self.halted = f"{time.strftime('%H:%M:%S')} 发往「{job.chat}」时出错：{e}"
                    log.critical("已熔断：%s", self.halted)
                self.last_send_ok = False
                log.error("发送到 %s 失败：%s", job.chat, e)
            finally:
                job.done.set()
                time.sleep(0.6)  # 气泡间隔

    def _verify(self, job: SendJob, t0: int, wait: float = 8) -> bool:
        want = (job.text or "").strip()
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                for mtype, text in self.reader.self_sent_since(job.chat, t0):
                    if job.image and mtype == "image":
                        return True
                    if job.text is not None and text.strip() == want:
                        return True
            except Exception:
                log.debug("核对读取失败", exc_info=True)
            time.sleep(0.5)
        return False

    # ---------- 收 ----------
    def _changed_chats(self) -> list[str]:
        changed = self.cache.refresh("session/session.db")
        for shard in self.reader.shards():
            changed = self.cache.refresh(shard) or changed
        if not changed and self.session_seen:
            return []
        out = []
        for s in self.reader.sessions(300):
            u, ts = s["username"], s["last_timestamp"] or 0
            if self.session_seen.get(u) != ts:
                if u in self.session_seen or self.session_seen.get("__init__"):
                    out.append(u)
                self.session_seen[u] = ts
        self.session_seen["__init__"] = 1
        return out

    def _allowed_chat(self, chat: str) -> bool:
        return not self.allow or chat in self.allow or self.name_of(chat) in self.allow

    def _accept(self, m: Message) -> bool:
        if m.type != "text" or not m.text.strip():
            return False
        if not self._allowed_chat(m.chat):
            return False
        if _h(m.text) in self.sent_hashes:
            return False  # 自己刚发的回复
        if m.is_self and not (self.reply_self and m.chat == "filehelper"):
            return False
        if m.chat.startswith("gh_") or m.sender.startswith("gh_") or m.chat.endswith("@openim"):
            return False
        if m.create_time < time.time() - self.stale:
            log.info("跳过过期消息 %s", m.msg_id)
            return False
        return True

    def poll_once(self) -> None:
        for chat in self._changed_chats():
            try:
                new = self.reader.poll(chat, since_time=int(self.started))
            except Exception:
                log.exception("读取 %s 失败", chat)
                continue
            acc = [m for m in new if self._accept(m)]
            if acc:
                self.pending.setdefault(chat, []).extend(acc)
                self.pending_at[chat] = time.monotonic()
        now = time.monotonic()
        for chat in [c for c, t in self.pending_at.items() if now - t >= self.settle]:
            batch = self.pending.pop(chat, [])
            self.pending_at.pop(chat, None)
            if batch:
                self.pool.submit(self._dispatch, chat, batch)

    def _payload(self, chat: str, batch: list[Message]) -> dict:
        last = batch[-1]
        names = self.self_names()
        text = "\n".join(m.text for m in batch if m.sender == last.sender)
        at_me = any(self.self_wxid in messages.at_users(m.source) for m in batch) or any(
            f"@{n}" in text for n in names)
        sender_name = self.name_of(last.sender) if last.sender else ""
        return {
            "chat_id": chat,
            "chat_type": "group" if last.is_group else "private",
            "user_id": last.sender or chat, "senderId": last.sender or chat, "sender_id": last.sender or chat,
            "senderName": sender_name, "nickname": sender_name,
            "text": text,
            "msg_id": f"{chat}:{last.shard}:" + "|".join(str(m.local_id) for m in batch),
            "at_me": at_me,
            "mentioned_names": [n for n in names if f"@{n}" in text],
            "self_names": names,
            "chat_title": self.name_of(chat) if last.is_group else "",
        }

    def _dispatch(self, chat: str, batch: list[Message]) -> None:
        with self.chat_locks[chat]:
            payload = self._payload(chat, batch)
            log.info("→ 内核 %s [%s] %s", payload["chat_title"] or self.name_of(chat), payload["senderName"],
                     payload["text"][:60])
            try:
                resp = self.kernel.webhook(payload)
            except KernelError as e:
                log.error("内核 webhook 失败：%s", e)
                return
            if resp.get("action") != "reply":
                log.info("内核不回复：%s", resp.get("reason") or resp.get("skipped") or resp.get("action"))
                return
            bubbles = [b for b in (resp.get("bubbles") or [resp.get("reply") or ""]) if b and b.strip()]
            allowed, reason = self.kernel.send_permission(chat, self.name_of(chat))
            if not allowed:
                log.info("内核不允许在 %s 发言：%s", chat, reason)
                return
            mention = resp.get("mention") or ""
            for i, b in enumerate(bubbles):
                text = f"@{mention} {b}" if (i == 0 and mention and chat.endswith("@chatroom")) else b
                self.send(SendJob(chat=chat, text=text))

    # ---------- 提醒 / 心跳 / 联系人 ----------
    def _reminder_loop(self) -> None:
        while True:
            time.sleep(5)
            try:
                items = self.kernel.reminders_due()
            except KernelError as e:
                log.debug("拉取提醒失败：%s", e)
                continue
            for it in items:
                rid = it.get("id")
                if it.get("kind") == "plugin" and not self.kernel.reminder_permission(rid):
                    self.kernel.reminder_ack(rid, False, "permission denied")
                    continue
                text = it.get("text") or ""
                if not it.get("wxid") or not text:
                    self.kernel.reminder_ack(rid, False, "missing target or text")
                    continue
                if it.get("chatType") == "group" and it.get("atName"):
                    text = f"@{it['atName']} {text}"
                job = self.send(SendJob(chat=it.get("wxid") or "", text=text))
                try:
                    self.kernel.reminder_ack(rid, job.ok and job.verified is not False, job.error)
                except KernelError as e:
                    log.warning("提醒回执失败：%s", e)

    def _heartbeat_loop(self) -> None:
        while True:
            try:
                self.kernel.heartbeat({
                    "uia": True, "rest": True, "listener": True, "ffmpeg": False,
                    "account": os.path.basename(os.path.dirname(self.db_root)), "account_wxid": self.self_wxid,
                    "account_nick": (self.reader.self_names() or [""])[0], "account_ok": True,
                    "account_why": "", "account_guard": False, "window": self.last_send_ok,
                })
            except Exception as e:
                log.debug("心跳失败：%s", e)
            time.sleep(20)

    def _sync_contacts(self) -> None:
        try:
            items = [{"wxid": c["username"], "name": c["name"], "remark": c["remark"], "isGroup": c["isGroup"]}
                     for c in self.reader.contacts()]
            self.kernel.contacts_sync(os.path.basename(os.path.dirname(self.db_root)), items)
            log.info("已同步 %d 个联系人/群到内核", len(items))
        except Exception as e:
            log.warning("同步联系人失败：%s", e)

    # ---------- 入口 ----------
    def run(self) -> None:
        log.info("等待内核就绪…")
        self.kernel.wait_ready()
        self._changed_chats()  # 记下当前各会话时间戳，不回放历史
        threading.Thread(target=self._send_worker, name="send", daemon=True).start()
        threading.Thread(target=self._reminder_loop, name="reminders", daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True).start()
        threading.Thread(target=self._sync_contacts, name="contacts", daemon=True).start()
        if os.environ.get("FUXI_API_TOKEN"):
            from .api import serve

            threading.Thread(target=serve, args=(self,), name="api", daemon=True).start()
        else:
            log.warning("未设置 FUXI_API_TOKEN，对外发送 API 不启动")
        log.info("开始监听全部会话（self=%s）", self.self_wxid[:6] + "…")
        while True:
            try:
                self.poll_once()
            except Exception:
                log.exception("轮询出错")
                time.sleep(3)
            time.sleep(self.interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Bridge().run()


if __name__ == "__main__":
    main()
