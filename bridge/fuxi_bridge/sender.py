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

    def _search(self, name: str) -> tuple[str, "_Shot"]:
        w = self._activate()
        self._key("ctrl+f")
        self._clip_text(name)
        self._key("ctrl+a")
        self._key("ctrl+v")
        time.sleep(1.2)  # 等搜索结果
        return w, self._capture(w)

    def locate(self, name: str) -> tuple[int, int] | None:
        """只搜索并定位，不点击（调试用）。"""
        with self._lock:
            _, shot = self._search(name)
            self._key("Escape")
            return shot.exact_match_row()

    def open_chat(self, name: str) -> None:
        """搜索并打开会话。name 必须是微信里显示的会话名（备注优先/群名/文件传输助手）。

        搜索下拉框的分区和顺序因关键词而异（网络联想、联系人、群聊「包含:xx」、聊天记录…），
        不能回车、也不能按位置猜。判定依据：微信把命中的关键词标绿，**只有标题与关键词
        完全相同的本地结果，整行标题才是纯绿色**。恰好一行这样的结果才点击，否则放弃。
        """
        w, shot = self._search(name)
        hit = shot.exact_match_row()
        if hit is None:
            self._key("Escape")
            raise SendError(f"搜索「{name}」没有唯一的完全匹配结果，放弃发送")
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
    """xwd 截图（微信窗口，每像素 3 或 4 字节、小端 BGR(A)）的最小解析与搜索结果定位。

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
        if self.bpp not in (24, 32):
            raise SendError(f"不支持的截图格式 bpp={self.bpp}")
        self.step = self.bpp // 8
        self.off = h[0] + h[19] * 12
        self.raw = raw

    def px(self, x: int, y: int) -> tuple[int, int, int]:
        i = self.off + y * self.bpl + x * self.step
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

    TITLE_X = (246, 700)   # 头像右侧到右侧 ⓘ 图标之前
    LOCAL_TITLE_MIN_X = 252  # 本地结果标题从 ~258 起；网络联想词从 ~214 起（会被扫描左界截断成 246）

    @staticmethod
    def _kind(p) -> str | None:
        r, g, b = p
        if g > r + 50 and g > b + 20:
            return "G"   # 关键词高亮绿
        if max(p) < 170 and max(p) - min(p) < 30:
            return "D"   # 普通黑/灰字
        return None

    def text_lines(self, bottom: int) -> list[dict]:
        x0, x1 = self.TITLE_X
        lines, cur = [], None
        for y in range(self.TOP_Y, bottom):
            hits = [(x, k) for x in range(x0, x1) if (k := self._kind(self.px(x, y)))]
            if hits:
                if cur is None:
                    cur = {"y0": y, "y1": y, "G": 0, "D": 0, "xmin": hits[0][0]}
                cur["y1"] = y
                cur["xmin"] = min(cur["xmin"], hits[0][0])
                for _, k in hits:
                    cur[k] += 1
            elif cur is not None and y - cur["y1"] > 3:
                lines.append(cur)
                cur = None
        if cur is not None:
            lines.append(cur)
        return lines

    def exact_match_row(self) -> tuple[int, int] | None:
        bottom = self.dropdown_bottom()
        exact = [
            l for l in self.text_lines(bottom)
            if l["xmin"] >= self.LOCAL_TITLE_MIN_X and 15 <= l["y1"] - l["y0"] <= 40
            and l["G"] >= 40 and l["D"] == 0
        ]
        if len(exact) != 1:
            if exact:
                log.warning("搜索结果里有 %d 个完全匹配，无法确定目标", len(exact))
            return None
        l = exact[0]
        return (400, (l["y0"] + l["y1"]) // 2)
