"""解密库缓存：只在源文件变化时重新解密，所有输出放在工作目录（建议 tmpfs）。

- 主库（.db）只在微信 checkpoint 时改写 → 缓存「已解密主库」base，按 (size, mtime) 判断失效；
- WAL 追加新帧时：复制 base → view，再合并 WAL，秒级完成；
- 对外给出只读 sqlite 连接（view 文件）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading

from . import keyscan, sqlcipher
from .sqlcipher import DbKey

log = logging.getLogger(__name__)


def _stat(p: str):
    try:
        st = os.stat(p)
        return (st.st_size, st.st_mtime_ns)
    except FileNotFoundError:
        return None


def find_db_root(wx_config: str) -> str:
    """在微信配置目录下找当前账号的 db_storage（多个时取最近修改的）。"""
    found = []
    for root, dirs, _ in os.walk(wx_config):
        if "db_storage" in dirs:
            p = os.path.join(root, "db_storage")
            found.append((os.path.getmtime(os.path.join(p, "session")) if os.path.isdir(os.path.join(p, "session")) else 0, p))
            dirs.clear()
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("cache", "temp", "msg")]
    if not found:
        raise FileNotFoundError(f"{wx_config} 下没有 db_storage（微信未登录？）")
    return max(found)[1]


class KeyRing:
    """密钥：优先读 tmpfs 缓存，缺库或校验失败时重扫内存。"""

    def __init__(self, db_root: str, cache_path: str):
        self.db_root = db_root
        self.cache_path = cache_path
        self.keys: dict[str, DbKey] = {}
        self._load()

    def _page1(self, rel: str) -> bytes:
        with open(os.path.join(self.db_root, rel), "rb") as f:
            return f.read(sqlcipher.PAGE)

    def _load(self) -> None:
        try:
            with open(self.cache_path) as f:
                raw = json.load(f)
        except (FileNotFoundError, ValueError):
            return
        for rel, h in raw.items():
            try:
                k = DbKey.from_hex(h, self._page1(rel))
            except (OSError, ValueError):
                continue
            # 缓存的 key 也要过一遍 HMAC，微信重登录后 key 会变
            if sqlcipher.try_key(k.key32, self._page1(rel), k.salt if k.explicit_salt else None):
                self.keys[rel] = k

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.cache_path), mode=0o700, exist_ok=True)
        tmp = self.cache_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({r: k.hex() for r, k in self.keys.items()}, f)
        os.replace(tmp, self.cache_path)

    def get(self, rel: str) -> DbKey | None:
        if rel not in self.keys:
            self.rescan()
        return self.keys.get(rel)

    def invalidate(self, rel: str) -> None:
        self.keys.pop(rel, None)

    def rescan(self) -> None:
        pid = keyscan.find_wechat_pid()
        if not pid:
            log.error("找不到微信进程，无法提取密钥")
            return
        self.keys.update(keyscan.extract_keys(pid, self.db_root))
        self._save()


class DbCache:
    def __init__(self, db_root: str, workdir: str, keyring: KeyRing):
        self.db_root = db_root
        self.workdir = workdir
        self.keyring = keyring
        os.makedirs(workdir, mode=0o700, exist_ok=True)
        self._state: dict[str, tuple] = {}  # rel -> (db_stat, wal_stat)
        self._lock = threading.Lock()

    def _paths(self, rel: str):
        safe = rel.replace("/", "__")
        return (
            os.path.join(self.db_root, rel),
            os.path.join(self.workdir, safe + ".base"),
            os.path.join(self.workdir, safe),
        )

    def refresh(self, rel: str) -> bool:
        """必要时重建 view，返回是否有变化。"""
        src, base, view = self._paths(rel)
        wal = src + "-wal"
        with self._lock:
            cur = (_stat(src), _stat(wal))
            old = self._state.get(rel)
            if old == cur and os.path.exists(view):
                return False
            key = self.keyring.get(rel)
            if key is None:
                raise sqlcipher.DecryptError(f"没有 {rel} 的密钥")
            if old is None or old[0] != cur[0] or not os.path.exists(base):
                self._rebuild_base(src, base, key, rel)
            tmp = view + ".tmp"
            shutil.copyfile(base, tmp)
            wal_copy = tmp + "-walsrc"
            try:
                if cur[1]:
                    shutil.copyfile(wal, wal_copy)
                    try:
                        sqlcipher.merge_wal(tmp, wal_copy, key)
                    except sqlcipher.DecryptError:
                        self.keyring.invalidate(rel)
                        self._state.pop(rel, None)
                        raise
            finally:
                if os.path.exists(wal_copy):
                    os.remove(wal_copy)
            os.replace(tmp, view)
            # 复制期间主库被 checkpoint 改写 → 下一轮重来
            self._state[rel] = cur if _stat(src) == cur[0] else (None, None)
            return True

    def _rebuild_base(self, src: str, base: str, key: DbKey, rel: str) -> None:
        for _ in range(3):
            before = _stat(src)
            tmp_src = base + ".src"
            shutil.copyfile(src, tmp_src)
            try:
                if _stat(src) != before:
                    continue
                try:
                    sqlcipher.decrypt_file(tmp_src, base, key)
                except sqlcipher.DecryptError:
                    self.keyring.invalidate(rel)
                    raise
                return
            finally:
                if os.path.exists(tmp_src):
                    os.remove(tmp_src)
        raise sqlcipher.DecryptError(f"{rel} 持续变化，无法取得一致快照")

    def connect(self, rel: str) -> sqlite3.Connection:
        self.refresh(rel)
        _, _, view = self._paths(rel)
        con = sqlite3.connect(f"file:{view}?mode=ro&immutable=1", uri=True)
        con.row_factory = sqlite3.Row
        return con
