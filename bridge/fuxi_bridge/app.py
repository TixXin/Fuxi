"""浮汐桥接主程序：全会话轮询 → 内核 webhook → 发送；提醒拉取；心跳；对外发送 API。

  python -m fuxi_bridge.app

环境变量（见 deploy/.env.example）：
  FUXI_WX_CONFIG / FUXI_DB_ROOT   微信数据（只读）
  FUXI_STATE_DIR / FUXI_WORK_DIR  持久状态 / tmpfs
  FUXI_KERNEL_URL                 默认 http://127.0.0.1:8787
  FUXI_KERNEL_TOKEN               与内核 gateway.wechat.inboundToken 一致
  FUXI_API_TOKEN / FUXI_API_PORT  对外发送 API（不设 token 则不开 API）

行为设置（看图、主动搭话、主人、限速、静默时段…）不走环境变量，见 settings.py（/state/settings.json，
遥控 /设置）。白名单在 /state/allow.txt（遥控 /允许 /禁止），第一次启动只放行文件传输助手。
"""

from __future__ import annotations

import base64
import collections
import concurrent.futures as cf
import hashlib
import logging
import os
import queue
import random
import re
import threading
import time
from dataclasses import dataclass, field

from . import messages, store
from . import announce, care, control, feedback, fuyu, images, knowledge, settings, timing, voice
from .allow import AllowList
from .kernel import Kernel, KernelError
from .messages import Message
from .sender import Sender, SendError

log = logging.getLogger("fuxi")

# 交给内核的消息类型：文本 + 解析过的 appmsg（引用、链接、文件、拍一拍…）
ACCEPT_TYPES = ("text",) + messages.APP_KINDS + messages.MEDIA_KINDS


def _h(text: str) -> str:
    return hashlib.sha1(text.strip().encode()).hexdigest()


def _squash(text: str) -> str:
    """核对用：去掉所有空白（微信存库时可能改动换行/空格），其余逐字比较。"""
    return "".join(text.split())


def _brief(text: str) -> str:
    """日志里不记消息正文，只记长度和指纹（排障时可与数据库比对）。
    FUXI_LOG_TEXT=1 时记录前 60 字，仅限调试。"""
    if os.environ.get("FUXI_LOG_TEXT") == "1":
        return repr(text[:60])
    return f"<{len(text)}字 #{_h(text)[:8]}>"


VERSION_Q = re.compile(r"版本|新版|更新|升级|发版|最新|release|v\d+\.\d+", re.I)
BUG_Q = re.compile(r"修(了|好|复)|bug|问题|没反应|不行|不了|不能|看不到|打不开|闪退|崩|卡|报错|失败|异常|不显示|没效果", re.I)


@dataclass
class SendJob:
    chat: str               # username
    text: str | None = None
    source: str = "bot"     # bot（自动回复，受限速）| reminder | api
    image: str | None = None
    sticker: str | None = None  # 表情包文件（按文件粘贴，GIF 保持动图）
    at: str = ""                # 群里真 @ 的对象（成员在群里的显示名）；用不上真 @ 时退回文字「@名字」
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
        # 行为设置：/state/settings.json（见 settings.py），apply_settings 把它们抄到下面这些属性上
        self.cfg = settings.Settings(os.path.join(state, "settings.json"), dict(env))
        self.settle, self.stale, self.reply_self = 1.5, 120, False
        self.rate_chat, self.rate_global, self.quiet = 6, 20, ""
        self.extra_names: list[str] = []
        self.masters: list[str] = []
        self.care_on, self.vision_mode, self.voice_mode = True, "all", "all"
        self.kernel_ready = False
        self.sent_times: collections.deque[tuple[float, str]] = collections.deque(maxlen=1000)
        self.interval = float(env.get("FUXI_POLL_INTERVAL", "0.5"))
        # 白名单：/state/allow.txt 热加载；文件不存在时（第一次）只放行文件传输助手
        self.allowlist = AllowList(os.path.join(state, "allow.txt"), env.get("FUXI_ALLOW", "filehelper"),
                                   on_change=self._sync_kernel_allow)

        self.sendq: "queue.Queue[SendJob]" = queue.Queue()
        self.sent_hashes: collections.deque[str] = collections.deque(maxlen=500)
        self.media_sent: dict[str, float] = {}   # 会话 → bot 最近一次发图/表情包的时刻（认出自己的回声）
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
        self.care = care.Care(self, os.path.join(state, "care.json"))
        self.kernel_data = env.get("FUXI_KERNEL_DATA", "/kernel-data")  # 内核 data 卷（只读）挂在这里
        self.images = images.Images(os.path.dirname(os.path.normpath(self.db_root)), self.self_wxid,
                                    os.path.join(state, "imgkey.json"))
        self.voices = voice.Voices(self.cache)
        self.media_cache: collections.OrderedDict[str, str] = collections.OrderedDict()   # 看图/语音结果
        self.announcer = announce.Announcer(self, os.path.join(state, "announce"))
        self.knowledge = knowledge.Knowledge(os.path.join(state, "knowledge"))
        self.fuyu = fuyu.Fuyu(os.path.join(state, "fuyu"))
        self.fuyu.on_events = self._fuyu_events
        self.feedback = feedback.Feedback(os.path.join(state, "feedback"),
                                          complete=lambda system, text: self.kernel.complete(system, text, 2500),
                                          fuyu=self.fuyu)
        self.handoffs: collections.deque[tuple[float, str]] = collections.deque(maxlen=50)
        self.control = control.Control(self)
        self.apply_settings()

    # ---------- 设置 ----------
    def apply_settings(self) -> list[str]:
        """把 settings.json 的值抄到属性上；返回变了的项（中文名），并处理需要联动的。"""
        c = self.cfg
        new = {
            "settle": c.get("settle_ms") / 1000, "stale": c.get("stale_sec"), "reply_self": c.get("reply_self"),
            "rate_chat": c.get("rate_chat"), "rate_global": c.get("rate_global"), "quiet": c.get("quiet"),
            "extra_names": c.get("self_names"), "masters": c.get("owners"),
            "care_on": c.get("care_on"), "vision_mode": c.get("vision"), "voice_mode": c.get("voice"),
        }
        labels = {"settle": "settle_ms", "stale": "stale_sec", "extra_names": "self_names", "masters": "owners",
                  "vision_mode": "vision", "voice_mode": "voice"}
        changed = []
        for attr, v in new.items():
            if getattr(self, attr, None) != v:
                setattr(self, attr, v)
                changed.append(attr)
        if "masters" in changed and self.kernel_ready:
            self._sync_master()
        if "vision_mode" in changed and self.vision_mode != "off" and self.kernel_ready:
            threading.Thread(target=self.images.ensure_key, name="imgkey", daemon=True).start()
        return [settings.BY_KEY[labels.get(a, a)].label for a in changed]

    def _sync_master(self) -> None:
        try:
            self.kernel.set_master(self.masters)
            log.info("主人已同步到内核：%s", "、".join(self.name_of(m) for m in self.masters) or "（空）")
        except Exception as e:
            log.warning("同步主人到内核失败：%s", e)

    def get_setting(self, key: str):
        it = settings.BY_KEY[key]
        if it.kernel:
            v = self.kernel.config()
            for p in it.kernel:
                v = v.get(p, {}) if isinstance(v, dict) else {}
            return it.default if v == {} else v
        return self.cfg.get(key)

    def set_setting(self, key: str, raw) -> object:
        """改一项并立即生效；不合法抛 ValueError，内核拒绝抛 KernelError。"""
        it = settings.BY_KEY[key]
        if it.kernel:
            v = settings.parse(it, raw)
            self.kernel.patch_config(it.kernel, v)
            return v
        v = self.cfg.set(key, raw)
        self.apply_settings()
        return v

    def resolve_chat(self, name: str) -> str:
        """群名/备注/昵称或 chat_id → chat_id；不唯一或找不到抛 ValueError。"""
        name = (name or "").strip()
        if not name:
            raise ValueError("没有指定会话")
        if name == "filehelper" or name.endswith("@chatroom") or name.startswith("wxid_"):
            return name
        hits = self.reader.find_chats(name)
        if len(hits) != 1:
            raise ValueError(f"「{name}」匹配到 {len(hits)} 个会话，请改用 chat_id")
        return hits[0]["username"]

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
                self._attempt(job)
                for retry in range(self.RETRIES):
                    if not (job.error and "放弃发送" in job.error) or self.halted:
                        break
                    log.warning("发送到 %s 被放弃，%.1f 秒后重试（%d/%d）：%s", self.name_of(job.chat),
                                self.RETRY_DELAY, retry + 1, self.RETRIES, job.error)
                    time.sleep(self.RETRY_DELAY)
                    job.error = ""
                    self._attempt(job)
            finally:
                job.t_done = time.monotonic()
                job.done.set()
                time.sleep(0.6)  # 气泡间隔

    RETRIES = 1          # 「放弃发送」（还没输入任何内容）后重试次数
    RETRY_DELAY = 1.5

    def _attempt(self, job: SendJob) -> None:
        """发送一次。放弃（未发出）只记 job.error；粘贴之后出错或核对失败才熔断。"""
        try:
            t0 = int(time.time()) - 2
            job.t_start = time.monotonic()
            self.sender.last_open = 0.0
            name = self.name_of(job.chat)
            if job.text is not None and job.at:
                self.sent_hashes.append(_h(job.text))
                self.sender.send_text_at(name, job.at, job.text)
            elif job.text is not None:
                self.sent_hashes.append(_h(job.text))
                self.sender.send_text(name, job.text)
            if job.image:
                self.sender.send_image(name, job.image)
            if job.sticker:
                self.sender.send_sticker(name, job.sticker)
            if job.image or job.sticker:
                self.media_sent[job.chat] = time.time()
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
            # 回车之后出错时界面状态未知，保守起见熔断；回车之前的错误都带「放弃发送」
            if "放弃发送" not in str(e):
                self.halted = f"{time.strftime('%H:%M:%S')} 发往「{job.chat}」时出错：{e}"
                log.critical("已熔断：%s", self.halted)
            else:
                self.last_abort = (time.time(), str(e))
            self.last_send_ok = False
            log.error("发送到 %s 失败：%s", job.chat, e)

    def _verify(self, job: SendJob, t0: int, wait: float = 8) -> bool:
        want = _squash(job.text or "")
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                for mtype, text in self.reader.self_sent_since(job.chat, t0):
                    if job.image and mtype == "image":
                        return True
                    if job.sticker and mtype in ("emoji", "image"):
                        return True
                    if job.text is not None and _squash(text) == want:
                        return True
                    if job.at and _squash(text).startswith("@") and _squash(text).endswith(want):
                        return True   # 真 @ 存库是「@群昵称\u2005正文」
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
        if m.is_self and m.type in ("image", "emoji") and abs(m.create_time - self.media_sent.get(m.chat, 0)) < 15:
            return False  # 自己刚发的图片/表情包（文件传输助手自测时会读回来）
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
            if new and self.is_feedback_group(chat):
                self._collect(new)
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
            gone = self._revoked(batch)
            if gone:
                batch = [m for m in batch if m.msg_id not in gone]
                log.info("%s 撤回了 %d 条消息，不再回复它们", self.name_of(chat), len(gone))
                if not batch:
                    return
            self._understand(chat, batch, tr)
            k0 = time.monotonic()
            payload = self._payload(chat, batch)
            self._assist(chat, payload)
            log.info("→ 内核 %s [%s] %s", payload["chat_title"] or self.name_of(chat), payload["senderName"],
                     _brief(payload["text"]))
            try:
                resp = self.kernel.webhook(payload)
            except KernelError as e:
                log.error("内核 webhook 失败：%s", e)
                return
            sticker = resp.get("sticker") or None   # 内核已为这次回复预留的表情包任务
            delivered = False
            try:
                delivered = self._deliver(chat, batch, resp, tr, k0)
            finally:
                if sticker:
                    self._send_sticker(chat, sticker, delivered)

    # ---------- 产品助手 ----------
    def is_assistant_group(self, chat: str) -> bool:
        groups = self.cfg.get("assistant_groups")
        return bool(groups) and chat.endswith("@chatroom") and (chat in groups or self.name_of(chat) in groups)

    def _assist(self, chat: str, payload: dict) -> None:
        """产品助手群里被 @ / 被回复时：从浮屿资料里找相关段落附在消息后面给内核参考；
        找不到又像是提问时，提示内核如实说不确定，并私聊主人。"""
        if not self.is_assistant_group(chat) or not (payload.get("at_me") or payload.get("reply_to_bot")):
            return
        q = payload["text"]
        for n in payload.get("self_names") or []:
            q = q.replace(f"@{n}", " ")
        q = q.strip()
        try:
            hits = self.knowledge.search(q)
        except Exception as e:   # 检索出错不能挡住回复
            log.warning("资料检索出错：%s", e)
            hits = []
        notes, sure = self._fuyu_notes(q)
        extra = ""
        if hits:
            extra += ("\n\n（以下是浮屿的说明资料，回答浮屿用法问题时以它为准；资料里没写的不要编，"
                      "就说不确定、已经转给主人了）\n" + knowledge.render(hits))
            log.info("产品助手 %s：附上 %d 段资料（%s）", self.name_of(chat), len(hits), "、".join(h.title[:20] for h in hits))
        if notes:
            extra += "\n\n（浮屿仓库的最新情况，回答版本、修没修时参考；开发记录未必就是对方说的问题，对不上就别提）\n" + "\n".join(notes)
            log.info("产品助手 %s：附上仓库信息 %d 条", self.name_of(chat), len(notes))
        if not hits and not sure and knowledge.is_question(q):
            extra += "\n\n（浮屿资料里没找到这个问题的答案。不确定就直说，告诉对方已经转给主人了，不要编）"
            self._handoff(chat, payload, q)
        payload["text"] += extra

    def _fuyu_notes(self, q: str) -> tuple[list[str], bool]:
        """版本 / #编号 / 像问题反馈时，从 F2 的仓库缓存里找参考。返回 (几行说明, 是否把握较大可不转人工)。"""
        notes, sure = [], False
        try:
            bug = bool(BUG_Q.search(q))   # 「更新后打不开了」是故障反馈，不算问版本
            if VERSION_Q.search(q):
                notes.append(self.fuyu.version_brief())
                sure = bool(self.fuyu.latest()) and not bug
            numbers = list(dict.fromkeys(int(x) for x in re.findall(r"#(\d{1,6})", q)))
            for n in numbers:
                notes.append(self.fuyu.status(n))
                sure = True
            if bug and not numbers:
                for score, it in self.fuyu.find(q, k=2):
                    if score >= 0.45:
                        notes.append(self.fuyu.status(it["number"]))
                        sure = sure or score >= 0.55
        except Exception as e:
            log.warning("浮屿仓库信息出错：%s", e)
        return notes, sure

    def _handoff(self, chat: str, payload: dict, q: str) -> None:
        now = time.time()
        recent = [t for t, c in self.handoffs if now - t < 3600]
        if len(recent) >= 10 or any(c == chat and now - t < 180 for t, c in self.handoffs):
            log.info("产品助手 %s：转人工太频繁，这次不私聊主人", self.name_of(chat))
            return
        self.handoffs.append((now, chat))
        text = (f"【浮屿群求助】{payload.get('senderName') or '有人'} 在「{self.name_of(chat)}」问：\n{q[:300]}\n"
                "资料里没找到相关内容，可能需要你来回。")

        def go():
            job = self.send(SendJob(chat="filehelper", text=text, source="control"))
            if not job.ok:
                log.warning("转人工通知没发出去：%s", job.error)
        threading.Thread(target=go, name="handoff", daemon=True).start()
        log.info("产品助手 %s：资料里没找到，已转主人", self.name_of(chat))

    # ---------- 群反馈（F1） ----------
    def is_feedback_group(self, chat: str) -> bool:
        groups = self.cfg.get("feedback_groups")
        return bool(groups) and chat.endswith("@chatroom") and (chat in groups or self.name_of(chat) in groups)

    def _collect(self, new: list[Message]) -> None:
        """反馈群的每条消息都记下来（不管白名单、静默、有没有 @ bot），整理交给 feedback.py。"""
        for m in new:
            if m.type in ("system", "revoke") or not m.text.strip():
                continue
            if m.is_self:
                role, name = "bot", "浮汐"
            else:
                role = "owner" if m.sender in self.masters else ""
                name = self.name_of(m.sender) if m.sender else "群友"
            try:
                self.feedback.add(m.msg_id, float(m.create_time), m.chat, m.sender, name, self._render(m), role)
            except OSError as e:
                log.warning("反馈收集写入失败：%s", e)

    def _feedback_loop(self) -> None:
        """每分钟看一次：整点前后整理（每小时）、到点发摘要和群小结。"""
        time.sleep(60)
        while True:
            try:
                self._feedback_tick()
            except Exception:
                log.exception("群反馈定时任务出错")
            time.sleep(60)

    def _feedback_tick(self, now: float | None = None) -> None:
        if not self.cfg.get("feedback_groups"):
            return
        fb, now = self.feedback, now or time.time()
        digest_due = fb.due("digest", self.cfg.get("digest_time"), now)
        group_due = fb.due("group", self.cfg.get("group_summary_time"), now)
        if digest_due or group_due or now - float(fb.meta.get("classified_at") or 0) >= feedback.CLASSIFY_EVERY:
            try:
                msg = fb.classify(now)
                if msg != "没有新消息":
                    log.info("群反馈：%s", msg)
            except Exception as e:   # 整理失败也照样发（用已有的条目），错误会写在摘要末尾
                log.warning("群反馈整理失败：%s", e)
        if digest_due:
            since = float(fb.meta.get("digest_at") or 0) or now - 86400
            text = fb.digest(since=max(since, now - 3 * 86400))
            to = self.masters[0] if self.masters else "filehelper"
            fb.mark("digest", now)
            job = self.send(SendJob(chat=to, text=text, source="control"))
            if job.ok:
                log.info("反馈摘要已发给主人")
            else:
                log.warning("反馈摘要没发出去：%s", job.error)
        if group_due:
            fb.mark("group", now)
            text = fb.group_summary()
            if not text:
                log.info("群反馈：今天没有反馈，不发群小结")
                return
            day = feedback._day(now)
            chats = {c for i in fb.items if feedback._day(i["last_seen"]) == day for c in i["chats"]}
            for chat in sorted(chats):
                if not self.is_feedback_group(chat) or not self._allowed_chat(chat):
                    continue
                job = self.send(SendJob(chat=chat, text=text, source="control"))
                if job.ok:
                    log.info("群小结已发到 %s", self.name_of(chat))
                else:
                    log.warning("群小结没发到 %s：%s", self.name_of(chat), job.error)

    def _fuyu_events(self, events: list[dict]) -> None:
        """浮屿仓库同步发现变化：按「浮屿动态」设置私聊主人（文件传输助手）。"""
        mode = self.cfg.get("fuyu_notify")
        if mode == "off":
            return
        if mode == "release":
            events = [e for e in events if e.get("type") == "new_release"]
        if not events:
            return
        lines = [fuyu.render_event(e) for e in events[:12]]
        if len(events) > 12:
            lines.append(f"……还有 {len(events) - 12} 条（/浮屿 看最近动态）")
        text = "【浮屿动态】\n" + "\n".join("- " + x for x in lines)

        def go():   # 同步线程还拿着锁，发送可能要等几十秒，放到别的线程里
            job = self.send(SendJob(chat="filehelper", text=text, source="control"))
            if not job.ok:
                log.warning("浮屿动态通知没发出去：%s", job.error)
        threading.Thread(target=go, name="fuyu-notify", daemon=True).start()

    def _understand(self, chat: str, batch: list[Message], tr: "timing.Trace") -> None:
        """图片、语音交给内核前先看懂/听懂：「[图片]（图片内容：…）」「[语音 5秒]（语音内容：…）」。
        只处理最后发言人的（_payload 只带这些），每种最多最后三条。任何失败都保留原占位，不影响回复。"""
        group = chat.endswith("@chatroom")
        last = batch[-1].sender
        t0 = time.monotonic()
        did = False
        for kind, mode, label in (("image", self.vision_mode, "图片内容"), ("voice", self.voice_mode, "语音内容")):
            if mode == "off" or (mode == "private" and group):
                continue
            todo = [m for m in batch if m.type == kind and m.media and m.sender == last]
            for m in todo[-3:]:
                key = f"{kind}:{chat}:{m.media}"
                got = self.media_cache.get(key)
                if got is None:
                    did = True
                    try:
                        got = self._recognize(chat, kind, m.media)
                    except (ValueError, KernelError) as e:
                        log.info("%s失败 %s：%s", "看图" if kind == "image" else "语音识别", self.name_of(chat), e)
                        continue
                    self.media_cache[key] = got
                    while len(self.media_cache) > 300:
                        self.media_cache.popitem(last=False)
                if got:
                    m.text = f"{m.text}（{label}：{got}）"
                    if self.is_feedback_group(chat):
                        self.feedback.annotate(m.msg_id, self._render(m))
        if did:
            tr.add("vision", time.monotonic() - t0)

    def _recognize(self, chat: str, kind: str, media: str) -> str:
        if kind == "image":
            jpeg, src = self.images.load(chat, media)
            text = self.kernel.vision(base64.b64encode(jpeg).decode())
            log.info("看图 %s：%s（%d 字）", self.name_of(chat), src, len(text))
            return text
        wav = self.voices.load(chat, int(media))
        text = self.kernel.asr(base64.b64encode(wav).decode())
        log.info("语音识别 %s：%.1f 秒 → %d 字", self.name_of(chat), (len(wav) - 44) / 32000, len(text))
        return text

    def _deliver(self, chat: str, batch: list[Message], resp: dict, tr: "timing.Trace", k0: float) -> bool:
        """发出内核的文字回复；全部气泡都发成功才返回 True（随后才发表情包）。"""
        if resp.get("action") != "reply":
            log.info("内核不回复：%s", resp.get("reason") or resp.get("skipped") or resp.get("action"))
            return False
        bubbles = [b for b in (resp.get("bubbles") or [resp.get("reply") or ""]) if b and b.strip()]
        allowed, reason = self.kernel.send_permission(chat, self.name_of(chat))
        if not allowed:
            log.info("内核不允许在 %s 发言：%s", chat, reason)
            return False
        tr.add("kernel", time.monotonic() - k0)
        mention = resp.get("mention") or ""
        for i, b in enumerate(bubbles):
            if self.paused:
                return False
            if not self.rate_ok(chat):
                log.warning("超过发送频率上限，丢弃 %s 的剩余 %d 条回复", self.name_of(chat), len(bubbles) - i)
                return False
            at, text = "", b
            if i == 0 and mention and chat.endswith("@chatroom"):
                at = self._mention_name(chat, batch, mention)
                head = text.lstrip()
                if head.startswith("@" + mention):   # 模型自己写了「@名字」开头：去掉，换成真 @
                    text = head[len(mention) + 1:].lstrip(" \u2005：:，,") or text
            delay = self.first_delay(text, tr.s["kernel"]) if i == 0 else self.typing_delay(text)
            time.sleep(delay)
            tr.add("typing", delay)
            if i == 0 and self._revoked(batch):
                log.info("%s 在生成回复期间撤回了消息，放弃这次回复", self.name_of(chat))
                return False
            self.sent_times.append((time.time(), chat))
            job = self.send(SendJob(chat=chat, text=text, at=at))
            if not job.ok:
                return False
            if job.t_sent:
                tr.add("queue", job.t_start - job.t_queued)
                tr.add("open", job.open_s)
                tr.add("paste", job.t_sent - job.t_start - job.open_s)
                tr.add("verify", job.t_done - job.t_sent)
                tr.bubble_sent(time.time() - (time.monotonic() - job.t_sent))
        return True

    def _mention_name(self, chat: str, batch: list[Message], mention: str) -> str:
        """内核给的 @ 对象（名字）→ 该成员在群里的显示名（@ 弹窗按它过滤）。
        认不出是这批消息里的谁，就原样用内核给的名字（多半会退回文字 @）。"""
        for m in reversed(batch):
            if not m.sender or m.sender == self.self_wxid:
                continue
            try:
                shown = self.reader.member_name(chat, m.sender)
            except Exception:
                log.debug("读群昵称失败", exc_info=True)
                shown = ""
            names = [n for n in (shown, self.name_of(m.sender)) if n]
            # 内核取 @ 名字时遇空格截断（「tixxin - 建议…」→「tixxin」），所以前缀也算
            if mention == m.sender or any(n == mention or n.startswith(mention) for n in names):
                return shown or mention
        return mention

    def _sticker_file(self, path: str) -> str:
        """内核容器里的路径（/app/data/...）→ 桥接这边只读挂载的同一个文件。"""
        for prefix in ("/app/data/", "data/"):
            if path.startswith(prefix):
                return os.path.join(self.kernel_data, path[len(prefix):])
        return path

    def _send_sticker(self, chat: str, item: dict, delivered: bool) -> None:
        """文字发完后发内核预留的表情包：领取 → 发送 → 回执。文字没发成就取消预留。"""
        sid = item.get("id") or ""
        try:
            if not delivered or self.paused or self.halted or not self.rate_ok(chat):
                self.kernel.sticker_cancel(item)
                log.info("取消表情包任务（文字回复没发完或已暂停）")
                return
            got = self.kernel.sticker_claim(sid)
            path = self._sticker_file(got.get("path") or item.get("path") or "")
            if not os.path.isfile(path):
                self.kernel.sticker_ack(sid, "failed", "表情包文件桥接读不到（内核 data 卷没挂载？）")
                log.error("表情包文件读不到：%s", path)
                return
            time.sleep(random.uniform(0.8, 2.0))
            self.sent_times.append((time.time(), chat))
            job = self.send(SendJob(chat=chat, sticker=path))
            status = "failed" if not job.ok else ("confirmed" if job.verified else "unconfirmed")
            self.kernel.sticker_ack(sid, status, job.error)
        except KernelError as e:
            log.warning("表情包任务处理失败：%s", e)

    def _revoked(self, batch: list[Message]) -> set[str]:
        try:
            return self.reader.revoked(batch)
        except Exception:
            log.exception("检查撤回失败")
            return set()

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
        if self.masters:
            self._sync_master()
        self.pending_boot = self._changed_chats()  # 记下各会话进度；过期窗口内有动静的稍后补处理
        threading.Thread(target=self._send_worker, name="send", daemon=True).start()
        threading.Thread(target=self._reminder_loop, name="reminders", daemon=True).start()
        if self.vision_mode != "off":
            threading.Thread(target=self.images.ensure_key, name="imgkey", daemon=True).start()
        threading.Thread(target=self.care.loop, name="care", daemon=True).start()   # 关着时 tick 直接跳过
        threading.Thread(target=self.knowledge.loop, name="knowledge", daemon=True,
                         args=(lambda: self.cfg.get("knowledge_repo"), lambda: bool(self.cfg.get("assistant_groups")))).start()
        threading.Thread(target=self.fuyu.loop, name="fuyu", daemon=True,
                         args=(lambda: self.cfg.get("knowledge_repo"), lambda: bool(self.cfg.get("fuyu_sync")))).start()
        threading.Thread(target=self._feedback_loop, name="feedback", daemon=True).start()
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
        next_cfg = 0.0
        while True:
            try:
                if time.monotonic() >= next_cfg:   # settings.json 被外部改了就重新读
                    next_cfg = time.monotonic() + 2
                    if self.cfg.reload():
                        log.info("设置已更新：%s", "、".join(self.apply_settings()) or "无变化")
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
