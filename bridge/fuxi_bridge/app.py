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

import collections
import concurrent.futures as cf
import json
import logging
import os
import queue
import re
import threading
import time

from . import messages, store
from . import announce, assist, care, community, control, events, feedback, fuyu, images, issues, knowledge, outreach, owner_agent, remote_agent, schedule, settings, timing, voice
from . import allow
from .allow import AllowList
from .assist import AssistMixin
from .groupwork import GroupWorkMixin
from .kernel import Kernel, KernelError
from .messages import Message
from .replying import ReplyMixin
from .sender import Sender
from .sending import SendingMixin, SendJob, _h, _squash  # noqa: F401  SendJob/_squash 其他模块和测试从这里导入

log = logging.getLogger("fuxi")

# 交给内核的消息类型：文本 + 解析过的 appmsg（引用、链接、文件、拍一拍…）
ACCEPT_TYPES = ("text",) + messages.APP_KINDS + messages.MEDIA_KINDS


def _brief(text: str) -> str:
    """日志里不记消息正文，只记长度和指纹（排障时可与数据库比对）。
    FUXI_LOG_TEXT=1 时记录前 60 字，仅限调试。"""
    if os.environ.get("FUXI_LOG_TEXT") == "1":
        return repr(text[:60])
    return f"<{len(text)}字 #{_h(text)[:8]}>"


class Bridge(SendingMixin, ReplyMixin, AssistMixin, GroupWorkMixin):
    """桥接主体。按职责拆在几个 mixin 里：发送（sending.py）、回复后半段（replying.py）、
    产品助手（assist.py）、浮屿用户群的后台工作（groupwork.py）；这里是收消息、交给内核、设置和启动。"""

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
        self.settle, self.group_settle, self.stale, self.boot_catchup_min, self.reply_self = 1.5, 3.5, 120, 60, False
        self.catchup_path = os.path.join(state, "boot_catchup.json")
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
        self.allowlist.name_of = self.name_of

        self.sendq: "queue.Queue[SendJob]" = queue.Queue()
        self.sent_hashes: collections.deque[str] = collections.deque(maxlen=500)
        self._control_hashes: collections.deque[str] = collections.deque(maxlen=200)
        self.media_sent: dict[str, float] = {}   # 会话 → bot 最近一次发图/表情包的时刻（认出自己的回声）
        self.pending: dict[str, list[Message]] = {}
        self.pending_at: dict[str, float] = {}
        self.pending_seen: dict[str, float] = {}   # 最后一条待处理消息被读到的时刻（time.time）
        self.latency = timing.LatencyStats()
        self.events = events.Events()   # 管理台的对话流和今天的数字（只在内存）
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
        self.templates = schedule.Templates(os.path.join(state, "templates.json"))
        self.scheduler = schedule.Scheduler(self, state)
        self.jobs = schedule.AsyncJobs(self)
        self.knowledge = knowledge.Knowledge(os.path.join(state, "knowledge"))
        self.fuyu = fuyu.Fuyu(os.path.join(state, "fuyu"))
        self.fuyu.on_events = self._fuyu_events
        self.feedback = feedback.Feedback(os.path.join(state, "feedback"),
                                          complete=lambda system, text: self.kernel.complete(system, text, 2500),
                                          fuyu=self.fuyu)
        self.outreach = outreach.Outreach(self, os.path.join(state, "outreach"))
        self.community = community.CommunityWatcher(self, os.path.join(state, "community"))
        self.remote = remote_agent.RemoteAgent(self, os.path.join(state, "remote"))
        self.fixflow = issues.FixFlow(self.feedback, issues.Issues(lambda: self.cfg.get("knowledge_repo") or self.fuyu.repo))
        self.handoffs: collections.deque[tuple[float, str]] = collections.deque(maxlen=50)
        self._fuyu_proactive_at: dict[str, float] = {}
        self._group_silenced_until: dict[str, float] = {}
        self._chat_msg_seq: dict[str, int] = {}
        self.control = control.Control(self)
        self.owner_agent = owner_agent.OwnerAgent(self, state)
        self.apply_settings()

    # ---------- 设置 ----------
    def apply_settings(self) -> list[str]:
        """把 settings.json 的值抄到属性上；返回变了的项（中文名），并处理需要联动的。"""
        c = self.cfg
        new = {
            "settle": c.get("settle_ms") / 1000, "group_settle": c.get("group_settle_ms") / 1000,
            "stale": c.get("stale_sec"), "boot_catchup_min": c.get("boot_catchup_min"),
            "reply_self": c.get("reply_self"),
            "rate_chat": c.get("rate_chat"), "rate_global": c.get("rate_global"), "quiet": c.get("quiet"),
            "extra_names": c.get("self_names"), "masters": c.get("owners"),
            "care_on": c.get("care_on"), "vision_mode": c.get("vision"), "voice_mode": c.get("voice"),
        }
        labels = {"settle": "settle_ms", "group_settle": "group_settle_ms", "stale": "stale_sec",
                  "extra_names": "self_names", "masters": "owners", "vision_mode": "vision", "voice_mode": "voice"}
        changed = []
        for attr, v in new.items():
            if getattr(self, attr, None) != v:
                setattr(self, attr, v)
                changed.append(attr)
        al = getattr(self, "allowlist", None)
        scope, blocked = c.get("reply_scope"), set(c.get("blocked_chats"))
        if al is not None and (scope, blocked) != (al.scope, al.blocked):
            if scope != al.scope:
                changed.append("reply_scope")
                log.warning("回复范围：%s", allow.SCOPES.get(scope, scope))
            if blocked != al.blocked:
                changed.append("blocked_chats")
            al.scope, al.blocked = scope, blocked
            if "reply_scope" in changed and self.kernel_ready:
                try:
                    self._sync_kernel_allow(al.items)
                except Exception as e:
                    log.warning("同步白名单到内核失败：%s", e)
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
            if v == {}:
                return it.default
            return round(v * it.scale) if it.scale != 1 and isinstance(v, (int, float)) else v
        return self.cfg.get(key)

    def set_setting(self, key: str, raw) -> object:
        """改一项并立即生效；不合法抛 ValueError，内核拒绝抛 KernelError。"""
        it = settings.BY_KEY[key]
        if it.kernel:
            v = settings.parse(it, raw)
            self.kernel.patch_config(it.kernel, v / it.scale if it.scale != 1 else v)
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
        self.kernel.set_whitelist(items, open_=self.allowlist.open)
        log.info("白名单已同步到内核（%s）", self.allowlist.brief())

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
            names = self.self_names()
            cmds = [m for m in new if control.is_command(m, self.self_wxid, self.masters, names)]
            if new and self.is_feedback_group(chat):
                non_cmds = [m for m in new if m not in cmds]
                if non_cmds:
                    self._collect(non_cmds)
            for m in cmds:
                if m.create_time >= time.time() - self.stale:
                    self.pool.submit(self._run_command, control.extract_command(m.text, names), m.chat)
            acc = [m for m in new if m not in cmds and self._accept(m)]
            if acc:
                self._chat_msg_seq[chat] = self._chat_msg_seq.get(chat, 0) + len(acc)
                self.pending.setdefault(chat, []).extend(acc)
                self.pending_at[chat] = time.monotonic()
                self.pending_seen[chat] = time.time()
        now = time.monotonic()
        ready = [
            c for c, t in self.pending_at.items()
            if now - t >= (self.group_settle if c.endswith("@chatroom") else self.settle)
        ]
        for chat in ready:
            batch = self.pending.pop(chat, [])
            self.pending_at.pop(chat, None)
            seen = self.pending_seen.pop(chat, time.time())
            chunks = self._split_by_sender(batch)
            cur_seq = self._chat_msg_seq.get(chat, 0)
            for idx, chunk in enumerate(chunks):
                # 同一合并窗口里后面已有其他发言人的消息 → 告诉前一批（用于主动答疑让贤）
                seq_for_chunk = cur_seq - 1 if idx < len(chunks) - 1 else cur_seq
                self.pool.submit(self._dispatch, chat, chunk, seen, seq_for_chunk)

    @staticmethod
    def _split_by_sender(batch: list[Message]) -> list[list[Message]]:
        """群聊合并窗口内可能有多人同时发言：按发言人拆成独立批次（保持先后顺序），
        避免只取最后发言人而吞掉前一个人的 @ 或提问。"""
        if len(batch) <= 1 or not batch[-1].is_group:
            return [batch] if batch else []
        groups: list[list[Message]] = []
        by_sender: dict[str, list[Message]] = {}
        for m in batch:
            key = m.sender or ""
            if key not in by_sender:
                by_sender[key] = []
                groups.append(by_sender[key])
            by_sender[key].append(m)
        return groups

    def _render(self, m: Message) -> str:
        """交给内核的正文：引用消息在回复后面附上被引用的内容。"""
        q = m.quote
        if not q:
            return m.text
        who = "你" if q.get("sender") == self.self_wxid else (q.get("name") or self.name_of(q.get("sender", "")))
        return f"{m.text}\n（引用 {who} 的消息：{q.get('text') or '[消息]'}）"

    def _run_command(self, text: str, reply_to: str = "filehelper") -> None:
        reply = self.control.handle(text)
        ch = getattr(self, "_control_hashes", None)
        if ch is not None and reply:
            ch.append(_h(reply))
        job = self.send(SendJob(chat=reply_to, text=reply, source="control"))
        if job.error:
            log.error("遥控回报发送失败（%s）：%s", reply_to, job.error)

    @staticmethod
    def _is_blind_media(batch: list[Message]) -> bool:
        """这批消息（最后发言人）是否全是未看懂/未听懂的纯媒体（如群里关着看图时的 [图片]、[视频]、[动画表情]、[语音]）。"""
        last = batch[-1]
        mine = [m for m in batch if m.sender == last.sender]
        if not mine:
            return False
        for m in mine:
            if m.type not in ("image", "video", "emoji", "voice") or m.quote:
                return False
            if "（图片内容：" in m.text or "（语音内容：" in m.text:
                return False
        return True

    def _payload(self, chat: str, batch: list[Message]) -> dict:
        last = batch[-1]
        names = self.self_names()
        mine = [m for m in batch if m.sender == last.sender]
        bare = "\n".join(m.text for m in mine)          # @ 识别只看本人写的内容，不看被引用的
        text = "\n".join(self._render(m) for m in mine)
        at_me = (any(self.self_wxid in messages.at_users(m.source) for m in mine)
                 or any(f"@{n}" in bare for n in names)
                 or any(m.type == "pat" for m in mine))
        other_uids = [u for m in mine for u in messages.at_users(m.source) if u != self.self_wxid]
        self_lower = {n.lower() for n in names}
        masters = set(getattr(self, "masters", None) or [])
        master_names = {self.name_of(u).lower() for u in masters if u}
        text_ats = [m.group(1) for m in re.finditer(r"(?:^|[\s，,。！？!?])@([^\s@，,。！？!?\u2005]+)", bare)]
        other_text = [a for a in text_ats if a.lower() not in self_lower and not any(a.lower() in s or s in a.lower() for s in self_lower)]
        other_names = [self.name_of(u) for u in other_uids if u != "notify@all"] + other_text
        mentioned = list(dict.fromkeys([n for n in names if f"@{n}" in bare] + other_names))
        at_other = bool(other_uids or other_text)
        # 是否只 @ 了主人（群友报 bug 常直接 @ 开发者，主人一时没回时可由助手兜底接住）
        only_owner_uids = bool(other_uids) and all(u in masters for u in other_uids)
        only_owner_text = bool(other_text) and all(
            any(a.lower() in mn or mn in a.lower() for mn in master_names) for a in other_text
        ) if master_names else False
        at_owner_only = at_other and (only_owner_uids or (not other_uids and only_owner_text))
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
            "at_other": at_other,
            "at_owner_only": at_owner_only,
            "reply_to_bot": quote.get("sender") == self.self_wxid if quote else False,
            "reply_to_name": quote.get("name", ""),
            "reply_to_user_id": quote.get("sender", ""),
            "mentioned_names": mentioned,
            "self_names": names,
            "chat_title": self.name_of(chat) if last.is_group else "",
        }

    def _load_catchup(self) -> dict[str, dict]:
        path = getattr(self, "catchup_path", "")
        if not path or not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_catchup(self, data: dict[str, dict]) -> None:
        path = getattr(self, "catchup_path", "")
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as e:
            log.warning("保存重启补回进度失败：%s", e)

    def _needs_group_catchup(self, chat: str, chunk: list[Message]) -> bool:
        """群里重启补回只挑确实需要回复的消息（被 @ / 回复她 / 产品助手群里的提问或反馈），避免对旧闲聊刷屏。"""
        p = self._payload(chat, chunk)
        if self._is_blind_media(chunk) and not p["at_me"] and not p["reply_to_bot"]:
            return False
        if p["at_me"] or p["reply_to_bot"]:
            return True
        if not self.is_assistant_group(chat):
            return False
        masters = set(getattr(self, "masters", None) or [])
        sender = p.get("sender_id") or p.get("user_id") or ""
        if sender and sender in masters:
            return False
        if p.get("reply_to_user_id") and not p.get("reply_to_bot"):
            return False
        if p.get("at_other") and not p.get("at_owner_only"):
            return False
        q = p["text"]
        for n in p.get("self_names") or []:
            q = q.replace(f"@{n}", " ")
        q = q.strip()
        if len(q) < 2:
            return False
        if assist.FEEDBACK_RE.search(q) and len(q) >= 6:
            return True
        if p.get("at_owner_only"):
            return False
        return bool(knowledge.is_question(q) or assist.ISSUE_NUM_RE.search(q))

    def _boot_catchup(self) -> int:
        """重启后回看最近 boot_catchup_min 分钟内仍未回复的消息并补处理。
        - 私聊：bot 最后一条消息之后的对方发言；
        - 群聊：bot 与主人最后一条消息之后、且被 @ 或属于产品助手群提问/反馈的发言；
        - 按 (sort_seq, FUXI_BUILD) 去重，防止同版本反复重启对同一条已跳过消息重复打扰。
        """
        mins = int(getattr(self, "boot_catchup_min", 0) or 0)
        if mins <= 0 or self.paused or self.in_quiet_hours():
            return 0
        now = time.time()
        since = int(now - mins * 60)
        cur_build = os.environ.get("FUXI_BUILD", "dev")
        masters = set(getattr(self, "masters", None) or [])
        state = self._load_catchup()
        boot_list = list(getattr(self, "pending_boot", []))
        dispatched = 0
        state_dirty = False
        for s in self.reader.sessions(300):
            chat = s["username"]
            ts = s["last_timestamp"] or 0
            if ts < since or chat == "filehelper":
                continue
            if chat.startswith("gh_") or chat.endswith("@openim") or not self._allowed_chat(chat):
                continue
            try:
                hist = self.reader.history(chat, since)
            except Exception:
                log.exception("重启补回：读取 %s 历史失败", chat)
                continue
            if not hist:
                continue
            # 推进水位并从 pending_boot 移出，避免后续 poll_once 重复处理
            old_wm = self.reader.watermarks.get(chat, 0)
            if self.is_feedback_group(chat):
                unseen = [m for m in hist if m.sort_seq > old_wm]
                if unseen:
                    self._collect(unseen)
            if hist[-1].sort_seq > old_wm:
                self.reader.watermarks[chat] = hist[-1].sort_seq
                self.reader._save()
            if chat in boot_list:
                boot_list.remove(chat)
            gone = self._revoked(hist)
            is_group = chat.endswith("@chatroom")
            last_reply_seq = 0
            for m in hist:
                if m.msg_id in gone:
                    continue
                if m.is_self or (is_group and m.sender in masters):
                    last_reply_seq = max(last_reply_seq, m.sort_seq)
            names = self.self_names()
            trailing = [
                m for m in hist
                if m.sort_seq > last_reply_seq
                and m.msg_id not in gone
                and not m.is_self
                and not control.is_command(m, self.self_wxid, self.masters, names)
                and m.type in ACCEPT_TYPES
                and m.text.strip()
            ]
            if not trailing:
                continue
            last_seq = trailing[-1].sort_seq
            prev = state.get(chat) or {}
            if prev.get("seq", 0) >= last_seq and prev.get("build") == cur_build:
                continue
            batches = [c for c in self._split_by_sender(trailing) if self._needs_group_catchup(chat, c)] if is_group else [trailing]
            state[chat] = {"seq": last_seq, "build": cur_build, "at": now}
            state_dirty = True
            if not batches:
                continue
            self._chat_msg_seq[chat] = self._chat_msg_seq.get(chat, 0) + len(trailing)
            cur_seq = self._chat_msg_seq[chat]
            for idx, batch in enumerate(batches):
                seq_for_chunk = cur_seq - 1 if idx < len(batches) - 1 else cur_seq
                log.info("重启补回 %s：补处理 %d 条未回复消息（距今 %d 分钟）",
                         self.name_of(chat), len(batch), max(0, int((now - batch[-1].create_time) // 60)))
                self.pool.submit(self._dispatch, chat, batch, now, seq_for_chunk, True)
                dispatched += 1
        self.pending_boot = boot_list
        if state_dirty:
            self._save_catchup(state)
        return dispatched

    def _dispatch(self, chat: str, batch: list[Message], seen: float | None = None, seq_before: int | None = None,
                  boot_catchup: bool = False) -> None:
        if self.paused:
            log.info("已暂停，不处理 %s 的 %d 条消息", self.name_of(chat), len(batch))
            self._publish_batch(chat, batch, None, "已暂停")
            return
        tr = timing.Trace(batch[-1].create_time, seen or time.time())
        tr.add("settle", time.time() - tr.seen)
        try:
            self._dispatch_traced(chat, batch, tr, seq_before=seq_before, boot_catchup=boot_catchup)
        finally:
            if tr.bubbles:
                self.latency.add(tr.result())
                log.info("回复耗时 %s %s", self.name_of(chat), tr.summary())
            self._publish_batch(chat, batch, tr)

    def _publish_batch(self, chat: str, batch: list[Message], tr: "timing.Trace | None", reason: str = "") -> None:
        try:
            last = batch[-1]
            self.events.publish("message", {
                "chat": chat, "chat_name": self.name_of(chat), "group": last.is_group,
                "sender": self.name_of(last.sender) if last.is_group and last.sender else "",
                "count": len(batch), "kinds": sorted({m.type for m in batch}),
                "text": "\n".join(m.text for m in batch)[:2000], "msg_time": last.create_time,
                "replies": list(getattr(tr, "replies", [])), "reason": reason or getattr(tr, "reason", ""),
                "timing": tr.result() if tr is not None and tr.bubbles else None})
        except Exception:
            log.exception("发布对话流事件失败")

    def _dispatch_traced(self, chat: str, batch: list[Message], tr: "timing.Trace", seq_before: int | None = None,
                         boot_catchup: bool = False) -> None:
        with self.chat_locks[chat]:
            gone = self._revoked(batch)
            if gone:
                batch = [m for m in batch if m.msg_id not in gone]
                log.info("%s 撤回了 %d 条消息，不再回复它们", self.name_of(chat), len(gone))
                if not batch:
                    tr.reason = "对方撤回了"
                    return
            self._understand(chat, batch, tr)
            k0 = time.monotonic()
            payload = self._payload(chat, batch)
            if boot_catchup:
                payload["msg_id"] = payload["msg_id"] + ":catchup"
            followup_img = False
            if self._is_blind_media(batch):
                followup_img = (
                    payload["chat_type"] == "group"
                    and not payload["at_me"]
                    and not payload["reply_to_bot"]
                    and any(m.type == "image" for m in batch)
                    and hasattr(self, "_has_recent_followup")
                    and self._has_recent_followup(chat, payload)
                )
                if payload["chat_type"] == "group" and not payload["at_me"] and not payload["reply_to_bot"] and not followup_img:
                    log.info("跳过群里纯媒体（未看图/未听语音且未 @）：%s [%s]",
                             payload["chat_title"] or self.name_of(chat), payload["senderName"])
                    tr.reason = "群里纯媒体未看懂，跳过"
                    return
                if not followup_img:
                    payload["text"] += "\n（提示：未能看清/听清这段媒体的具体内容，不要凭空猜测，自然说明看不到或听不清即可）"
            if self._source_asked(payload):
                url = self.cfg.get("source_url")
                log.info("%s 问源码，回仓库地址", self.name_of(chat))
                self._deliver(chat, batch, {"action": "reply", "reply": f"我是开源的：{url}\n（AGPL-3.0，对话内核来自 whale_girl）"}, tr, k0)
                return
            agent = getattr(self, "owner_agent", None)
            if agent is not None and not boot_catchup:
                agent_reply = agent.maybe_handle(chat, payload, batch=batch)
                if agent_reply is not None:
                    ch = getattr(self, "_control_hashes", None)
                    if ch is not None:
                        ch.append(_h(agent_reply))
                    self._deliver(chat, batch, {"action": "reply", "reply": agent_reply}, tr, k0)
                    return
            if seq_before is not None:
                payload["_chat_seq"] = seq_before
            payload["_msg_ts"] = batch[-1].create_time
            if boot_catchup:
                payload["_boot_catchup"] = True
            if followup_img:
                payload["_followup_img"] = True
            self._assist(chat, payload)
            was_followup = bool(payload.pop("_followup_turn", False))
            payload.pop("_chat_seq", None)
            payload.pop("_msg_ts", None)
            payload.pop("_boot_catchup", None)
            payload.pop("_followup_img", None)
            log.info("→ 内核 %s [%s] %s", payload["chat_title"] or self.name_of(chat), payload["senderName"],
                     _brief(payload["text"]))
            try:
                resp = self.kernel.webhook(payload)
            except KernelError as e:
                log.error("内核 webhook 失败：%s", e)
                tr.reason = f"内核出错：{e}"
                return
            sticker = resp.get("sticker") or None   # 内核已为这次回复预留的表情包任务
            delivered = False
            try:
                delivered = self._deliver(chat, batch, resp, tr, k0)
                if delivered and payload.get("chat_type") == "group" and hasattr(self, "_note_group_reply"):
                    self._note_group_reply(chat, payload, resp, was_followup=was_followup)
            finally:
                if sticker:
                    self._send_sticker(chat, sticker, delivered)

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
        from .admin import serve as serve_admin

        threading.Thread(target=serve_admin, args=(self,), name="admin", daemon=True).start()
        threading.Thread(target=self.scheduler.loop, name="schedule", daemon=True).start()
        n_catchup = self._boot_catchup()
        if n_catchup:
            log.info("重启补回：已提交 %d 批未回复消息", n_catchup)
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
