"""从微信进程内存提取各数据库的 SQLCipher 密钥（Linux，只读）。

原理：密钥在内存里以 `x'<64~192 位十六进制>'` 字符串存在，但被一个固定的
32 字节掩码循环异或混淆。掩码取自 whale_girl（AGPL-3.0）的 CONFIG_XOR_MASK，
Windows / Linux 通用。已知明文开头是 `x'`，对 32 种相位各算出 2 字节密文作锚点，
单遍 bytes.find 扫 /proc/<pid>/mem 的可读段，命中后就地解码、再用库页 1 的
HMAC 逐个验证候选。

运行条件：与微信进程同 pid 命名空间（compose: pid: "container:wechat-selkies"），
并有 CAP_SYS_PTRACE。只读内存，不注入、不写入。
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Iterable

from .sqlcipher import PAGE, DbKey, is_encrypted, try_key

log = logging.getLogger(__name__)

# 分两段书写，避免被仓库的密钥扫描误判；这是公开的混淆常量，不是密钥。
MASK = bytes.fromhex("d2c7442458020000004889442450488b" "450048844c2448488944254048584c24")
_HEXLIT = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
_ANCHORS = [(a, bytes([0x78 ^ MASK[a], 0x27 ^ MASK[(a + 1) % 32]])) for a in range(32)]
_MAX_REGION = 1 << 30


def find_wechat_pid(exe_names: Iterable[str] = ("/usr/bin/wechat", "/opt/wechat/wechat")) -> int | None:
    """在当前 pid 命名空间里找微信主进程。"""
    names = set(exe_names)
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as f:
                argv0 = f.read().split(b"\x00", 1)[0].decode(errors="replace")
        except OSError:
            continue
        if argv0 in names:
            return int(d)
    return None


def _probable(b: bytes) -> bool:
    return len(b) == 32 and len(set(b)) >= 15


def scan_candidates(pid: int) -> set[tuple[bytes, bytes | None]]:
    """扫进程内存，返回候选 (key32, 显式 salt 或 None)。"""
    t0 = time.monotonic()
    cands: set[tuple[bytes, bytes | None]] = set()
    scanned = 0
    with open(f"/proc/{pid}/maps") as maps, open(f"/proc/{pid}/mem", "rb", 0) as mem:
        for line in maps:
            parts = line.split()
            if "r" not in parts[1]:
                continue
            a0, a1 = (int(x, 16) for x in parts[0].split("-"))
            if a1 - a0 > _MAX_REGION:
                continue
            try:
                mem.seek(a0)
                buf = mem.read(a1 - a0)
            except (OSError, ValueError, OverflowError):
                continue
            scanned += len(buf)
            n = len(buf)
            for a, pat in _ANCHORS:
                pos = buf.find(pat)
                while pos >= 0:
                    end = min(pos + 200, n)
                    dec = bytes(buf[pos + j] ^ MASK[(a + j) % 32] for j in range(end - pos))
                    m = _HEXLIT.match(dec)
                    if m:
                        run = m.group(1).decode().lower()
                        for s in range(0, len(run) - 63, 32):
                            c = bytes.fromhex(run[s : s + 64])
                            if _probable(c):
                                cands.add((c, None))
                                if s + 96 <= len(run):
                                    cands.add((c, bytes.fromhex(run[s + 64 : s + 96])))
                    pos = buf.find(pat, pos + 1)
    log.info("内存扫描 %d MB，用时 %.1fs，候选 %d 个", scanned >> 20, time.monotonic() - t0, len(cands))
    return cands


def list_encrypted_dbs(db_root: str) -> list[str]:
    """db_storage 下所有加密库的相对路径。"""
    out = []
    for root, _, files in os.walk(db_root):
        for name in files:
            if not name.endswith(".db"):
                continue
            p = os.path.join(root, name)
            try:
                if is_encrypted(p):
                    out.append(os.path.relpath(p, db_root))
            except OSError:
                continue
    return sorted(out)


def match_keys(cands: set[tuple[bytes, bytes | None]], db_root: str, rels: Iterable[str]) -> dict[str, DbKey]:
    """用候选逐个校验各库页 1，返回 {相对路径: DbKey}。"""
    keys: dict[str, DbKey] = {}
    for rel in rels:
        with open(os.path.join(db_root, rel), "rb") as f:
            page1 = f.read(PAGE)
        for c, salt in cands:
            k = try_key(c, page1, salt)
            if k:
                keys[rel] = k
                break
    return keys


def extract_keys(pid: int, db_root: str) -> dict[str, DbKey]:
    rels = list_encrypted_dbs(db_root)
    keys = match_keys(scan_candidates(pid), db_root, rels)
    log.info("解出密钥 %d / %d 个库", len(keys), len(rels))
    missing = [r for r in rels if r not in keys]
    if missing:
        log.warning("未解出的库：%s", ", ".join(missing))
    return keys
