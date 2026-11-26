"""通过 X11 UI 自动化往微信窗口发消息（xdotool + xclip）。

微信跑在 wechat-selkies 容器的 Xvfb :1 上（-ac，无 X 认证）。桥接容器通过共享
/tmp/.X11-unix 卷连上同一个显示。中文用剪贴板粘贴，xdotool type 会丢字。

打开会话不用写死坐标：Ctrl+F 搜会话名 → 回车进入，窗口大小变了也不受影响。
所有发送串行执行（一个全局锁），避免两条消息交叉粘贴。
"""

from __future__ import annotations

import collections
import ctypes
import contextlib
import logging
import os
import re
import subprocess
import threading
import time

import numpy as np

log = logging.getLogger(__name__)


class SendError(Exception):
    pass


class Sender:
    # 与微信容器共享的目录（两边路径相同）：表情包按「文件」粘贴，微信要能读到原文件。
    # 借用已共享的 X11 socket 目录，免得为此重建微信容器（重建要手机确认登录）。
    SHARE_DIR = os.environ.get("FUXI_SHARE_DIR", "/tmp/.X11-unix/fuxi")

    def __init__(self, display: str = ":1", step_delay: float = 0.3):
        self.env = {**os.environ, "DISPLAY": display}
        self.delay = step_delay
        self._lock = threading.Lock()
        self._current_chat: str | None = None
        self.open_times: collections.deque[tuple[float, float, str]] = collections.deque(maxlen=50)
        self.last_open = 0.0
        self.last_input = 0.0   # 自己最后一次往 X 里注入键盘鼠标的时间（monotonic），用来区分人的操作
        self.display = display

    # ---- 底层 ----
    def _run(self, *args: str, stdin: bytes | None = None) -> str:
        try:
            r = subprocess.run(args, input=stdin, env=self.env, capture_output=True, timeout=15)
        except subprocess.TimeoutExpired as e:
            raise SendError(f"{args[0]} 超时") from e
        finally:
            if args[0] == "xdotool" and len(args) > 1 and args[1] in INPUT_CMDS:
                self.last_input = time.monotonic()
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

    def _share(self, path: str) -> str:
        """把文件复制到共享目录，返回微信那边看到的路径；顺手清掉 10 分钟前的旧文件。"""
        import shutil
        import uuid

        os.makedirs(self.SHARE_DIR, exist_ok=True)
        now = time.time()
        for n in os.listdir(self.SHARE_DIR):
            f = os.path.join(self.SHARE_DIR, n)
            try:
                if now - os.path.getmtime(f) > 600:
                    os.remove(f)
            except OSError:
                pass
        ext = os.path.splitext(path)[1].lower() or ".gif"
        dst = os.path.join(self.SHARE_DIR, uuid.uuid4().hex[:12] + ext)
        shutil.copyfile(path, dst)
        os.chmod(dst, 0o644)
        return dst

    def _clip_file(self, shared: str) -> None:
        self._xclip(f"file://{shared}\r\n".encode(), "-t", "text/uri-list")

    def _windows(self) -> list[tuple[str, int, int]]:
        try:
            out = self._run("xdotool", "search", "--onlyvisible", "--class", "wechat")
        except SendError:
            return []
        res = []
        for w in out.split():
            try:
                geo = self._run("xdotool", "getwindowgeometry", w)
                ww, hh = geo.rsplit("Geometry:", 1)[1].strip().split("x")
                res.append((w, int(ww), int(hh)))
            except (SendError, IndexError, ValueError):
                continue
        return res

    def window(self) -> str:
        """微信主窗口 id。主窗口被隐藏到托盘时，先点托盘图标，不行再按微信的显示窗口快捷键 Ctrl+Alt+W。"""
        for attempt in range(3):
            wins = self._windows()
            main = [w for w in wins if w[1] >= 1000]
            if main:
                return max(main, key=lambda w: w[1] * w[2])[0]
            tray = [w for w in wins if w[1] <= 64 and w[2] <= 64]
            if attempt == 0 and tray:
                log.warning("微信主窗口不可见，点击托盘图标恢复")
                self._run("xdotool", "mousemove", "--window", tray[0][0], "12", "12", "click", "1")
                time.sleep(1.5)
                continue
            if attempt == 1 and tray:
                log.warning("点托盘图标没恢复，按 Ctrl+Alt+W")
                self._run("xdotool", "key", "--clearmodifiers", "ctrl+alt+w")
                time.sleep(1.5)
                continue
            break
        raise SendError("找不到微信主窗口（未登录、待确认或已退出）")

    def window_state(self) -> str:
        """ok = 主窗口可见；login = 只有登录/确认小窗；tray = 主窗口被隐藏到托盘；
        missing = 没有微信窗口。只读，不抢焦点。"""
        widths = [w[1] for w in self._windows()]
        if any(x >= 1000 for x in widths):
            return "ok"
        if any(x >= 300 for x in widths):
            return "login"
        return "tray" if widths else "missing"

    def _ocr_boxes(self, w: str) -> list[tuple[int, int, str, float]]:
        """对指定窗口截图并做 RapidOCR，返回 [(cx, cy, text, score), ...]。"""
        shot = self._capture(w)
        try:
            res, _ = _ocr()(shot.bgr, use_cls=False)
        except Exception as e:
            raise SendError(f"OCR 失败：{e}") from e
        out: list[tuple[int, int, str, float]] = []
        for box, txt, score in (res or []):
            s = str(txt or "").strip()
            if not s or float(score) < 0.7:
                continue
            cx = int(sum(p[0] for p in box) / 4)
            cy = int(sum(p[1] for p in box) / 4)
            out.append((cx, cy, s, float(score)))
        return out

    def _click_in(self, w: str, cx: int, cy: int) -> None:
        self._run("xdotool", "windowactivate", "--sync", w)
        time.sleep(0.1)
        self._run("xdotool", "mousemove", "--window", w, str(cx), str(cy), "click", "1")

    def auto_relogin(self) -> tuple[str, str]:
        """处理微信掉线弹窗（「为了你的账号安全，请重新登录」→ 点「我知道了」→ 点「登录」推送手机确认）。

        返回 (status, detail):
          ok / missing / waiting_phone / need_qr / clicked_login / dismissed_modal / unknown
        """
        modal_hints = ("提示", "账号安全", "重新登录", "已退出", "下线", "登录已过期", "登录状态", "异常")
        modal_btns = {"我知道了", "确定", "好的"}
        wait_hints = ("手机上确认", "请在手机", "等待确认", "取消登录", "已发送登录请求")
        qr_hints = ("扫一扫", "扫码登录", "二维码")
        login_btns = {"登录", "进入微信"}

        with self._lock:
            wins = self._windows()
            if any(x[1] >= 1000 for x in wins):
                return "ok", ""
            dialogs = [x for x in wins if x[1] >= 300]
            if not dialogs:
                return "missing", ""

            # 1) 优先检查并关闭盖在登录窗上面的提示弹窗（面积小的窗口优先）
            dismissed = False
            for w, ww, hh in sorted(dialogs, key=lambda x: x[1] * x[2]):
                try:
                    boxes = self._ocr_boxes(w)
                except SendError as e:
                    log.debug("自动重登 OCR 窗口 %s 失败：%s", w, e)
                    continue
                all_txt = " ".join(t for _, _, t, _ in boxes)
                if any(h in all_txt for h in modal_hints):
                    btn = next(((cx, cy, t) for cx, cy, t, _ in boxes if t.replace(" ", "") in modal_btns), None)
                    if btn:
                        cx, cy, t = btn
                        self._click_in(w, cx, cy)
                        log.warning("微信掉线弹窗（%dx%d）：识别到「%s」，已自动点击「%s」(%d,%d)",
                                    ww, hh, all_txt[:80], t, cx, cy)
                        dismissed = True
                        time.sleep(1.2)
                        break

            # 2) 重新枚举窗口，定位登录窗并判断是否可安全点击「登录」
            wins = self._windows()
            if any(x[1] >= 1000 for x in wins):
                return "ok", ""
            dialogs = [x for x in wins if x[1] >= 300]
            if not dialogs:
                return "missing", ""
            login_w, lww, lhh = max(dialogs, key=lambda x: x[1] * x[2])
            boxes = self._ocr_boxes(login_w)
            all_txt = " ".join(t for _, _, t, _ in boxes)
            if any(h in all_txt for h in wait_hints):
                return "waiting_phone", all_txt[:120]
            btn = next(((cx, cy, t) for cx, cy, t, _ in boxes if t.replace(" ", "") in login_btns), None)
            if btn:
                cx, cy, t = btn
                self._click_in(login_w, cx, cy)
                log.warning("微信待登录（%dx%d）：已自动点击「%s」(%d,%d) 推送手机确认", lww, lhh, t, cx, cy)
                time.sleep(1.5)
                try:
                    after = " ".join(x[2] for x in self._ocr_boxes(login_w))
                except SendError:
                    after = ""
                if any(h in after for h in qr_hints) and not any(h in after for h in wait_hints):
                    return "need_qr", after[:120]
                return "clicked_login", (after or all_txt)[:120]
            if any(h in all_txt for h in qr_hints):
                return "need_qr", all_txt[:120]
            return ("dismissed_modal" if dismissed else "unknown"), all_txt[:120]

    def _activate(self) -> str:
        w = self.window()
        try:
            active = self._run("xdotool", "getactivewindow")
        except SendError:
            active = ""
        if active != w:
            self._run("xdotool", "windowactivate", "--sync", w)
            time.sleep(0.1)
        return w

    def _capture(self, w: str) -> "_Shot":
        """截取窗口。xwd 偶发失败（窗口正被重绘/映射时 X 返回 BadMatch 等），重试两次。"""
        err = ""
        for attempt in range(3):
            if attempt:
                time.sleep(0.15)
            r = subprocess.run(["xwd", "-id", w, "-silent"], env=self.env, capture_output=True, timeout=15)
            if r.returncode == 0 and r.stdout:
                if attempt:
                    log.warning("xwd 第 %d 次重试成功（之前：%s）", attempt, err)
                return _Shot(r.stdout)
            err = r.stderr.decode(errors="replace").strip()[:200] or f"退出码 {r.returncode}"
        raise SendError(f"xwd 截图失败：{err}")

    def _type_search(self, name: str) -> None:
        """Ctrl+F → 全选 → 粘贴关键词，一次 xdotool 调用完成。"""
        self._clip_text(name)
        self._run("xdotool", "key", "--clearmodifiers", "--delay", "30", "ctrl+f", "ctrl+a", "ctrl+v")

    def _wait_results(self, w: str, max_wait: float = 3.0) -> tuple[int, int] | None:
        """轮询截图直到下拉框内容稳定，返回唯一完全匹配行的坐标。

        结果是分批渲染的（实测先出 1 行、约 0.6 秒内出全），中途某一帧的「唯一匹配」可能在
        后面的帧里变得不唯一，所以必须等内容连续 RESULT_STABLE 秒不变、且距粘贴至少
        RESULT_MIN 秒，才采信。
        """
        start = time.monotonic()
        prev, since = None, start
        while True:
            shot = self._capture(w)
            sig, rows = shot.search_state()
            now = time.monotonic()
            if sig != prev:
                prev, since = sig, now
            elif sig[1] and now - start >= self.RESULT_MIN and now - since >= self.RESULT_STABLE:
                return self._pick(shot, rows)
            if now - start > max_wait:
                log.warning("搜索结果 %.1f 秒内未稳定", max_wait)
                return None
            time.sleep(0.04)

    RESULT_MIN = 0.45
    RESULT_STABLE = 0.25
    # 可以点击的分区。「聊天记录」里也会出现标题与关键词完全相同的行（会话内容里提到了
    # 会话名），点它打开的是记录检索而非会话；「搜索网络结果」更不能点。
    SECTIONS_OK = ("联系人", "群聊", "功能", "最常使用")
    SECTIONS_ALL = SECTIONS_OK + ("聊天记录", "搜索网络结果", "网络结果", "公众号", "小程序", "文章", "视频号", "服务")

    def _pick(self, shot: "_Shot", rows: list[dict]) -> tuple[int, int] | None:
        """在完全匹配行里只保留可点击分区的，恰好一行才返回其坐标。"""
        ok = []
        for r in rows:
            sec = self._section_of(shot, r["y0"])
            if sec in self.SECTIONS_OK:
                ok.append(r)
            else:
                log.info("忽略「%s」分区里的完全匹配行", sec or "未知")
        if len(ok) != 1:
            if len(ok) > 1:
                log.warning("搜索结果里有 %d 个可点击的完全匹配，无法确定目标", len(ok))
            return None
        return (400, (ok[0]["y0"] + ok[0]["y1"]) // 2)

    def _section_of(self, shot: "_Shot", y: int) -> str:
        """从 y 往上找最近的分区标题（左侧小字，OCR 识别），返回分区名；找不到返回空串。"""
        for y0, y1 in shot.header_bands(y):
            crop = shot.bgr[max(0, y0 - 6):y1 + 6, 160:600]
            try:
                res, _ = _ocr()(np.ascontiguousarray(crop), use_det=False, use_cls=False)
            except Exception as e:
                raise SendError(f"OCR 失败：{e}") from e
            name = section_name(str(res[0][0]) if res else "", self.SECTIONS_ALL)
            if name:
                return name
        return ""

    def _search(self, name: str) -> tuple[str, tuple[int, int] | None]:
        w = self._activate()
        self._type_search(name)
        return w, self._wait_results(w)

    def locate(self, name: str) -> tuple[int, int] | None:
        """只搜索并定位，不点击（调试用）。"""
        with self._lock:
            _, hit = self._search(name)
            self._run("xdotool", "key", "Escape")
            return hit

    def _title_is(self, w: str, name: str, thorough: bool = False) -> tuple[bool, str]:
        title = self.read_title(w, thorough=thorough)
        return title_matches(name, title), title

    def open_chat(self, name: str) -> None:
        """搜索并打开会话。name 必须是微信里显示的会话名（备注优先/群名/文件传输助手）。

        搜索下拉框的分区和顺序因关键词而异（网络联想、联系人、群聊「包含:xx」、聊天记录…），
        不能回车、也不能按位置猜。判定依据：微信把命中的关键词标绿，**只有标题与关键词
        完全相同的本地结果，整行标题才是纯绿色**。恰好一行这样的结果才点击，否则放弃。

        提速（目标 2 秒内）：上一次就是由我们打开的同一会话且标题仍对 → 不再搜索；
        固定等待都换成轮询；OCR 只跑识别模型。
        """
        t0 = time.monotonic()
        w = self._activate()
        before_ok, _ = self._title_is(w, name)
        if before_ok and self._current_chat == name:
            self._record(t0, "已在会话")
            return
        self._current_chat = None
        self._type_search(name)
        hit = self._wait_results(w)
        if hit is None:
            self._run("xdotool", "key", "Escape")
            raise SendError(f"搜索「{name}」没有唯一的完全匹配结果，放弃发送")
        x, y = hit
        self._run("xdotool", "mousemove", "--window", w, str(x), str(y), "click", "1")
        # 粘贴前核对：OCR 聊天标题与目标名一致才继续。若点之前标题就已经是目标名，
        # 读到的可能是旧画面，先等界面切换完再核对。
        time.sleep(0.4 if before_ok else 0.12)
        deadline = time.monotonic() + 1.5
        while True:
            ok, title = self._title_is(w, name)
            if ok:
                break
            if time.monotonic() > deadline:
                ok, title = self._title_is(w, name, thorough=True)
                if ok:
                    break
                # 注意：此时焦点在聊天里，按 Escape 会把整个主窗口隐藏到托盘，所以什么都不按
                raise SendError(f"打开的会话标题是「{title}」，与目标「{name}」不符，放弃发送")
            time.sleep(0.1)
        self._current_chat = name
        self._record(t0, "搜索")

    def _record(self, t0: float, mode: str) -> None:
        dt = time.monotonic() - t0
        self.open_times.append((time.time(), dt, mode))
        self.last_open = dt
        log.info("进入会话耗时 %.2f 秒（%s）", dt, mode)

    def open_stats(self) -> dict:
        """最近进入会话的耗时统计（秒）。"""
        xs = sorted(d for _, d, m in self.open_times if m == "搜索")
        if not xs:
            return {"n": 0}
        return {"n": len(xs), "p50": round(xs[len(xs) // 2], 2), "max": round(xs[-1], 2),
                "last": round(self.open_times[-1][1], 2)}

    def read_title(self, w: str | None = None, thorough: bool = False) -> str:
        """OCR 当前聊天标题（RapidOCR / PP-OCR，对生僻字比 tesseract 准得多）。

        默认只把标题文字裁出来跑识别模型（约 0.02 秒）；thorough=True 跑完整的
        检测+识别（约 0.6 秒，标题里有表情等导致裁切不准时兜底）。
        """
        w = w or self.window()
        img = self._capture(w).title_bgr()
        try:
            if not thorough:
                crop = title_crop(img)
                if crop is not None:
                    res, _ = _ocr()(crop, use_det=False, use_cls=False)
                    if res and res[0][1] >= 0.8:
                        return str(res[0][0]).strip()
            res, _ = _ocr()(img, use_cls=False)
        except Exception as e:
            raise SendError(f"OCR 失败：{e}") from e
        boxes = sorted(res or [], key=lambda r: r[0][0][0])  # 按左上角 x 排序
        return "   ".join(b[1] for b in boxes).strip()

    # ---- 对外 ----
    def _open_or_abort(self, chat: str) -> None:
        """进入会话阶段（粘贴之前）出任何错都只放弃这一条：还没往任何会话输入内容，
        下一条会重新搜索并核对标题，不需要熔断。"""
        try:
            self.open_chat(chat)
        except SendError as e:
            if "放弃发送" in str(e):
                raise
            raise SendError(f"进入会话时出错（未粘贴任何内容）：{e}，放弃发送") from e

    def _focus_input(self) -> None:
        """点一下聊天输入框（窗口底部、聊天区中间），保证焦点在输入框里。
        标题已核对过，这里点的一定是目标会话的输入框。"""
        w = self.window()
        geo = self._run("xdotool", "getwindowgeometry", w)
        try:
            ww, hh = (int(v) for v in geo.rsplit("Geometry:", 1)[1].strip().split("x"))
        except (IndexError, ValueError) as e:
            raise SendError(f"读不到窗口大小：{geo!r}") from e
        # 2 倍缩放实测：输入区约 y∈[h-280, h-80]（再往下是工具栏），聊天区 x∈[610, w]
        x, y = (610 + ww) // 2, hh - 170
        self._run("xdotool", "mousemove", "--window", w, str(x), str(y), "click", "1")

    def _paste(self, clip) -> None:
        """进会话后、回车前：点输入框 → 写剪贴板 → 全选（替换掉可能的草稿）→ 粘贴。
        这一段出错时输入框里至多是没发出的草稿，不会发出任何东西，所以只放弃不熔断。"""
        try:
            self._focus_input()
            clip()
            self._run("xdotool", "key", "--clearmodifiers", "--delay", "40", "ctrl+a", "ctrl+v")
        except SendError as e:
            raise SendError(f"粘贴前出错（未发出）：{e}，放弃发送") from e

    # ---- 真 @ ----
    def _insert_mention(self, name: str) -> bool:
        """在（已聚焦、已清空的）输入框里打「@名字」，从成员弹窗里点选，成为真正的 @。

        只在弹窗过滤到恰好一行、且 OCR 出的名字与目标一致时才点；用点击不用回车，
        弹窗万一已关也不会把半句话发出去。返回 False 时调用方清空输入框改用文字 @。
        """
        # 过滤词只取第一个空格 / @ 之前：空格会关掉成员框，@ 会再弹一个。完整名字靠 OCR 核对
        query = re.split(r"[\s@]", name.strip(), maxsplit=1)[0]
        if len(_norm(query)) < 1:
            return False
        w = self.window()
        before = self._capture(w).bgr
        self._run("xdotool", "type", "--delay", "60", "@")
        time.sleep(0.35)
        self._clip_text(query)
        self._run("xdotool", "key", "--clearmodifiers", "ctrl+v")
        box, prev = None, None
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:     # 等弹窗过滤稳定
            time.sleep(0.3)
            after = self._capture(w).bgr
            cur = popup_box(before, after)
            if cur is not None and cur == prev:
                box = cur
                break
            prev = cur
        if box is None:
            log.info("真 @：没等到唯一的成员弹窗（%s），改用文字 @", name)
            return False
        x0, y0, x1, y1 = box
        if not (40 <= y1 - y0 <= 110):
            log.info("真 @：弹窗里不止一个候选（%s），改用文字 @", name)
            return False
        res, _ = _ocr()(np.ascontiguousarray(after[y0:y1, x0 + 60:x1]), use_cls=False)
        text = " ".join(r[1] for r in (res or []))
        if not title_matches(name, text):
            log.info("真 @：弹窗里是「%s」，不是「%s」，改用文字 @", text, name)
            return False
        self._run("xdotool", "mousemove", "--window", w, str((x0 + x1) // 2), str((y0 + y1) // 2), "click", "1")
        time.sleep(0.35)
        # 点完核对输入框：应当恰好是「@完整名字」，残留了过滤词等就算失败
        img = self._capture(w).bgr
        hh, ww = img.shape[:2]
        res, _ = _ocr()(np.ascontiguousarray(img[hh - 275:hh - 190, 630:ww - 30]), use_cls=False)
        got = "".join(r[1] for r in (res or []))
        if _norm(got) != _norm(name):
            log.info("真 @：点选后输入框是「%s」，不对，改用文字 @", got)
            return False
        return True

    def send_text_at(self, chat: str, name: str, text: str) -> bool:
        """群里 @name 并发送 text。返回是否用上了真 @（否则发的是「@name text」纯文字）。"""
        with self._lock, self._fresh_on_error():
            self._open_or_abort(chat)
            try:
                self._focus_input()
                self._run("xdotool", "key", "--clearmodifiers", "--delay", "40", "ctrl+a", "BackSpace")
                real = self._insert_mention(name)
                if real:
                    self._clip_text(text)
                    self._run("xdotool", "key", "--clearmodifiers", "ctrl+v")   # 不能全选，会盖掉 @
                else:
                    for _ in range(2):   # 第一下可能只是关掉弹窗（不能按 Esc：会把微信藏到托盘）
                        self._run("xdotool", "key", "--clearmodifiers", "--delay", "40", "ctrl+a", "BackSpace")
                        time.sleep(0.15)
                    self._clip_text(f"@{name} {text}")
                    self._run("xdotool", "key", "--clearmodifiers", "--delay", "40", "ctrl+a", "ctrl+v")
            except SendError as e:
                raise SendError(f"粘贴前出错（未发出）：{e}，放弃发送") from e
            time.sleep(self.delay)
            self._key("Return")
            log.info("已发送文本到 %s（%s@%s，%d 字）", chat, "" if real else "文字", name, len(text))
            return real

    def send_text(self, chat: str, text: str) -> None:
        with self._lock, self._fresh_on_error():
            self._open_or_abort(chat)
            self._paste(lambda: self._clip_text(text))
            time.sleep(self.delay)   # 粘贴是异步取剪贴板的，留一点时间再回车
            self._key("Return")
            log.info("已发送文本到 %s（%d 字）", chat, len(text))

    def send_image(self, chat: str, path: str) -> None:
        with self._lock, self._fresh_on_error():
            self._open_or_abort(chat)
            self._paste(lambda: self._clip_image(path))
            time.sleep(0.7)
            self._key("Return")
            log.info("已发送图片到 %s：%s", chat, os.path.basename(path))

    def send_sticker(self, chat: str, path: str) -> None:
        """发表情包：按文件粘贴，GIF 会原样作为动画表情发出（按图片粘贴会被转成静态图）。"""
        with self._lock, self._fresh_on_error():
            try:
                shared = self._share(path)
            except OSError as e:
                raise SendError(f"表情包文件准备失败（未发出）：{e}，放弃发送") from e
            self._open_or_abort(chat)
            self._paste(lambda: self._clip_file(shared))
            time.sleep(0.7)
            self._key("Return")
            log.info("已发送表情包到 %s：%s", chat, os.path.basename(path))

    def probe_open(self, chat: str) -> float:
        """只打开会话并核对标题、不发送，返回耗时（遥控 /测试 与调试用）。"""
        with self._lock, self._fresh_on_error():
            t0 = time.monotonic()
            self.open_chat(chat)
            return time.monotonic() - t0

    def human_idle(self) -> float | None:
        """人最后一次动键盘鼠标是多少秒前；bot 自己操作之后没人动过（或查不到）返回 None。

        X 的空闲时间（MIT-SCREEN-SAVER）会被 xdotool 注入的输入一起清零，所以只有比 bot 自己的
        最后一次输入更晚的那次操作才算人的。网页版微信（selkies）的操作也是注入到同一个 X 里。"""
        idle = x_idle_seconds(self.display)
        if idle is None:
            return None
        since_bot = time.monotonic() - self.last_input if self.last_input else float("inf")
        return idle if idle < since_bot - 1.0 else None

    def forget_chat(self) -> None:
        """界面可能被人动过（或发送结果存疑）时调用：下次发送必须重新搜索。"""
        self._current_chat = None

    @contextlib.contextmanager
    def _fresh_on_error(self):
        try:
            yield
        except BaseException:
            self._current_chat = None
            raise

    def screenshot(self, out_xwd: str) -> None:
        """截取微信窗口（xwd 格式，调试用）。"""
        w = self.window()
        with open(out_xwd, "wb") as f:
            r = subprocess.run(["xwd", "-id", w, "-silent"], env=self.env, stdout=f, timeout=15)
        if r.returncode != 0:
            raise SendError("xwd 截图失败")


INPUT_CMDS = {"key", "keydown", "keyup", "type", "mousemove", "click", "mousedown", "mouseup"}

_XSS = None


class _XssInfo(ctypes.Structure):
    _fields_ = [("window", ctypes.c_ulong), ("state", ctypes.c_int), ("kind", ctypes.c_int),
                ("til_or_since", ctypes.c_ulong), ("idle", ctypes.c_ulong), ("event_mask", ctypes.c_ulong)]


def x_idle_seconds(display: str) -> float | None:
    """X 显示的键盘鼠标空闲秒数；没装 libXss 或连不上显示时返回 None（当作没人在用）。"""
    global _XSS
    try:
        if _XSS is None:
            x11, xss = ctypes.cdll.LoadLibrary("libX11.so.6"), ctypes.cdll.LoadLibrary("libXss.so.1")
            x11.XOpenDisplay.restype = ctypes.c_void_p
            x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x11.XDefaultRootWindow.restype = ctypes.c_ulong
            x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
            xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(_XssInfo)
            xss.XScreenSaverQueryInfo.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_XssInfo)]
            _XSS = (x11, xss)
        x11, xss = _XSS
        d = x11.XOpenDisplay(display.encode())
        if not d:
            return None
        try:
            info = xss.XScreenSaverAllocInfo()
            if not xss.XScreenSaverQueryInfo(d, x11.XDefaultRootWindow(d), info):
                return None
            return info.contents.idle / 1000
        finally:
            x11.XCloseDisplay(d)
    except OSError:
        _XSS = False
        return None
    except TypeError:   # _XSS 是 False：之前加载失败过
        return None


_OCR = None


def _ocr():
    """RapidOCR 引擎单例（首次加载约 1 秒）。"""
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR = RapidOCR()
    return _OCR


def title_crop(img, gap: int = 60, pad: int = 8):
    """从标题栏截图里裁出标题文字（最左边一段深色文字，与右侧图标间隔 > gap 像素）。"""
    import numpy as np

    a = img.astype(np.int16)
    mx, mn = a.max(axis=2), a.min(axis=2)
    dark = (mx < 170) & (mx - mn < 30)
    cols = np.where(dark.any(axis=0))[0]
    if not len(cols):
        return None
    end = cols[-1]
    for i, g in enumerate(np.diff(cols)):
        if g > gap:
            end = cols[i]
            break
    x0, x1 = int(cols[0]), int(end) + 1
    rows = np.where(dark[:, x0:x1].any(axis=1))[0]
    y0, y1 = int(rows[0]), int(rows[-1]) + 1
    h, w = img.shape[:2]
    return np.ascontiguousarray(img[max(0, y0 - pad):min(h, y1 + pad), max(0, x0 - pad):min(w, x1 + pad)])


def popup_box(before, after, min_px: int = 4):
    """@ 成员弹窗的位置 (x0, y0, x1, y1)：比较打 @ 前后的截图，取输入框上方新出现的一块。

    弹窗贴着输入框上沿（输入区约 y ≥ h-280），宽 250~600 像素；变化区域不像弹窗
    （比如恰好来了新消息、整块聊天区在动）就返回 None，调用方会退回文字 @。
    """
    if before.shape != after.shape:
        return None
    h = before.shape[0]
    top = h - 282
    diff = (np.abs(before[70:top].astype(np.int16) - after[70:top].astype(np.int16)).max(axis=2) > 24)
    rows = np.where(diff.sum(axis=1) >= min_px)[0]
    cols = np.where(diff.sum(axis=0) >= min_px)[0]
    if not len(rows) or not len(cols):
        return None
    y0, y1 = int(rows[0]) + 70, int(rows[-1]) + 71
    x0, x1 = int(cols[0]), int(cols[-1]) + 1
    if top - y1 > 40 or not (250 <= x1 - x0 <= 600) or y1 - y0 > 700:
        return None
    return x0, y0, x1, y1


def section_name(ocr: str, names: tuple[str, ...]) -> str:
    """OCR 出的分区标题 → 分区名。左侧图标常被识别成 1~2 个杂字（如「群聊」→「君群聊」），
    所以允许分区名前后共有至多 3 个杂字；更长的文字是结果行（如网络联想「xx群聊」），不算。
    取最长的匹配（「搜索网络结果」优先于「网络结果」）。"""
    text = _norm(ocr)
    hits = [n for n in names if n in text[: len(n) + 2] and len(text) <= len(n) + 3]
    return max(hits, key=len) if hits else ""


_NON_WORD = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
_MEMBER_COUNT = re.compile(r"[(（]\s*\d+\s*[)）]")


def _norm(s: str) -> str:
    return _NON_WORD.sub("", s).lower()


def title_matches(target: str, ocr: str) -> bool:
    """OCR 出的聊天标题是否就是目标会话。

    标题右侧图标会被识别成噪声，先按 3 个以上空格切掉；群标题带「(成员数)」，去掉；
    只比较汉字/字母/数字（表情符号 OCR 不出来）。过长被截断（以 … / ... / .. 结尾）时按前缀比较。
    宁可误拒（不发），不可误放。
    """
    head = re.split(r"\s{3,}", ocr.strip())[0] if ocr.strip() else ""
    truncated = head.rstrip().endswith(("…", ".."))   # OCR 常把「…」读成 ... 或 ..
    head = _MEMBER_COUNT.sub("", head)
    t, h = _norm(target), _norm(head)
    if not t or not h:
        return False
    if t == h:
        return True
    return truncated and len(h) >= 4 and t.startswith(h)


class _Shot:
    """xwd 截图（微信窗口，每像素 3 或 4 字节、小端 BGR(A)）的最小解析与搜索结果定位。

    布局常量按微信 4.x Linux 默认 2 倍缩放实测：左侧导航 0~146，会话列表 146~600，
    搜索下拉框宽到约 786。下拉框里网络联想词只有小图标，本地结果是 ~72px 的大头像。
    像素运算用 numpy（整窗扫描从纯 Python 的 ~0.4 秒降到几毫秒）。
    """

    AVATAR_X = (168, 244)
    LIST_EDGE_X = 590     # 会话列表区（列表底色）
    DROPDOWN_ONLY_X = 700  # 下拉框伸出列表外的部分（白底）
    TOP_Y = 150

    def __init__(self, raw: bytes):
        import struct

        import numpy as np

        h = struct.unpack(">25I", raw[:100])
        self.w, self.h, self.bpp, self.bpl = h[4], h[5], h[11], h[12]
        if self.bpp not in (24, 32):
            raise SendError(f"不支持的截图格式 bpp={self.bpp}")
        self.step = self.bpp // 8
        self.off = h[0] + h[19] * 12
        self.raw = raw
        if len(raw) < self.off + self.h * self.bpl:
            raise SendError("截图数据不完整")
        rows = np.frombuffer(raw, dtype=np.uint8, count=self.h * self.bpl, offset=self.off).reshape(self.h, self.bpl)
        self.bgr = rows[:, : self.w * self.step].reshape(self.h, self.w, self.step)[:, :, :3]

    def px(self, x: int, y: int) -> tuple[int, int, int]:
        b, g, r = self.bgr[y, x]
        return (int(r), int(g), int(b))

    @staticmethod
    def _near(p, q, tol=6) -> bool:
        return all(abs(a - c) <= tol for a, c in zip(p, q))

    TITLE_BOX = (610, 80, 1400, 150)  # 聊天标题栏（窗口坐标，2 倍缩放）

    def title_bgr(self):
        """把聊天标题栏裁成 BGR 数组（numpy），喂给 OCR。"""
        import numpy as np

        x0, y0, x1, y1 = self.TITLE_BOX
        return np.ascontiguousarray(self.bgr[y0:min(y1, self.h), x0:min(x1, self.w)])

    def dropdown_bottom(self) -> int:
        import numpy as np

        def near_white(col):
            c = self.bgr[self.TOP_Y:, col].astype(np.int16)
            return (np.abs(c - 250) <= 6).all(axis=1)

        mask = ~near_white(self.LIST_EDGE_X) & near_white(self.DROPDOWN_ONLY_X)
        run = 0
        for i, v in enumerate(mask):
            run = run + 1 if v else 0
            if run >= 10:
                return self.TOP_Y + i - run
        return self.h

    TITLE_X = (246, 700)   # 头像右侧到右侧 ⓘ 图标之前
    LOCAL_TITLE_MIN_X = 252  # 本地结果标题从 ~258 起；网络联想词从 ~214 起（会被扫描左界截断成 246）

    def _kinds(self, bottom: int):
        """区域内每个像素的分类：G = 关键词高亮绿，D = 普通黑/灰字。"""
        import numpy as np

        x0, x1 = self.TITLE_X
        a = self.bgr[self.TOP_Y:bottom, x0:x1].astype(np.int16)
        b, g, r = a[:, :, 0], a[:, :, 1], a[:, :, 2]
        green = (g > r + 50) & (g > b + 20)
        mx, mn = a.max(axis=2), a.min(axis=2)
        dark = (mx < 170) & (mx - mn < 30) & ~green
        return green, dark

    def text_lines(self, bottom: int) -> list[dict]:
        import numpy as np

        if bottom <= self.TOP_Y:
            return []
        green, dark = self._kinds(bottom)
        hit = green | dark
        g_cnt, d_cnt, any_hit = green.sum(axis=1), dark.sum(axis=1), hit.any(axis=1)
        first_x = np.where(any_hit, hit.argmax(axis=1), 0) + self.TITLE_X[0]
        lines, cur = [], None
        for i in range(len(any_hit)):
            y = self.TOP_Y + i
            if any_hit[i]:
                if cur is None:
                    cur = {"y0": y, "y1": y, "G": 0, "D": 0, "xmin": int(first_x[i])}
                cur["y1"] = y
                cur["xmin"] = min(cur["xmin"], int(first_x[i]))
                cur["G"] += int(g_cnt[i])
                cur["D"] += int(d_cnt[i])
            elif cur is not None and y - cur["y1"] > 3:
                lines.append(cur)
                cur = None
        if cur is not None:
            lines.append(cur)
        return lines

    HEADER_X = (160, 246)  # 分区标题（「联系人」「聊天记录」…）从 ~170 起，在头像列里

    def header_bands(self, below: int) -> list[tuple[int, int]]:
        """y < below 范围内、头像列里矮的一段段深色文字（候选分区标题），由近到远。
        头像是 ~72px 的大方块，按高度排除；误判的由调用方 OCR 再筛。"""
        import numpy as np

        if below <= self.TOP_Y:
            return []
        x0, x1 = self.HEADER_X
        a = self.bgr[self.TOP_Y:below, x0:x1].astype(np.int16)
        mx, mn = a.max(axis=2), a.min(axis=2)
        rows = ((mx < 190) & (mx - mn < 30)).any(axis=1)
        bands, start, last = [], None, None
        for i, v in enumerate(rows):
            if v:
                start = i if start is None else start
                last = i
            elif start is not None and i - last > 3:
                bands.append((start, last))
                start = None
        if start is not None:
            bands.append((start, last))
        out = [(self.TOP_Y + a0, self.TOP_Y + a1 + 1) for a0, a1 in bands if 10 <= a1 - a0 + 1 <= 34]
        return out[::-1]

    def search_state(self) -> tuple[tuple, list[dict]]:
        """(下拉框内容签名, 完全匹配的标题行)。签名用于判断结果是否已加载稳定。"""
        bottom = self.dropdown_bottom()
        lines = self.text_lines(bottom)
        sig = (bottom, tuple((l["y0"], l["y1"], l["xmin"], l["G"] > 0, l["D"] > 0) for l in lines))
        exact = [
            l for l in lines
            if l["xmin"] >= self.LOCAL_TITLE_MIN_X and 15 <= l["y1"] - l["y0"] <= 40
            and l["G"] >= 40 and l["D"] == 0
        ]
        return sig, exact

    def exact_match_row(self) -> tuple[int, int] | None:
        """不分区地看：恰好一个完全匹配行时返回坐标（单元测试与调试用）。"""
        exact = self.search_state()[1]
        if len(exact) != 1:
            return None
        return (400, (exact[0]["y0"] + exact[0]["y1"]) // 2)
