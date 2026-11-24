"""语音：SILK → WAV、从 media 库按 server_id 取语音、转写结果附在「[语音]」后面。合成数据。"""

import collections
import io
import math
import os
import sqlite3
import struct
import tempfile
import unittest
import wave
from unittest import mock

import pysilk

from fuxi_bridge import timing, voice
from fuxi_bridge.kernel import KernelError
from fuxi_bridge.messages import Message


def tone_silk(secs=0.5) -> bytes:
    n = int(voice.RATE * secs)
    pcm = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / voice.RATE))) for i in range(n))
    out = io.BytesIO()
    pysilk.encode(io.BytesIO(pcm), out, voice.RATE, 24000, tencent=True)
    return out.getvalue()


class SilkTest(unittest.TestCase):
    def test_tencent_silk_to_wav(self):
        data = tone_silk(0.5)
        self.assertEqual(data[:1], b"\x02")
        with wave.open(io.BytesIO(voice.silk_to_wav(data))) as w:
            self.assertEqual((w.getframerate(), w.getnchannels()), (voice.RATE, 1))
            self.assertAlmostEqual(w.getnframes() / voice.RATE, 0.5, delta=0.1)

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            voice.silk_to_wav(b"\x02not silk at all")


class FakeCache:
    def __init__(self, root):
        self.db_root = root

    def connect(self, rel):
        con = sqlite3.connect(os.path.join(self.db_root, rel))
        con.row_factory = sqlite3.Row
        return con


class VoicesTest(unittest.TestCase):
    def test_lookup_by_server_id_and_chat(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "message"))
        con = sqlite3.connect(os.path.join(root, "message", "media_0.db"))
        con.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY)")
        con.execute("CREATE TABLE VoiceInfo(chat_name_id INTEGER, create_time INTEGER, local_id INTEGER, "
                    "svr_id INTEGER, voice_data BLOB, data_index TEXT DEFAULT '0')")
        con.execute("INSERT INTO Name2Id(user_name) VALUES ('wxid_a'), ('wxid_b')")
        silk = tone_silk(0.3)
        con.execute("INSERT INTO VoiceInfo VALUES (1, 0, 5, 777, ?, '0')", (silk,))
        con.execute("INSERT INTO VoiceInfo VALUES (2, 0, 6, 888, x'00', '0')")
        con.commit()
        con.close()
        v = voice.Voices(FakeCache(root))
        self.assertEqual(v.silk("wxid_a", 777), silk)
        self.assertEqual(v.silk("wxid_b", 777), b"")      # 同 svr_id 换个会话不算
        self.assertTrue(v.load("wxid_a", 777, wait=0).startswith(b"RIFF"))
        with self.assertRaises(ValueError):
            v.load("wxid_a", 999, wait=0)


def voice_msg(i, sender="wxid_a"):
    return Message(chat="wxid_a", local_id=i, sort_seq=i, type="voice", sender=sender, is_self=False,
                   create_time=0, text="[语音 3秒]", source="", shard="message_0.db", media=str(100 + i))


class UnderstandVoiceTest(unittest.TestCase):
    def bridge(self):
        from fuxi_bridge.app import Bridge
        b = object.__new__(Bridge)
        b.vision_mode, b.voice_mode = "off", "all"
        b.cfg = __import__("fuxi_bridge.settings").settings.Settings(os.path.join(tempfile.mkdtemp(), "s.json"), {})
        b.voices = mock.Mock()
        b.voices.load.return_value = b"RIFF" + b"\0" * 40
        b.kernel = mock.Mock()
        b.kernel.asr.return_value = "晚上一起吃饭吗"
        b.media_cache = collections.OrderedDict()
        b.name_of = lambda u: u
        return b

    def test_transcribes(self):
        b = self.bridge()
        batch = [voice_msg(1)]
        b._understand("wxid_a", batch, timing.Trace(0, 0))
        self.assertEqual(batch[0].text, "[语音 3秒]（语音内容：晚上一起吃饭吗）")
        b.voices.load.assert_called_once_with("wxid_a", 101)

    def test_failure_and_private_mode(self):
        b = self.bridge()
        b.kernel.asr.side_effect = KernelError("x")
        batch = [voice_msg(1)]
        b._understand("wxid_a", batch, timing.Trace(0, 0))
        self.assertEqual(batch[0].text, "[语音 3秒]")
        b.voice_mode = "private"
        b._understand("g@chatroom", [voice_msg(2)], timing.Trace(0, 0))
        self.assertEqual(b.voices.load.call_count, 1)


if __name__ == "__main__":
    unittest.main()
