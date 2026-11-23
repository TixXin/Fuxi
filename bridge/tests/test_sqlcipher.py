"""SQLCipher4 页格式与 WAL 合并：用合成数据自加密再解密，不依赖真实微信库。"""

import hashlib
import hmac
import os
import struct
import tempfile
import unittest

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from fuxi_bridge import sqlcipher as sc

PAGE, RES = sc.PAGE, sc.RESERVE


def encrypt_page(key32: bytes, salt: bytes, plain_usable: bytes, pgno: int) -> bytes:
    """按微信 4.x 格式加密一页。plain_usable：页 1 为 4000 字节（去掉 16 字节头），其余为 4016 字节。"""
    iv = os.urandom(16)
    enc = Cipher(algorithms.AES(key32), modes.CBC(iv)).encryptor()
    ct = enc.update(plain_usable) + enc.finalize()
    body = (salt + ct) if pgno == 1 else ct
    mac_key = sc._mac_key(key32, salt)
    start = 16 if pgno == 1 else 0
    tag = hmac.new(mac_key, (body + iv)[start:] + struct.pack("<I", pgno), hashlib.sha512).digest()
    page = body + iv + tag
    assert len(page) == PAGE
    return page


def make_db(key32: bytes, salt: bytes, n: int) -> tuple[bytes, list[bytes]]:
    plains, out = [], b""
    for pgno in range(1, n + 1):
        size = PAGE - RES - (16 if pgno == 1 else 0)
        p = (bytes([pgno]) * size) if pgno > 1 else (b"\x10\x00\x01\x01\x50" + b"\x00" * (size - 5))
        plains.append(p)
        out += encrypt_page(key32, salt, p, pgno)
    return out, plains


def wal(salt8: bytes, frames: list[tuple[int, int, bytes, bytes | None]]) -> bytes:
    """frames: (pgno, commit, page, 帧 salt 覆盖)"""
    hdr = struct.pack(">IIII", 0x377F0682, 3007000, PAGE, 1) + salt8 + b"\x00" * 8
    body = b""
    for pgno, commit, page, fsalt in frames:
        body += struct.pack(">II", pgno, commit) + (fsalt or salt8) + b"\x00" * 8 + page
    return hdr + body


class SqlcipherTest(unittest.TestCase):
    def setUp(self):
        self.key = os.urandom(32)
        self.salt = os.urandom(16)
        self.dir = tempfile.mkdtemp()

    def test_try_key(self):
        blob, _ = make_db(self.key, self.salt, 3)
        self.assertIsNotNone(sc.try_key(self.key, blob[:PAGE]))
        self.assertIsNone(sc.try_key(os.urandom(32), blob[:PAGE]))

    def test_decrypt_file_roundtrip(self):
        blob, plains = make_db(self.key, self.salt, 4)
        src, dst = os.path.join(self.dir, "a.db"), os.path.join(self.dir, "a.plain")
        open(src, "wb").write(blob)
        k = sc.try_key(self.key, blob[:PAGE])
        self.assertEqual(sc.decrypt_file(src, dst, k, verify=True), 4)
        out = open(dst, "rb").read()
        self.assertEqual(out[:16], sc.SQLITE_MAGIC)
        self.assertEqual(out[16:PAGE - RES], plains[0])
        for i in range(1, 4):
            self.assertEqual(out[i * PAGE:(i + 1) * PAGE - RES], plains[i])

    def test_decrypt_detects_tamper(self):
        blob, _ = make_db(self.key, self.salt, 2)
        bad = bytearray(blob)
        bad[PAGE + 100] ^= 1
        src = os.path.join(self.dir, "b.db")
        open(src, "wb").write(bytes(bad))
        k = sc.try_key(self.key, blob[:PAGE])
        with self.assertRaises(sc.DecryptError):
            sc.decrypt_file(src, os.path.join(self.dir, "b.plain"), k, verify=True)

    def _base(self, n=3):
        blob, plains = make_db(self.key, self.salt, n)
        src, dst = os.path.join(self.dir, "c.db"), os.path.join(self.dir, "c.plain")
        open(src, "wb").write(blob)
        k = sc.try_key(self.key, blob[:PAGE])
        sc.decrypt_file(src, dst, k)
        return k, dst

    def _page(self, pgno, fill):
        return encrypt_page(self.key, self.salt, bytes([fill]) * (PAGE - RES), pgno)

    def test_wal_applies_only_committed_frames(self):
        k, dst = self._base()
        salt8 = os.urandom(8)
        w = os.path.join(self.dir, "c.wal")
        open(w, "wb").write(wal(salt8, [
            (2, 0, self._page(2, 0xAA), None),
            (3, 3, self._page(3, 0xBB), None),   # 提交帧
            (2, 0, self._page(2, 0xCC), None),   # 未提交，不应生效
        ]))
        self.assertEqual(sc.merge_wal(dst, w, k), 2)
        out = open(dst, "rb").read()
        self.assertEqual(out[PAGE:PAGE + 4], b"\xaa" * 4)
        self.assertEqual(out[2 * PAGE:2 * PAGE + 4], b"\xbb" * 4)

    def test_wal_ignores_old_generation(self):
        k, dst = self._base()
        w = os.path.join(self.dir, "d.wal")
        open(w, "wb").write(wal(os.urandom(8), [(2, 2, self._page(2, 0xAA), os.urandom(8))]))
        self.assertEqual(sc.merge_wal(dst, w, k), 0)

    def test_wal_extends_page_count(self):
        k, dst = self._base(2)
        salt8 = os.urandom(8)
        w = os.path.join(self.dir, "e.wal")
        open(w, "wb").write(wal(salt8, [(4, 4, self._page(4, 0xDD), None)]))
        sc.merge_wal(dst, w, k)
        out = open(dst, "rb").read()
        self.assertEqual(struct.unpack(">I", out[28:32])[0], 4)
        self.assertEqual(out[3 * PAGE:3 * PAGE + 4], b"\xdd" * 4)


if __name__ == "__main__":
    unittest.main()
