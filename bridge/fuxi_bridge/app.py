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
  FUXI_RATE_CHAT / FUXI_RATE_GLOBAL  自动回复每会话 / 全局每分钟上限，默认 6 / 20
  FUXI_QUIET                      夜间静默时段，如 00:30-07:30（期间不自动回复）
  FUXI_ALLOW                      初始白名单（chat_id，逗号分隔），仅在 /state/allow.txt 不存在时使用
"""

from __future__ import annotations

import collections
import concurrent.futures as cf
import hashlib
import logging
import os
import queue
import random
import threading
import time
from dataclasses import dataclass, field

from . import messages, store
from . import control, timing
from .allow import AllowList
from .kernel import Kernel, KernelError
from .messages import Message
from .sender import Sender, SendError

log = logging.getLogger("fuxi")

# 交给内核的消息类型：文本 + 解析过的 appmsg（引用、链接、文件、拍一拍…）
ACCEPT_TYPES = ("text",) + messages.APP_KINDS


def _h(text: str) -> str:
    return hashlib.sha1(text.strip().encode()).hexdigest()


def _brief(text: str) -> str:
    """日志里不记消息正文，只记长度和指纹（排障时可与数据库比对）。
    FUXI_LOG_TEXT=1 时记录前 60 字，仅限调试。"""
    if os.environ.get("FUXI_LOG_TEXT") == "1":
        return repr(text[:60])
    return f"<{len(text)}字 #{_h(text)[:8]}>"


@dataclass
class SendJob:
    chat: str               # username
    text: str | None = None
    source: str = "bot"     # bot（自动回复，受限速）| reminder | api
    image: str | None = None
    verify: bool = True
    done: threading.Event = field(default_factory=threading.Event)
    ok: bool = False
    verified: bool | None = None
    error: str = ""
    # 耗时（time.monotonic）：入队、开始处理、回车发出、核对完成；进入会话用时（秒）
    t_queued: float = 0.0
    t_start: float = 0.0
    t_sent: float = 0.0
    t_done: float = 0.0
    open_s: float = 0.0


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
        # 防封节奏：自动回复每会话/全局每分钟上限、打字延迟、夜间静默（如 00:30-07:30）
        self.rate_chat = int(env.get("FUXI_RATE_CHAT", "6"))
        self.rate_global = int(env.get("FUXI_RATE_GLOBAL", "20"))
        self.quiet = env.get("FUXI_QUIET", "").strip()
        self.sent_times: collections.deque[tuple[float, str]] = collections.deque(maxlen=1000)
        self.extra_names = [n.strip() for n in env.get("FUXI_SELF_NAMES", "").split(",") if n.strip()]
        self.interval = float(env.get("FUXI_POLL_INTERVAL", "0.5"))
        # 白名单：/state/allow.txt 热加载；FUXI_ALLOW 只在文件不存在时用作初始值
        self.allowlist = AllowList(os.path.join(state, "allow.txt"), env.get("FUXI_ALLOW", ""),
                                   on_change=self._sync_kernel_allow)

        self.sendq: "queue.Queue[SendJob]" = queue.Queue()
        self.sent_hashes: collections.deque[str] = collections.deque(maxlen=500)
        self.pending: dict[str, list[Message]] = {}
        self.pending_at: dict[str, float] = {}
        self.pending_seen: dict[str, float] = {}   # 最后一条待处理消息被读到的时刻（time.time）
        self.latency = timing.LatencyStats()
        self.chat_locks: dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
        self.pool = cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="chat")
        self.session_seen: dict[str, int] = {}
        self.names_cache: dict[str, str] = {}
        self.started = time.time()
        self.last_send_ok = True
        # 熔断：发送后核对失败（疑似发错会话）就停止一切发送，直到人工 POST /resume 或重启
        self.halted: str = ""
        # 健康状态（/healthz 汇总，宿主侧看门狗据此推送 Kuma 告警）
        self.last_poll_ok_at = 0.0
        self.kernel_ok_at = 0.0
        self.last_abort: tuple[float, str] | None = None  # 最近一次发送前拦截
        # 暂停（文件传输助手 /暂停）：停自动回复与提醒，落盘以便重启后保持
        self.paused_path = os.path.join(state, "paused")
        self.paused = os.path.exists(self.paused_path)
        self.control = control.Control(self)

    def set_paused(self, on: bool) -> None:
        self.paused = on
        if on:
            with open(self.paused_path, "w") as f:
                f.write(time.strftime("%F %T\n"))
        elif os.path.exists(self.paused_path):
            os.remove(self.paused_path)
        log.warning("自动回复与提醒已%s", "暂停" if on else "恢复")

    # ---------- 名字 ----------
    def name_of(self, username: str) -> str:
        if username not in self.names_cache:
            self.names_cache[username] = self.reader.display_name(username)
        return self.names_cache[username]

    def self_names(self) -> list[str]:
        return list(dict.fromkeys(self.extra_names + self.reader.self_names()))

    # ---------- 节奏 ----------
    def in_quiet_hours(self, now: float | None = None) -> bool:
        if not self.quiet or "-" not in self.quiet:
            return False
        try:
            a, b = (int(x[:2]) * 60 + int(x[3:5]) for x in self.quiet.split("-", 1))
        except ValueError:
            return False
        t = time.localtime(now or time.time())
        m = t.tm_hour * 60 + t.tm_min
        return a <= m < b if a <= b else (m >= a or m < b)

    def rate_ok(self, chat: str) -> bool:
        cut = time.time() - 60
        recent = [c for t, c in self.sent_times if t >= cut]
        return len(recent) < self.rate_global and recent.count(chat) < self.rate_chat

    @staticmethod
    def typing_delay(text: str) -> float:
        """模拟打字：约每秒 6 字，0.8~6 秒，加随机抖动。"""
        return min(6.0, 0.8 + len(text) / 6) + random.uniform(0, 0.8)

    @classmethod
    def first_delay(cls, text: str, spent: float) -> float:
        """第一个气泡：像人边想边打，模型已经花掉的时间算进打字时间里，至少留 0.3~0.8 秒。"""
        return max(random.uniform(0.3, 0.8), cls.typing_delay(text) - spent)

    # ---------- 发送（单线程串行） ----------
    def send(self, job: SendJob, timeout: float = 90) -> SendJob:
        job.t_queued = time.monotonic()
        self.sendq.put(job)
        job.done.wait(timeout)
        if not job.done.is_set():
            job.error = "发送排队超时"
        return job

    def _send_worker(self) -> None:
        while True:
            job = self.sendq.get()
            # 遥控回报（只发文件传输助手）在熔断时也放行，否则主人看不到 /状态
            if self.halted and not (job.source == "control" and job.chat == "filehelper"):
                job.error = f"发送已熔断：{self.halted}"
                job.done.set()
                continue
            try:
                t0 = int(time.time()) - 2
                job.t_start = time.monotonic()
                self.sender.last_open = 0.0
                name = self.name_of(job.chat)
                if job.text is not None:
                    self.sent_hashes.append(_h(job.text))
                    self.sender.send_text(name, job.text)
                if job.image:
                    self.sender.send_image(name, job.image)
                job.ok = True
                job.t_sent = time.monotonic()
                job.open_s = self.sender.last_open
                if job.verify:
                    job.verified = self._verify(job, t0)
                    if not job.verified:
                        self.sender.forget_chat()
                        self.halted = f"{time.strftime('%H:%M:%S')} 发往「{name}」后核对失败，可能发错会话"
                        log.critical("已熔断，停止一切发送：%s。确认无误后 POST /resume 恢复", self.halted)
                self.last_send_ok = job.verified is not False
            except (SendError, OSError) as e:
                job.error = str(e)
                # 发送中途出错时界面状态未知（可能已点开了别的会话），保守起见同样熔断
                if "放弃发送" not in str(e):
                    self.halted = f"{time.strftime('%H:%M:%S')} 发往「{job.chat}」时出错：{e}"
                    log.critical("已熔断：%s", self.halted)
                else:
                    self.last_abort = (time.time(), str(e))
                self.last_send_ok = False
                log.error("发送到 %s 失败：%s", job.chat, e)
            finally:
                job.t_done = time.monotonic()
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
                # 启动后新出现的会话、或启动时就在过期窗口内有新动静的会话（停机补偿）
                if u in self.session_seen or self.session_seen.get("__init__") or ts >= time.time() - self.stale:
                    out.append(u)
                self.session_seen[u] = ts
        self.session_seen["__init__"] = 1
        return out

    def _allowed_chat(self, chat: str) -> bool:
        return self.allowlist.allows(chat)

    def _sync_kernel_allow(self, items: list[str]) -> None:
        if not getattr(self, "kernel_ready", False):
            return  # 内核就绪后 run() 里会补一次
        self.kernel.set_whitelist(items)
        log.info("白名单已同步到内核（%s）", "全部放行" if "*" in items else f"{len(items)} 个会话")

    def _accept(self, m: Message) -> bool:
        if m.type not in ACCEPT_TYPES or not m.text.strip():
            return False
        if m.type == "pat" and m.patted != self.self_wxid:
            return False  # 只关心拍了拍「我」
        if self.in_quiet_hours():
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
        self.allowlist.reload()
        boot, self.pending_boot = getattr(self, "pending_boot", []), []
        if boot:
            log.info("停机补偿：检查 %d 个近期有动静的会话", len(boot))
        for chat in list(dict.fromkeys(boot + self._changed_chats())):
            try:
                new = self.reader.poll(chat, since_time=int(self.started) - self.stale)
            except Exception:
                log.exception("读取 %s 失败", chat)
                continue
            cmds = [m for m in new if control.is_command(m, self.self_wxid)]
            for m in cmds:
                if m.create_time >= time.time() - self.stale:
                    self.pool.submit(self._run_command, m.text)
            acc = [m for m in new if m not in cmds and self._accept(m)]
            if acc:
                self.pending.setdefault(chat, []).extend(acc)
                self.pending_at[chat] = time.monotonic()
                self.pending_seen[chat] = time.time()
        now = time.monotonic()
        for chat in [c for c, t in self.pending_at.items() if now - t >= self.settle]:
            batch = self.pending.pop(chat, [])
            self.pending_at.pop(chat, None)
            seen = self.pending_seen.pop(chat, time.time())
            if batch:
                self.pool.submit(self._dispatch, chat, batch, seen)

    def _render(self, m: Message) -> str:
        """交给内核的正文：引用消息在回复后面附上被引用的内容。"""
        q = m.quote
        if not q:
            return m.text
        who = "你" if q.get("sender") == self.self_wxid else (q.get("name") or self.name_of(q.get("sender", "")))
        return f"{m.text}\n（引用 {who} 的消息：{q.get('text') or '[消息]'}）"

    def _run_command(self, text: str) -> None:
        reply = self.control.handle(text)
        job = self.send(SendJob(chat="filehelper", text=reply, source="control"))
        if job.error:
            log.error("遥控回报发送失败：%s", job.error)

    def _payload(self, chat: str, batch: list[Message]) -> dict:
        last = batch[-1]
        names = self.self_names()
        mine = [m for m in batch if m.sender == last.sender]
        bare = "\n".join(m.text for m in mine)          # @ 识别只看本人写的内容，不看被引用的
        text = "\n".join(self._render(m) for m in mine)
        at_me = (any(self.self_wxid in messages.at_users(m.source) for m in batch)
                 or any(f"@{n}" in bare for n in names)
                 or any(m.type == "pat" for m in mine))
        quote = next((m.quote for m in reversed(mine) if m.quote), {})
        sender_name = self.name_of(last.sender) if last.sender else ""
        return {
            "chat_id": chat,
            "chat_type": "group" if last.is_group else "private",
            "user_id": last.sender or chat, "senderId": last.sender or chat, "sender_id": last.sender or chat,
            "senderName": sender_name, "nickname": sender_name,
            "text": text,
            "msg_id": f"{chat}:{last.shard}:" + "|".join(str(m.local_id) for m in batch),
            "at_me": at_me,
            "reply_to_bot": quote.get("sender") == self.self_wxid if quote else False,
            "reply_to_name": quote.get("name", ""),
            "reply_to_user_id": quote.get("sender", ""),
            "mentioned_names": [n for n in names if f"@{n}" in bare],
            "self_names": names,
            "chat_title": self.name_of(chat) if last.is_group else "",
        }

    def _dispatch(self, chat: str, batch: list[Message], seen: float | None = None) -> None:
        if self.paused:
            log.info("已暂停，不处理 %s 的 %d 条消息", self.name_of(chat), len(batch))
            return
        tr = timing.Trace(batch[-1].create_time, seen or time.time())
        tr.add("settle", time.time() - tr.seen)
        try:
            self._dispatch_traced(chat, batch, tr)
        finally:
            if tr.bubbles:
                self.latency.add(tr.result())
                log.info("回复耗时 %s %s", self.name_of(chat), tr.summary())

    def _dispatch_traced(self, chat: str, batch: list[Message], tr: "timing.Trace") -> None:
        with self.chat_locks[chat]:
            k0 = time.monotonic()
            payload = self._payload(chat, batch)
            log.info("→ 内核 %s [%s] %s", payload["chat_title"] or self.name_of(chat), payload["senderName"],
                     _brief(payload["text"]))
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
            tr.add("kernel", time.monotonic() - k0)
            mention = resp.get("mention") or ""
            for i, b in enumerate(bubbles):
                if self.paused:
                    return
                if not self.rate_ok(chat):
                    log.warning("超过发送频率上限，丢弃 %s 的剩余 %d 条回复", self.name_of(chat), len(bubbles) - i)
                    return
                text = f"@{mention} {b}" if (i == 0 and mention and chat.endswith("@chatroom")) else b
                delay = self.first_delay(text, tr.s["kernel"]) if i == 0 else self.typing_delay(text)
                time.sleep(delay)
                tr.add("typing", delay)
                self.sent_times.append((time.time(), chat))
                job = self.send(SendJob(chat=chat, text=text))
                if job.ok and job.t_sent:
                    tr.add("queue", job.t_start - job.t_queued)
                    tr.add("open", job.open_s)
                    tr.add("paste", job.t_sent - job.t_start - job.open_s)
                    tr.add("verify", job.t_done - job.t_sent)
                    tr.bubble_sent(time.time() - (time.monotonic() - job.t_sent))

    # ---------- 健康 ----------
    def problems(self) -> list[str]:
        """当前需要人关注的问题；空列表 = 健康。"""
        now = time.time()
        out = []
        if self.halted:
            out.append(f"已熔断：{self.halted}")
        state = self.sender.window_state()
        if state != "ok":
            out.append({"login": "微信待登录（需在手机上确认）", "missing": "找不到微信窗口",
                        "tray": "微信主窗口被隐藏到托盘"}.get(state, state))
        for rel in ("session/session.db", "message/message_0.db"):
            if rel not in self.keyring.keys:
                out.append(f"缺少密钥：{rel}")
        if now - self.last_poll_ok_at > 120:
            out.append("消息轮询停滞或持续出错")
        if now - self.kernel_ok_at > 90:
            out.append("内核不可达")
        if self.last_abort and now - self.last_abort[0] < 600:
            out.append(f"10 分钟内有发送被拦截：{self.last_abort[1]}")
        return out

    # ---------- 提醒 / 心跳 / 联系人 ----------
    def _reminder_loop(self) -> None:
        while True:
            time.sleep(5)
            if self.paused:
                continue  # 不取：取出即标记 sending，暂停期间留在内核里
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
                allowed, reason = self.kernel.send_permission(it["wxid"], self.name_of(it["wxid"]))
                if not allowed:
                    self.kernel.reminder_ack(rid, False, f"not allowed: {reason}")
                    continue
                if it.get("chatType") == "group" and it.get("atName"):
                    text = f"@{it['atName']} {text}"
                job = self.send(SendJob(chat=it.get("wxid") or "", text=text, source="reminder"))
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
                self.kernel_ok_at = time.time()
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
        self.kernel_ready = True
        try:
            self._sync_kernel_allow(self.allowlist.items)
        except Exception as e:
            log.warning("同步白名单到内核失败：%s", e)
        self.pending_boot = self._changed_chats()  # 记下各会话进度；过期窗口内有动静的稍后补处理
        threading.Thread(target=self._send_worker, name="send", daemon=True).start()
        threading.Thread(target=self._reminder_loop, name="reminders", daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True).start()
        threading.Thread(target=self._sync_contacts, name="contacts", daemon=True).start()
        from .sender import _ocr

        threading.Thread(target=_ocr, name="ocr-warmup", daemon=True).start()
        if os.environ.get("FUXI_API_TOKEN"):
            from .api import serve

            threading.Thread(target=serve, args=(self,), name="api", daemon=True).start()
        else:
            log.warning("未设置 FUXI_API_TOKEN，对外发送 API 不启动")
        log.info("开始监听全部会话（self=%s）", self.self_wxid[:6] + "…")
        while True:
            try:
                self.poll_once()
                self.last_poll_ok_at = time.time()
            except Exception:
                log.exception("轮询出错")
                time.sleep(3)
            time.sleep(self.interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Bridge().run()


if __name__ == "__main__":
    main()
