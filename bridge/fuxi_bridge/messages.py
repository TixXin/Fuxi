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
from dataclasses import asdict, dataclass

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

    def _rows(self, shard: str, chat: str, after: int):
        table = "Msg_" + hashlib.md5(chat.encode()).hexdigest()
        con = self.cache.connect(shard)
        try:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                return []
            names = dict(con.execute("SELECT rowid, user_name FROM Name2Id").fetchall())
            rows = con.execute(
                f'SELECT local_id, local_type, real_sender_id, create_time, message_content, '
                f'compress_content, source, sort_seq FROM "{table}" WHERE sort_seq > ? '
                f"ORDER BY sort_seq, local_id",
                (after,),
            ).fetchall()
            return [(r, names.get(r["real_sender_id"], "")) for r in rows]
        finally:
            con.close()

    def max_seq(self, chat: str) -> int:
        best = 0
        for shard in self.shards():
            for r, _ in self._rows(shard, chat, 0)[-1:]:
                best = max(best, r["sort_seq"])
        return best

    def poll(self, chat: str) -> list[Message]:
        """返回 chat 自上次水位以来的新消息；首次见到的会话从当前最新开始（不回放历史）。"""
        if chat not in self.watermarks:
            self.watermarks[chat] = self.max_seq(chat)
            self._save()
            return []
        after = self.watermarks[chat]
        out: list[Message] = []
        for shard in self.shards():
            for r, sender in self._rows(shard, chat, after):
                text = _decode(r["message_content"], r["compress_content"])
                if chat.endswith("@chatroom"):
                    m = _SENDER_PREFIX.match(text)
                    if m:
                        sender = sender or m.group(1)
                        text = text[m.end() :]
                mtype = MSG_TYPES.get(r["local_type"] & 0xFFFFFFFF, str(r["local_type"]))
                out.append(Message(
                    chat=chat, local_id=r["local_id"], sort_seq=r["sort_seq"], type=mtype,
                    sender=sender, is_self=(sender == self.self_wxid), create_time=r["create_time"],
                    text=text, source=_decode(r["source"], None), shard=os.path.basename(shard),
                ))
        out.sort(key=lambda m: (m.sort_seq, m.local_id))
        if out:
            self.watermarks[chat] = out[-1].sort_seq
            self._save()
        return out

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
