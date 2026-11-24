"""从解密后的库读取会话与新消息，按会话维护 sort_seq 水位（持久化）。

表结构（微信 4.x）：
- session/session.db   SessionTable(username, summary, last_timestamp, sort_timestamp, is_hidden, ...)
- message/message_N.db Msg_<md5(username)>(local_id, local_type, real_sender_id, create_time,
                        message_content, source, compress_content, server_id, sort_seq, ...)
                        Name2Id(rowid -> user_name)，real_sender_id 指向**同一分片**的 Name2Id
- contact/contact.db   contact(username, nick_name, remark, alias, ...)
字段与解码方式参考 whale_girl vendor/wechatauto/db.py（AGPL-3.0）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field

from .store import DbCache

log = logging.getLogger(__name__)

try:
    import zstandard

    _zstd = zstandard.ZstdDecompressor()
except ImportError:  # 宿主机调试时可能没有
    _zstd = None

MSG_TYPES = {
    1: "text", 3: "image", 34: "voice", 42: "card", 43: "video", 47: "emoji",
    48: "location", 49: "app", 50: "voip", 10000: "system", 10002: "revoke",
}
_SENDER_PREFIX = re.compile(r"^([A-Za-z][\w-]{3,}|[\w-]+@chatroom):\n")


@dataclass
class Message:
    chat: str          # 会话 username：wxid_xxx / xxx@chatroom / filehelper
    local_id: int
    sort_seq: int
    type: str
    sender: str        # 发送者 username
    is_self: bool
    create_time: int
    text: str
    source: str        # 原始 source XML（@ 列表等）
    shard: str
    # type 49 解析结果（见 parse_appmsg）：引用的原消息、拍一拍对象等；普通文本为空
    quote: dict = field(default_factory=dict)
    patted: str = ""

    @property
    def is_group(self) -> bool:
        return self.chat.endswith("@chatroom")

    @property
    def msg_id(self) -> str:
        return f"{self.chat}:{self.shard}:{self.local_id}"

    def to_dict(self) -> dict:
        return asdict(self) | {"is_group": self.is_group, "msg_id": self.msg_id}


def _decode(raw, compress) -> str:
    for blob in (raw, compress):
        if blob is None:
            continue
        if isinstance(blob, str):
            return blob
        if blob[:4] == b"\x28\xb5\x2f\xfd" and _zstd:
            try:
                return _zstd.decompress(blob, max_output_size=4 << 20).decode("utf-8", "replace")
            except zstandard.ZstdError:
                continue
        try:
            return blob.decode("utf-8").split("\x01", 1)[0]
        except UnicodeDecodeError:
            continue
    return ""


def self_wxid_from_root(db_root: str) -> str:
    """db_storage 的上一级目录名形如 wxid_xxx_abcd（末尾 4 位是客户端加的后缀）。"""
    name = os.path.basename(os.path.dirname(os.path.normpath(db_root)))
    m = re.match(r"^(.+)_[0-9a-f]{4}$", name)
    return m.group(1) if m else name


class MessageReader:
    def __init__(self, cache: DbCache, state_path: str, self_wxid: str):
        self.cache = cache
        self.state_path = state_path
        self.self_wxid = self_wxid
        self.watermarks: dict[str, int] = {}
        try:
            with open(state_path) as f:
                self.watermarks = json.load(f)
        except (FileNotFoundError, ValueError):
            pass

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.watermarks, f)
        os.replace(tmp, self.state_path)

    def shards(self) -> list[str]:
        d = os.path.join(self.cache.db_root, "message")
        return sorted(
            f"message/{n}" for n in os.listdir(d) if re.fullmatch(r"message_\d+\.db", n)
        )

    def _rows(self, shard: str, chat: str, after: int, min_time: int = 0):
        table = "Msg_" + hashlib.md5(chat.encode()).hexdigest()
        con = self.cache.connect(shard)
        try:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                return []
            names = dict(con.execute("SELECT rowid, user_name FROM Name2Id").fetchall())
            rows = con.execute(
                f'SELECT local_id, local_type, real_sender_id, create_time, message_content, '
                f'compress_content, source, sort_seq FROM "{table}" WHERE sort_seq > ? AND create_time >= ? '
                f"ORDER BY sort_seq, local_id",
                (after, min_time),
            ).fetchall()
            return [(r, names.get(r["real_sender_id"], "")) for r in rows]
        finally:
            con.close()

    def max_seq(self, chat: str) -> int:
        best = 0
        table = "Msg_" + hashlib.md5(chat.encode()).hexdigest()
        for shard in self.shards():
            con = self.cache.connect(shard)
            try:
                if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    best = max(best, con.execute(f'SELECT COALESCE(MAX(sort_seq),0) FROM "{table}"').fetchone()[0])
            finally:
                con.close()
        return best

    def poll(self, chat: str, since_time: int | None = None) -> list[Message]:
        """返回 chat 自上次水位以来的新消息。

        没有水位的会话：给了 since_time 就从该时刻（秒）起取，否则把水位设为当前最新、不回放历史。
        """
        min_time = 0
        if chat not in self.watermarks:
            if since_time is None:
                self.watermarks[chat] = self.max_seq(chat)
                self._save()
                return []
            self.watermarks[chat] = 0
            min_time = int(since_time)
        after = self.watermarks[chat]
        out: list[Message] = []
        for shard in self.shards():
            for r, sender in self._rows(shard, chat, after, min_time=min_time):
                text = _decode(r["message_content"], r["compress_content"])
                if chat.endswith("@chatroom"):
                    m = _SENDER_PREFIX.match(text)
                    if m:
                        sender = sender or m.group(1)
                        text = text[m.end() :]
                mtype = MSG_TYPES.get(r["local_type"] & 0xFFFFFFFF, str(r["local_type"]))
                extra: dict = {}
                if mtype == "app":
                    app = parse_appmsg(text)
                    if app:
                        mtype, text = app["kind"], app["text"]
                        extra = {"quote": app.get("quote") or {}, "patted": app.get("patted", "")}
                elif mtype in MEDIA_KINDS:
                    text = parse_media(mtype, text)
                out.append(Message(
                    chat=chat, local_id=r["local_id"], sort_seq=r["sort_seq"], type=mtype,
                    sender=sender, is_self=(sender == self.self_wxid), create_time=r["create_time"],
                    text=text, source=_decode(r["source"], None), shard=os.path.basename(shard), **extra,
                ))
        out.sort(key=lambda m: (m.sort_seq, m.local_id))
        if out:
            self.watermarks[chat] = out[-1].sort_seq
            self._save()
        return out

    def revoked(self, msgs: list[Message]) -> set[str]:
        """已撤回的消息（msg_id）。微信撤回是原地改写：那一行变成 type 10000 的 revokemsg；
        行不见了也当作撤回。读失败时返回空集（宁可照常回复）。"""
        out: set[str] = set()
        by_shard: dict[tuple[str, str], list[Message]] = {}
        for m in msgs:
            by_shard.setdefault((m.chat, m.shard), []).append(m)
        for (chat, shard), ms in by_shard.items():
            table = "Msg_" + hashlib.md5(chat.encode()).hexdigest()
            try:
                con = self.cache.connect(f"message/{shard}")
            except Exception:
                continue
            try:
                ids = [m.local_id for m in ms]
                rows = con.execute(
                    f'SELECT local_id, local_type, message_content, compress_content FROM "{table}" '
                    f"WHERE local_id IN ({','.join('?' * len(ids))})", ids).fetchall()
            except Exception:
                continue
            finally:
                con.close()
            now = {r["local_id"]: r for r in rows}
            for m in ms:
                r = now.get(m.local_id)
                if r is None or ((r["local_type"] & 0xFFFFFFFF) == 10000
                                 and "revokemsg" in _decode(r["message_content"], r["compress_content"])):
                    out.add(m.msg_id)
        return out

    def last_emoji(self, chat: str, since: int) -> str:
        """chat 里 since（秒）之后最新一条动画表情的原始 XML；没有返回空串。"""
        table = "Msg_" + hashlib.md5(chat.encode()).hexdigest()
        best: tuple[int, str] = (0, "")
        for shard in self.shards():
            con = self.cache.connect(shard)
            try:
                if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    continue
                r = con.execute(
                    f'SELECT sort_seq, message_content, compress_content FROM "{table}" '
                    "WHERE (local_type & 4294967295) = 47 AND create_time >= ? ORDER BY sort_seq DESC LIMIT 1",
                    (since,)).fetchone()
                if r and r["sort_seq"] > best[0]:
                    best = (r["sort_seq"], _decode(r["message_content"], r["compress_content"]))
            finally:
                con.close()
        return best[1]

    def sessions(self, limit: int = 200) -> list[dict]:
        con = self.cache.connect("session/session.db")
        try:
            rows = con.execute(
                "SELECT username, summary, last_timestamp FROM SessionTable "
                "WHERE is_hidden=0 ORDER BY sort_timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def display_name(self, username: str) -> str:
        if username == "filehelper":
            return "文件传输助手"
        con = self.cache.connect("contact/contact.db")
        try:
            r = con.execute(
                "SELECT remark, nick_name FROM contact WHERE username=?", (username,)
            ).fetchone()
            return (r and (r["remark"] or r["nick_name"])) or username
        finally:
            con.close()

    # ---- 联系人 / 群 ----
    def contacts(self) -> list[dict]:
        con = self.cache.connect("contact/contact.db")
        try:
            rows = con.execute("SELECT username, nick_name, remark, alias FROM contact").fetchall()
        finally:
            con.close()
        return [
            {"username": r["username"], "name": r["nick_name"] or "", "remark": r["remark"] or "",
             "alias": r["alias"] or "", "isGroup": (r["username"] or "").endswith("@chatroom")}
            for r in rows if r["username"]
        ]

    def find_chats(self, name: str) -> list[dict]:
        """按备注/昵称/微信号精确匹配会话（发送时用于把显示名解析成唯一 username）。"""
        if name in ("文件传输助手", "filehelper"):
            return [{"username": "filehelper", "name": "文件传输助手", "remark": "", "alias": "", "isGroup": False}]
        return [c for c in self.contacts() if name in (c["remark"], c["name"], c["alias"], c["username"])]

    def self_names(self) -> list[str]:
        con = self.cache.connect("contact/contact.db")
        try:
            r = con.execute("SELECT nick_name FROM contact WHERE username=?", (self.self_wxid,)).fetchone()
        finally:
            con.close()
        return [r["nick_name"]] if r and r["nick_name"] else []

    def self_sent_since(self, chat: str, since_ts: int) -> list[tuple[str, str]]:
        """chat 中自己在 since_ts（秒）之后发出的消息 [(type, text)]，不动水位。用于发送后核对。"""
        out = []
        for shard in self.shards():
            self.cache.refresh(shard)
            for r, sender in self._rows(shard, chat, 0, min_time=since_ts):
                if sender == self.self_wxid:
                    mtype = MSG_TYPES.get(r["local_type"] & 0xFFFFFFFF, str(r["local_type"]))
                    out.append((mtype, _decode(r["message_content"], r["compress_content"])))
        return out


_AT_LIST = re.compile(r"<atuserlist>\s*(?:<!\[CDATA\[)?([^<\]]*)")


def at_users(source: str) -> list[str]:
    m = _AT_LIST.search(source or "")
    return [u for u in (m.group(1).split(",") if m else []) if u]


# ---- type 49（appmsg）：引用、链接、文件、小程序、聊天记录、拍一拍… ----

# 交给内核的消息类型（其余 appmsg 如转账/红包/未知类型直接忽略）
APP_KINDS = ("quote", "link", "file", "miniapp", "record", "channels", "music", "pat", "share")
_PLACEHOLDER = {
    "1": None, "3": "[图片]", "34": "[语音]", "42": "[名片]", "43": "[视频]", "47": "[表情]",
    "48": "[位置]", "50": "[通话]",
}
_APP_KIND = {
    "57": "quote", "5": "link", "6": "file", "33": "miniapp", "36": "miniapp", "19": "record",
    "40": "record", "51": "channels", "3": "music", "76": "music", "62": "pat",
}
_SKIP_APP = {"2000", "2001", "2002", "2003"}  # 转账、红包：不交给模型


def _xml(text: str):
    i = text.find("<msg")
    if i < 0:
        return None
    try:
        return ET.fromstring(text[i:])
    except ET.ParseError:
        return None


def _t(el, path: str) -> str:
    return ((el.findtext(path) if el is not None else None) or "").strip()


def _quoted_text(ref) -> str:
    """refermsg 里被引用消息的可读内容。"""
    rtype = _t(ref, "type")
    content = _t(ref, "content")
    if rtype == "1":
        return _SENDER_PREFIX.sub("", content, count=1)
    if rtype == "49":
        inner = _xml(content)
        title = _t(inner, "appmsg/title") if inner is not None else ""
        return f"[分享] {title}" if title else "[分享]"
    return _PLACEHOLDER.get(rtype) or "[消息]"


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ---- 图片 / 语音 / 视频 / 表情 / 位置 / 名片：内核看不到内容，转成占位文字交给它 ----

MEDIA_KINDS = ("image", "voice", "video", "emoji", "location", "card")


def _attr(root, tag: str, name: str) -> str:
    el = root if root is not None and root.tag == tag else (root.find(tag) if root is not None else None)
    return ((el.get(name) if el is not None else None) or "").strip()


def _secs(v: str, ms: bool = False) -> str:
    if not v.isdigit() or int(v) <= 0:
        return ""
    n = round(int(v) / 1000) if ms else int(v)
    return f" {max(1, n)}秒"


def parse_media(mtype: str, text: str) -> str:
    """媒体消息的占位文字，例如「[语音 5秒]」「[位置] 西湖 杭州市西湖区」。XML 解析失败时退回纯占位。"""
    root = _xml(text)
    if mtype == "image":
        return "[图片]"
    if mtype == "voice":
        return f"[语音{_secs(_attr(root, 'voicemsg', 'voicelength'), ms=True)}]"
    if mtype == "video":
        return f"[视频{_secs(_attr(root, 'videomsg', 'playlength'))}]"
    if mtype == "emoji":
        return "[动画表情]"
    if mtype == "location":
        poi, label = _attr(root, "location", "poiname"), _attr(root, "location", "label")
        where = " ".join(x for x in (poi, label if label != poi else "") if x)
        return f"[位置] {where}".rstrip()
    if mtype == "card":
        nick = _attr(root, "msg", "nickname")
        return f"[名片] {nick}".rstrip()
    return f"[{mtype}]"


def emoji_url(xml: str) -> str:
    """动画表情原图的下载地址（cdnurl，明文）。自己从电脑端发的表情没有这个字段。"""
    root = _xml(xml)
    return _attr(root, "emoji", "cdnurl")


def parse_appmsg(text: str) -> dict | None:
    """把 appmsg XML 转成 {kind, text, quote?, patted?}；不认识或不该交给模型的返回 None。

    quote 的 text 只放回复正文（@ 识别只看它），被引用内容放在 quote 里，组装 payload 时再拼。
    """
    root = _xml(text)
    am = root.find("appmsg") if root is not None else None
    if am is None:
        return None
    atype = _t(am, "type")
    if atype in _SKIP_APP:
        return None
    kind = _APP_KIND.get(atype, "share")
    title, des, url = _t(am, "title"), _t(am, "des"), _t(am, "url")
    if kind == "quote":
        ref = am.find("refermsg")
        if not title:
            return None
        q = {}
        if ref is not None:
            q = {
                "sender": _t(ref, "chatusr") or _t(ref, "fromusr"),
                "name": _t(ref, "displayname"),
                "text": _clip(_quoted_text(ref), 200),
            }
        return {"kind": "quote", "text": title, "quote": q}
    if kind == "pat":
        patted = _t(am, "patinfo/pattedusername")
        return {"kind": "pat", "text": f"[拍一拍] {title}" if title else "[拍一拍]", "patted": patted}
    if kind == "file":
        size = _t(am, "appattach/totallen")
        kb = f"（{int(size) // 1024} KB）" if size.isdigit() else ""
        return {"kind": "file", "text": f"[文件] {title}{kb}"}
    if kind == "miniapp":
        src = _t(am, "sourcedisplayname")
        return {"kind": "miniapp", "text": f"[小程序{('·' + src) if src else ''}] {title}"}
    if kind == "record":
        return {"kind": "record", "text": f"[聊天记录] {title}\n{_clip(des, 300)}".rstrip()}
    if kind == "channels":
        desc = _t(am, "finderFeed/desc") or title
        nick = _t(am, "finderFeed/nickname")
        return {"kind": "channels", "text": f"[视频号{('·' + nick) if nick else ''}] {_clip(desc, 200)}"}
    if kind == "music":
        return {"kind": "music", "text": f"[音乐] {title}{(' - ' + des) if des else ''}"}
    if not title:
        return None
    parts = [f"[分享链接] {title}" if kind == "link" else f"[分享] {title}"]
    if des and des != title:
        parts.append(_clip(des, 200))
    if url.startswith(("http://", "https://")):
        parts.append(url)  # 内核会按 URL 抓取网页摘要
    return {"kind": kind, "text": "\n".join(parts)}
