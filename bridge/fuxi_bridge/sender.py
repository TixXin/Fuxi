"""通过 X11 UI 自动化往微信窗口发消息（xdotool + xclip）。

微信跑在 wechat-selkies 容器的 Xvfb :1 上（-ac，无 X 认证）。桥接容器通过共享
/tmp/.X11-unix 卷连上同一个显示。中文用剪贴板粘贴，xdotool type 会丢字。

打开会话不用写死坐标：Ctrl+F 搜会话名 → 回车进入，窗口大小变了也不受影响。
所有发送串行执行（一个全局锁），避免两条消息交叉粘贴。
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time

log = logging.getLogger(__name__)


class SendError(Exception):
    pass


class Sender:
    def __init__(self, display: str = ":1", step_delay: float = 0.35):
        self.env = {**os.environ, "DISPLAY": display}
        self.delay = step_delay
        self._lock = threading.Lock()
        self._current_chat: str | None = None

    # ---- 底层 ----
    def _run(self, *args: str, stdin: bytes | None = None) -> str:
        r = subprocess.run(args, input=stdin, env=self.env, capture_output=True, timeout=15)
        if r.returncode != 0:
            raise SendError(f"{args[0]} 失败: {r.stderr.decode(errors='replace').strip()}")
        return r.stdout.decode(errors="replace").strip()

    def _key(self, *keys: str) -> None:
        self._run("xdotool", "key", "--clearmodifiers", *keys)
        time.sleep(self.delay)

    def _xclip(self, data: bytes, *extra: str) -> None:
        # xclip 会 fork 出后台进程持有剪贴板；它继承的 stdout/stderr 若是管道，
        # subprocess.run 会一直等到超时。所以输出必须丢到 DEVNULL。
        r = subprocess.run(
            ("xclip", "-selection", "clipboard", *extra),
            input=data, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )
        if r.returncode != 0:
            raise SendError("xclip 失败")

    def _clip_text(self, text: str) -> None:
        self._xclip(text.encode())

    def _clip_image(self, path: str) -> None:
        mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
        with open(path, "rb") as f:
            self._xclip(f.read(), "-t", mime)

    def window(self) -> str:
        """微信主窗口 id（可见、面积最大的那个）。"""
        out = self._run("xdotool", "search", "--onlyvisible", "--class", "wechat")
        ids = [w for w in out.split() if w]
        if not ids:
            raise SendError("找不到微信窗口（未登录或未启动？）")
        best, area = ids[0], -1
        for w in ids:
            try:
                geo = self._run("xdotool", "getwindowgeometry", w)
                wh = geo.rsplit("Geometry:", 1)[1].strip().split("x")
                a = int(wh[0]) * int(wh[1])
            except (SendError, IndexError, ValueError):
                continue
            if a > area:
                best, area = w, a
        return best

    def _activate(self) -> str:
        w = self.window()
        self._run("xdotool", "windowactivate", "--sync", w)
        time.sleep(self.delay)
        return w

    def _capture(self, w: str) -> "_Shot":
        r = subprocess.run(["xwd", "-id", w, "-silent"], env=self.env, capture_output=True, timeout=15)
        if r.returncode != 0:
            raise SendError("xwd 截图失败")
        return _Shot(r.stdout)

    def open_chat(self, name: str) -> None:
        """搜索并打开会话。name 用微信里显示的会话名（备注/群名/文件传输助手）。

        搜索下拉框顶部总是先列「搜索网络结果」和若干网络联想词，回车会走网络搜索，
        所以不能直接回车：截图找到第一个本地结果（功能/联系人/群聊，带大头像），点击它。
        找不到本地结果就放弃，绝不点到下拉框后面的会话列表。
        """
        w = self._activate()
        self._key("ctrl+f")
        self._clip_text(name)
        self._key("ctrl+a")
        self._key("ctrl+v")
        time.sleep(1.2)  # 等搜索结果
        hit = self._capture(w).first_local_result()
        if hit is None:
            self._key("Escape")
            raise SendError(f"搜索「{name}」没有本地会话结果")
        x, y = hit
        self._run("xdotool", "mousemove", "--window", w, str(x), str(y), "click", "1")
        time.sleep(0.6)
        self._current_chat = name

    # ---- 对外 ----
    def send_text(self, chat: str, text: str) -> None:
        with self._lock:
            self.open_chat(chat)
            self._clip_text(text)
            self._key("ctrl+v")
            self._key("Return")
            log.info("已发送文本到 %s（%d 字）", chat, len(text))

    def send_image(self, chat: str, path: str) -> None:
        with self._lock:
            self.open_chat(chat)
            self._clip_image(path)
            self._key("ctrl+v")
            time.sleep(0.6)
            self._key("Return")
            log.info("已发送图片到 %s：%s", chat, os.path.basename(path))

    def screenshot(self, out_xwd: str) -> None:
        """截取微信窗口（xwd 格式，调试用）。"""
        w = self.window()
        with open(out_xwd, "wb") as f:
            r = subprocess.run(["xwd", "-id", w, "-silent"], env=self.env, stdout=f, timeout=15)
        if r.returncode != 0:
            raise SendError("xwd 截图失败")


class _Shot:
    """xwd 截图（微信窗口，24 位深、每像素 3 字节 BGR）的最小解析与搜索结果定位。

    布局常量按微信 4.x Linux 默认 2 倍缩放实测：左侧导航 0~146，会话列表 146~600，
    搜索下拉框宽到约 786。下拉框里网络联想词只有小图标，本地结果是 ~72px 的大头像。
    """

    AVATAR_X = (168, 244)
    LIST_EDGE_X = 590     # 会话列表区（列表底色）
    DROPDOWN_ONLY_X = 700  # 下拉框伸出列表外的部分（白底）
    TOP_Y = 150

    def __init__(self, raw: bytes):
        import struct

        h = struct.unpack(">25I", raw[:100])
        self.w, self.h, self.bpp, self.bpl = h[4], h[5], h[11], h[12]
        if self.bpp != 24:
            raise SendError(f"不支持的截图格式 bpp={self.bpp}")
        self.off = h[0] + h[19] * 12
        self.raw = raw

    def px(self, x: int, y: int) -> tuple[int, int, int]:
        i = self.off + y * self.bpl + x * 3
        b = self.raw
        return (b[i + 2], b[i + 1], b[i])

    @staticmethod
    def _near(p, q, tol=6) -> bool:
        return all(abs(a - c) <= tol for a, c in zip(p, q))

    def dropdown_bottom(self) -> int:
        white = (250, 250, 250)
        run = 0
        for y in range(self.TOP_Y, self.h):
            inside_list_bg = not self._near(self.px(self.LIST_EDGE_X, y), white)
            dropdown_white = self._near(self.px(self.DROPDOWN_ONLY_X, y), white)
            run = run + 1 if (inside_list_bg and dropdown_white) else 0
            if run >= 10:
                return y - run
        return self.h

    def first_local_result(self) -> tuple[int, int] | None:
        """下拉框内第一个高 55~90px 的头像块（允许 8px 内的断开，头像里可能有白色图案）。"""
        bottom = self.dropdown_bottom()
        x0, x1 = self.AVATAR_X
        start = last = None
        for y in range(self.TOP_Y, bottom):
            row = [self.px(x, y) for x in range(x0, x1, 2)]
            bg = max(set(row), key=row.count)
            if not any(sum(abs(p[k] - bg[k]) for k in range(3)) > 40 for p in row):
                continue
            if last is not None and y - last > 8:  # 上一块结束
                if 55 <= last - start <= 90:
                    break
                start = None
            if start is None:
                start = y
            last = y
        if start is not None and 55 <= last - start <= 90:
            return (400, (start + last) // 2)
        return None
