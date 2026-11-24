"""收到的图片：找到 .dat → 解密 → 转成 JPEG，交给内核 /api/vision 看图。

微信 4 的图片在 <账号目录>/msg/attach/<md5(会话)>/<年-月>/Img/<名>[_t|_h].dat：
  <名>.dat 原图（多为 wxgf，也有 PNG）、_h.dat 高清（PNG）、_t.dat 缩略图（JPEG）。
<名> 在消息行的 packed_info_data 里（32 位十六进制）。原图要等微信下载完，缩略图几乎立刻就有。

V2 格式（头 07 08 'V2' 08 07）：
  [6 字节头][aes_n u32 LE][xor_n u32 LE][1 字节]
  然后 aes_n 字节明文按 AES-128-ECB + PKCS7 加密（密文长度 aes_n 向上取整到 16 的倍数，整除时多 16），
  中间一段是明文，最后 xor_n 字节逐字节异或一个单字节。
  AES 密钥 = md5(str(uin) + wxid) 的十六进制前 16 个字符，异或字节 = uin & 0xFF。
  uin 不在库里，用缩略图 JPEG 的结尾（FF D9）反推异或字节，再在 2^24 个候选里暴力找：
  能把两个不同文件的首块都解成完整图片头的才算（3 字节魔数会撞假阳性）。结果缓存进 state。
V1（头 'V1'）用固定密钥 cfcd208495d565ef；更老的无头格式整文件异或一个字节。

wxgf 是微信自己的 HEVC 封装：从第一个 00 00 00 01 起就是裸 HEVC 码流，OpenCV 自带的
FFmpeg 能解出第一帧。
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import multiprocessing as mp
import os
import re
import struct
import tempfile
import threading
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

log = logging.getLogger(__name__)

V1_HEAD = b"\x07\x08V1\x08\x07"
V2_HEAD = b"\x07\x08V2\x08\x07"
V1_KEY = b"cfcd208495d565ef"
_NAME = re.compile(rb"[0-9a-f]{32}")
MAX_SIDE = 1600


def image_name(packed_info: bytes | None) -> str:
    """消息行 packed_info_data 里的文件名（32 位十六进制）；没有返回空串。"""
    if not packed_info:
        return ""
    m = _NAME.search(bytes(packed_info))
    return m.group(0).decode() if m else ""


def is_full_header(d: bytes) -> bool:
    """解密出的首块像不像一张完整图片的开头（比 3 字节魔数严，避免暴力搜索撞假阳性）。"""
    return (d[:4] in (b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xdb", b"\xff\xd8\xff\xe2")
            or d[:8] == b"\x89PNG\r\n\x1a\n" or d[:6] in (b"GIF89a", b"GIF87a") or d[:4] == b"wxgf")


def aes_key(uin: int, wxid: str) -> bytes:
    return hashlib.md5(str(uin).encode() + wxid.encode()).hexdigest()[:16].encode()


def _aes_ecb(key: bytes, data: bytes) -> bytes:
    return Cipher(algorithms.AES(key), modes.ECB()).decryptor().update(data)


def decrypt(data: bytes, key: bytes | None, xor: int | None) -> bytes:
    """解出 .dat 里的原始图片字节；格式不认识或缺密钥时抛 ValueError。"""
    if data[:6] in (V1_HEAD, V2_HEAD):
        k = V1_KEY if data[:6] == V1_HEAD else key
        if k is None or xor is None:
            raise ValueError("还没有图片密钥")
        aes_n, xor_n = struct.unpack_from("<II", data, 6)
        body = data[15:]
        an = (aes_n // 16 + 1) * 16
        if an > len(body) or xor_n > len(body) - an:
            raise ValueError("长度字段不对")
        head = _aes_ecb(k, body[:an])
        pad = head[-1]
        if not 1 <= pad <= 16:
            raise ValueError("解密失败（密钥不对？）")
        tail = bytes(c ^ xor for c in body[len(body) - xor_n:]) if xor_n else b""
        return head[:-pad] + body[an:len(body) - xor_n] + tail
    # 老格式：整文件异或一个字节，从魔数反推
    for magic in (b"\xff\xd8\xff", b"\x89PN", b"GIF"):
        x = data[0] ^ magic[0]
        if all(data[i] ^ x == magic[i] for i in range(3)):
            return bytes(c ^ x for c in data)
    raise ValueError("不认识的图片格式")


def to_jpeg(raw: bytes) -> bytes:
    """任意格式（wxgf/PNG/JPEG/GIF）→ 长边不超过 MAX_SIDE 的 JPEG。"""
    import cv2  # 延迟导入：只有看图时才需要
    import numpy as np

    if raw[:4] == b"wxgf":
        i = raw.find(b"\x00\x00\x00\x01")
        if i < 0:
            raise ValueError("wxgf 里没有 HEVC 码流")
        fd, path = tempfile.mkstemp(suffix=".hevc")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw[i:])
            cap = cv2.VideoCapture(path)
            ok, img = cap.read()
            cap.release()
        finally:
            os.unlink(path)
        if not ok:
            raise ValueError("wxgf 解码失败")
    else:
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("图片解码失败")
    h, w = img.shape[:2]
    if max(h, w) > MAX_SIDE:
        s = MAX_SIDE / max(h, w)
        img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise ValueError("JPEG 编码失败")
    return buf.tobytes()


# ---- 暴力找 uin（子进程里跑） ----
def _search(args: tuple[int, int, int, str, list[bytes]]) -> int | None:
    start, stop, xor, wxid, blocks = args
    w = wxid.encode()
    for mid in range(start, stop):
        uin = (mid << 8) | xor
        k = hashlib.md5(str(uin).encode() + w).hexdigest()[:16].encode()
        if is_full_header(_aes_ecb(k, blocks[0])) and all(is_full_header(_aes_ecb(k, b)) for b in blocks[1:]):
            return uin
    return None


def find_uin(wxid: str, xor: int, blocks: list[bytes], procs: int | None = None) -> int | None:
    procs = procs or max(1, min(os.cpu_count() or 1, 16))
    step = 1 << 14
    jobs = [(s, s + step, xor, wxid, blocks) for s in range(0, 1 << 24, step)]
    with mp.get_context("spawn").Pool(procs) as pool:
        for r in pool.imap_unordered(_search, jobs):
            if r is not None:
                pool.terminate()
                return r
    return None


class Images:
    def __init__(self, account_dir: str, wxid: str, state_path: str):
        self.attach = os.path.join(account_dir, "msg", "attach")
        self.wxid = wxid
        self.path = state_path
        self.uin: int | None = None
        self._deriving = threading.Lock()
        self._failed_at = 0.0
        try:
            with open(state_path) as f:
                self.uin = int(json.load(f)["uin"])
        except (OSError, ValueError, KeyError, TypeError):
            pass

    @property
    def key(self) -> bytes | None:
        return aes_key(self.uin, self.wxid) if self.uin is not None else None

    @property
    def xor(self) -> int | None:
        return self.uin & 0xFF if self.uin is not None else None

    def _samples(self) -> tuple[int | None, list[bytes]]:
        """从现有的 V2 缩略图推异或字节，并取几个文件的首个密文块做验证。"""
        xor = None
        blocks: list[bytes] = []
        files = sorted(glob.glob(os.path.join(self.attach, "*", "*", "Img", "*.dat")),
                       key=os.path.getmtime, reverse=True)[:200]
        for f in files:
            try:
                with open(f, "rb") as fh:
                    d = fh.read()
            except OSError:
                continue
            if d[:6] != V2_HEAD or len(d) < 31:
                continue
            xor_n = struct.unpack_from("<I", d, 10)[0]
            if xor is None and xor_n >= 2 and f.endswith("_t.dat"):
                x = d[-1] ^ 0xD9
                if d[-2] ^ x == 0xFF:
                    xor = x
            if len(blocks) < 3:
                blocks.append(d[15:31])
        return xor, blocks

    def ensure_key(self) -> bool:
        """有密钥返回 True；没有就（最多每 10 分钟一次）现场推一遍。"""
        if self.uin is not None:
            return True
        if time.time() - self._failed_at < 600 or not self._deriving.acquire(blocking=False):
            return False
        try:
            t0 = time.monotonic()
            xor, blocks = self._samples()
            if xor is None or len(blocks) < 2:
                log.info("图片密钥：样本不够（需要至少两张收到的图片），稍后再试")
                self._failed_at = time.time()
                return False
            uin = find_uin(self.wxid, xor, blocks)
            if uin is None:
                log.warning("图片密钥：没找到（%.0f 秒）", time.monotonic() - t0)
                self._failed_at = time.time()
                return False
            self.uin = uin
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"uin": uin}, f)
            os.replace(tmp, self.path)
            log.info("图片密钥已推出并保存（%.1f 秒）", time.monotonic() - t0)
            return True
        finally:
            self._deriving.release()

    def locate(self, chat: str, name: str) -> dict[str, str]:
        """{'full'|'h'|'t': 路径}，只列已存在的。"""
        d = os.path.join(self.attach, hashlib.md5(chat.encode()).hexdigest())
        out = {}
        for tag, suffix in (("full", ""), ("h", "_h"), ("t", "_t")):
            hits = glob.glob(os.path.join(d, "*", "Img", f"{name}{suffix}.dat"))
            if hits:
                out[tag] = max(hits, key=os.path.getmtime)
        return out

    def load(self, chat: str, name: str, wait: float = 3.5) -> tuple[bytes, str]:
        """等原图/高清下好（最多 wait 秒，否则用缩略图），返回 (JPEG, 来源 full|h|t)。"""
        if not self.ensure_key():
            raise ValueError("还没有图片密钥")
        deadline = time.monotonic() + wait
        while True:
            found = self.locate(chat, name)
            if "full" in found or "h" in found or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        for tag in ("full", "h", "t"):
            if tag not in found:
                continue
            try:
                with open(found[tag], "rb") as f:
                    raw = decrypt(f.read(), self.key, self.xor)
                return to_jpeg(raw), tag
            except (OSError, ValueError) as e:
                log.info("图片 %s（%s）解不开：%s", name[:8], tag, e)
        raise ValueError("没找到可用的图片文件")
