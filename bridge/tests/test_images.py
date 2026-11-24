"""图片：V2 .dat 解密、uin 暴力推算、文件定位、看图失败不影响回复。全部用合成数据。"""

import hashlib
import os
import struct
import tempfile
import unittest
from unittest import mock

import cv2
import numpy as np
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from fuxi_bridge import images, timing
from fuxi_bridge.kernel import KernelError
from fuxi_bridge.messages import Message

WXID = "wxid_z"
UIN = 0x1234 << 8 | 0x5A


def jpeg(w=40, h=30) -> bytes:
    img = np.zeros((h, w, 3), np.uint8)
    img[:, :, 2] = 200
    return cv2.imencode(".jpg", img)[1].tobytes()


def make_v2(raw: bytes, uin: int = UIN, aes_n: int = 1024, xor_n: int = 64) -> bytes:
    key = images.aes_key(uin, WXID)
    aes_n = min(aes_n, len(raw))
    xor_n = min(xor_n, len(raw) - aes_n)
    p = padding.PKCS7(128).padder()
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    head = enc.update(p.update(raw[:aes_n]) + p.finalize()) + enc.finalize()
    mid = raw[aes_n:len(raw) - xor_n]
    tail = bytes(c ^ (uin & 0xFF) for c in raw[len(raw) - xor_n:])
    return images.V2_HEAD + struct.pack("<II", aes_n, xor_n) + b"\x01" + head + mid + tail


class DecryptTest(unittest.TestCase):
    def test_v2_roundtrip(self):
        raw = jpeg()
        for aes_n in (16, 1024):   # 恰好整除时 PKCS7 多一整块
            out = images.decrypt(make_v2(raw, aes_n=aes_n), images.aes_key(UIN, WXID), UIN & 0xFF)
            self.assertEqual(out, raw)

    def test_wrong_key_raises(self):
        with self.assertRaises(ValueError):
            images.decrypt(make_v2(jpeg()), images.aes_key(UIN + 256, WXID), UIN & 0xFF)

    def test_missing_key_raises(self):
        with self.assertRaises(ValueError):
            images.decrypt(make_v2(jpeg()), None, None)

    def test_old_xor_format(self):
        raw = jpeg()
        self.assertEqual(images.decrypt(bytes(c ^ 0x37 for c in raw), None, None), raw)

    def test_image_name(self):
        pi = b'\x08\x02\x10\x03\x1a&\x08x\x10x" ' + b"63985e810a3e339d1e9bd0f39cf3adf4" + b"X\x00"
        self.assertEqual(images.image_name(pi), "63985e810a3e339d1e9bd0f39cf3adf4")
        self.assertEqual(images.image_name(None), "")

    def test_to_jpeg_downscales_png(self):
        big = cv2.imencode(".png", np.zeros((100, 4000, 3), np.uint8))[1].tobytes()
        out = cv2.imdecode(np.frombuffer(images.to_jpeg(big), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(max(out.shape[:2]), images.MAX_SIDE)


class KeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.acct = self.tmp.name
        self.chat = "wxid_a"
        self.dir = os.path.join(self.acct, "msg", "attach", hashlib.md5(self.chat.encode()).hexdigest(), "2026-11", "Img")
        os.makedirs(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, name: str, data: bytes) -> None:
        with open(os.path.join(self.dir, name), "wb") as f:
            f.write(data)

    def test_derive_then_load(self):
        self.put("a" * 32 + "_t.dat", make_v2(jpeg(20, 20), aes_n=256))   # 太小的文件没有异或段，推不出
        self.put("b" * 32 + "_t.dat", make_v2(jpeg(30, 20), aes_n=256))
        state = os.path.join(self.acct, "imgkey.json")
        im = images.Images(self.acct, WXID, state)
        self.assertIsNone(im.uin)
        real = images.find_uin
        with mock.patch.object(images, "find_uin", lambda w, x, b: real(w, x, b, procs=2)):
            self.assertTrue(im.ensure_key())
        self.assertEqual(im.uin, UIN)
        self.assertEqual(images.Images(self.acct, WXID, state).uin, UIN)   # 已落盘
        out, src = im.load(self.chat, "b" * 32, wait=0)
        self.assertEqual(src, "t")
        self.assertEqual(out[:3], b"\xff\xd8\xff")

    def test_prefers_full_image(self):
        im = images.Images(self.acct, WXID, os.path.join(self.acct, "k.json"))
        im.uin = UIN
        png = cv2.imencode(".png", np.zeros((50, 60, 3), np.uint8))[1].tobytes()
        self.put("c" * 32 + "_t.dat", make_v2(jpeg(6, 5)))
        self.put("c" * 32 + ".dat", make_v2(png))
        out, src = im.load(self.chat, "c" * 32, wait=0)
        self.assertEqual(src, "full")
        self.assertEqual(cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR).shape[:2], (50, 60))

    def test_no_samples_no_key(self):
        im = images.Images(self.acct, WXID, os.path.join(self.acct, "k.json"))
        self.assertFalse(im.ensure_key())
        with self.assertRaises(ValueError):
            im.load(self.chat, "d" * 32, wait=0)


def img_msg(i: int, sender="wxid_a", media="e" * 32) -> Message:
    return Message(chat="wxid_a", local_id=i, sort_seq=i, type="image", sender=sender, is_self=False,
                   create_time=0, text="[图片]", source="", shard="message_0.db", media=media)


class SeeImagesTest(unittest.TestCase):
    def bridge(self):
        from fuxi_bridge.app import Bridge
        b = object.__new__(Bridge)
        b.vision_mode = "all"
        b.cfg = __import__("fuxi_bridge.settings").settings.Settings(os.path.join(tempfile.mkdtemp(), "s.json"), {})
        b.images = mock.Mock()
        b.images.load.return_value = (b"jpg", "full")
        b.kernel = mock.Mock()
        b.kernel.vision.return_value = "一只橘猫趴在键盘上"
        b.media_cache = __import__("collections").OrderedDict()
        b.voice_mode = "off"
        b.names_cache = {}
        b.name_of = lambda u: u
        return b

    def test_describes_and_caches(self):
        b = self.bridge()
        batch = [img_msg(1)]
        b._understand("wxid_a", batch, timing.Trace(0, 0))
        self.assertEqual(batch[0].text, "[图片]（图片内容：一只橘猫趴在键盘上）")
        again = [img_msg(2)]
        b._understand("wxid_a", again, timing.Trace(0, 0))
        self.assertEqual(b.kernel.vision.call_count, 1)
        self.assertIn("橘猫", again[0].text)

    def test_failure_keeps_placeholder(self):
        b = self.bridge()
        b.kernel.vision.side_effect = KernelError("x")
        batch = [img_msg(1)]
        b._understand("wxid_a", batch, timing.Trace(0, 0))
        self.assertEqual(batch[0].text, "[图片]")
        b.images.load.side_effect = ValueError("还没有图片密钥")
        b._understand("wxid_a", batch, timing.Trace(0, 0))
        self.assertEqual(batch[0].text, "[图片]")

    def test_modes_and_other_senders(self):
        b = self.bridge()
        b.vision_mode = "private"
        g = [img_msg(1)]
        b._understand("g@chatroom", g, timing.Trace(0, 0))
        b.vision_mode = "all"
        mixed = [img_msg(1, sender="wxid_b", media="f" * 32), img_msg(2, sender="wxid_a")]
        b._understand("wxid_a", mixed, timing.Trace(0, 0))
        self.assertEqual(b.images.load.call_count, 1)        # 只看最后发言人的图
        self.assertEqual(mixed[0].text, "[图片]")


if __name__ == "__main__":
    unittest.main()
