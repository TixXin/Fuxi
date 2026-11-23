"""微信 4.x（WCDB / SQLCipher 4）数据库校验与解密，只读。

格式要点（已在 Linux 微信 4.1 上实测）：
- 页大小 4096，每页尾部 reserve=80：IV 16 字节 + HMAC-SHA512 64 字节。
- 加密用裸 32 字节 key（非口令派生），AES-256-CBC，IV 取该页 reserve 前 16 字节。
- 页 1 前 16 字节是 salt；mac_salt = salt XOR 0x3A；
  mac_key = PBKDF2-HMAC-SHA512(key, mac_salt, 2 次, 32 字节)；
  页 HMAC = HMAC-SHA512(mac_key, 页密文区 + IV + 小端页号)。
- 48 字节 key = 32 字节 key + 16 字节显式 salt（明文头库），页 1 前 16 字节是明文头。
- WAL：文件头 32 字节，帧 = 24 字节帧头（大端：页号、提交标记、salt、校验）+ 4096 加密页。

WAL 合并思路参考 whale_girl vendor/wechatauto/db.py（AGPL-3.0），本实现改为
只应用到最后一个提交帧为止，并逐帧做 HMAC 校验。

本模块永远不写微信原始文件：所有解密结果写到调用方给出的工作目录。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE = 4096
RESERVE = 80
SQLITE_MAGIC = b"SQLite format 3\x00"
WAL_HEADER = 32
WAL_FRAME_HDR = 24
WAL_FRAME = WAL_FRAME_HDR + PAGE


class DecryptError(Exception):
    pass


def _mac_key(key32: bytes, salt: bytes) -> bytes:
    mac_salt = bytes(b ^ 0x3A for b in salt)
    return hashlib.pbkdf2_hmac("sha512", key32, mac_salt, 2, 32)


def _page_hmac(mac_key: bytes, page: bytes, pgno: int) -> bytes:
    start = 16 if pgno == 1 else 0
    body = page[start : PAGE - RESERVE + 16]  # 密文区 + IV
    return hmac.new(mac_key, body + struct.pack("<I", pgno), hashlib.sha512).digest()


@dataclass(frozen=True)
class DbKey:
    """一个库的密钥：key32 用于 AES；salt 用于派生 HMAC key。"""

    key32: bytes
    salt: bytes
    explicit_salt: bool = False  # True = 48 字节变体

    @property
    def mac_key(self) -> bytes:
        return _mac_key(self.key32, self.salt)

    def hex(self) -> str:
        return (self.key32 + self.salt).hex() if self.explicit_salt else self.key32.hex()

    @classmethod
    def from_hex(cls, h: str, page1: bytes) -> "DbKey":
        raw = bytes.fromhex(h)
        if len(raw) == 48:
            return cls(raw[:32], raw[32:], True)
        return cls(raw, page1[:16], False)


def try_key(cand: bytes, page1: bytes, explicit_salt: bytes | None = None) -> DbKey | None:
    """用候选 key 校验页 1 的 HMAC，通过则返回 DbKey。

    先按标准形式（salt = 文件头 16 字节）校验，不过再试显式 salt；
    顺序不能反，否则标准库会被误存成 48 字节、解出非 SQLite。
    """
    if len(page1) < PAGE:
        return None
    tail = page1[PAGE - 64 :]
    std = DbKey(cand, page1[:16], False)
    if hmac.compare_digest(_page_hmac(std.mac_key, page1, 1), tail):
        return std
    if explicit_salt:
        alt = DbKey(cand, explicit_salt, True)
        if hmac.compare_digest(_page_hmac(alt.mac_key, page1, 1), tail):
            return alt
    return None


def is_encrypted(path: str) -> bool:
    with open(path, "rb") as f:
        head = f.read(PAGE)
    return len(head) == PAGE and not head.startswith(SQLITE_MAGIC)


def _aes(key32: bytes, iv: bytes, data: bytes) -> bytes:
    d = Cipher(algorithms.AES(key32), modes.CBC(iv)).decryptor()
    return d.update(data) + d.finalize()


def decrypt_page(key: DbKey, page: bytes, pgno: int) -> bytes:
    iv = page[PAGE - RESERVE : PAGE - RESERVE + 16]
    pad = b"\x00" * RESERVE
    if pgno == 1:
        plain = _aes(key.key32, iv, page[16 : PAGE - RESERVE])
        head = page[:16] if key.explicit_salt else SQLITE_MAGIC
        return head + plain + pad
    return _aes(key.key32, iv, page[: PAGE - RESERVE]) + pad


def decrypt_file(src: str, dst: str, key: DbKey, verify: bool = False) -> int:
    """整库解密到 dst，返回页数。verify=True 时逐页校验 HMAC。"""
    mk = key.mac_key if verify else None
    n = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            page = fin.read(PAGE)
            if not page:
                break
            n += 1
            if len(page) < PAGE:
                page += b"\x00" * (PAGE - len(page))
            if mk and not hmac.compare_digest(_page_hmac(mk, page, n), page[PAGE - 64 :]):
                raise DecryptError(f"page {n} HMAC mismatch")
            fout.write(decrypt_page(key, page, n))
    return n


def merge_wal(dst: str, wal_path: str, key: DbKey) -> int:
    """把 WAL 中当前世代、已提交的帧按页号覆盖进已解密库 dst，返回应用的帧数。

    - 只认 salt 与 WAL 头一致的帧（checkpoint 后旧世代帧会残留在文件里）；
    - 只应用到最后一个提交帧（提交标记 != 0）为止，避免读到写了一半的事务；
    - 每帧做 HMAC 校验，校验不过视为该帧及之后无效。
    """
    if not os.path.exists(wal_path) or os.path.getsize(wal_path) <= WAL_HEADER:
        return 0
    with open(wal_path, "rb") as f:
        blob = f.read()
    salt = blob[16:24]
    mk = key.mac_key
    frames: list[tuple[int, bytes]] = []
    committed = 0
    off = WAL_HEADER
    while off + WAL_FRAME <= len(blob):
        hdr = blob[off : off + WAL_FRAME_HDR]
        page = blob[off + WAL_FRAME_HDR : off + WAL_FRAME]
        off += WAL_FRAME
        if hdr[8:16] != salt:
            break
        pgno, commit = struct.unpack(">II", hdr[:8])
        if not hmac.compare_digest(_page_hmac(mk, page, pgno), page[PAGE - 64 :]):
            break
        frames.append((pgno, page))
        if commit:
            committed = len(frames)
    if not committed:
        return 0
    max_pgno = 0
    with open(dst, "r+b") as out:
        for pgno, page in frames[:committed]:
            out.seek((pgno - 1) * PAGE)
            out.write(decrypt_page(key, page, pgno))
            max_pgno = max(max_pgno, pgno)
        # 修正库头里的页数（偏移 28，大端）
        out.seek(28)
        hdr_pages = struct.unpack(">I", out.read(4))[0]
        real = max(hdr_pages, max_pgno, (out.seek(0, 2) + PAGE - 1) // PAGE)
        if real != hdr_pages:
            out.seek(28)
            out.write(struct.pack(">I", real))
    return committed


def snapshot(db_path: str, key: DbKey, out_path: str, retries: int = 3) -> int:
    """复制库与 WAL 的一致快照，解密并合并 WAL 到 out_path，返回合并帧数。

    微信可能在复制过程中 checkpoint；复制前后对比 (size, mtime)，变了就重试。
    最终仍不一致时退回「只用主库」，保证输出一定可读。
    """
    wal = db_path + "-wal"
    tmp_db = out_path + ".src"
    tmp_wal = out_path + ".src-wal"

    def stat(p: str):
        try:
            st = os.stat(p)
            return (st.st_size, st.st_mtime_ns)
        except FileNotFoundError:
            return None

    for _ in range(retries):
        before = (stat(db_path), stat(wal))
        shutil.copyfile(db_path, tmp_db)
        has_wal = before[1] is not None
        if has_wal:
            shutil.copyfile(wal, tmp_wal)
        if (stat(db_path), stat(wal)) == before:
            break
    else:
        has_wal = False  # 一直在变：放弃 WAL，只用主库

    try:
        decrypt_file(tmp_db, out_path, key)
        return merge_wal(out_path, tmp_wal, key) if has_wal else 0
    finally:
        for p in (tmp_db, tmp_wal):
            if os.path.exists(p):
                os.remove(p)
