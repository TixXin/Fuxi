"""收到的语音：从库里取 SILK → 解成 16 kHz WAV → 内核 /api/fuxi/asr 转文字。

微信 4 的语音不落文件，存在 message/media_*.db 的 VoiceInfo(chat_name_id, create_time, local_id,
svr_id, voice_data, data_index) 里；svr_id 对应消息行的 server_id，chat_name_id 指向同库的 Name2Id。
voice_data 是腾讯版 SILK v3（开头多一个 0x02），用 silk-python 解码。
"""

from __future__ import annotations

import io
import logging
import os
import re
import time
import wave

from .store import DbCache

log = logging.getLogger(__name__)

RATE = 16000


def silk_to_wav(data: bytes) -> bytes:
    import pysilk  # 延迟导入：只有转语音时才需要

    if data[:1] == b"\x02":
        data = data[1:]
    if not data.startswith(b"#!SILK_V3"):
        raise ValueError("不是 SILK 语音")
    pcm = io.BytesIO()
    try:
        pysilk.decode(io.BytesIO(data), pcm, RATE)
    except pysilk.SilkError as e:
        raise ValueError(f"SILK 解码失败：{e}") from e
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.getvalue())
    return out.getvalue()


class Voices:
    def __init__(self, cache: DbCache):
        self.cache = cache

    def _shards(self) -> list[str]:
        d = os.path.join(self.cache.db_root, "message")
        try:
            return sorted(f"message/{n}" for n in os.listdir(d) if re.fullmatch(r"media_\d+\.db", n))
        except OSError:
            return []

    def silk(self, chat: str, server_id: int) -> bytes:
        for shard in self._shards():
            con = self.cache.connect(shard)
            try:
                r = con.execute(
                    "SELECT v.voice_data FROM VoiceInfo v JOIN Name2Id n ON n.rowid = v.chat_name_id "
                    "WHERE v.svr_id = ? AND n.user_name = ?", (server_id, chat)).fetchone()
            finally:
                con.close()
            if r and r["voice_data"]:
                return bytes(r["voice_data"])
        return b""

    def load(self, chat: str, server_id: int, wait: float = 6.0) -> bytes:
        """等语音写进库（最多 wait 秒），返回 WAV；没有或解不开抛 ValueError。"""
        deadline = time.monotonic() + wait
        while True:
            data = self.silk(chat, server_id)
            if data or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        if not data:
            raise ValueError("库里没有这条语音")
        return silk_to_wav(data)
