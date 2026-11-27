"""浮屿用户群二维码自动换（微信群码 7 天过期，浮屿仓库 main 上的码要跟着换）。

微信每次打开「群二维码」都给一条新链接，有效期是打开那天往后 7 天（「12月3日前有效」）。所以每晚到点
（设置「换码时间」，默认 00:00）换一次，仓库里的码一直还剩 6 天以上；到点没换成每 30 分钟再试。
「换码时间」留空时只在仓库里的码到了最后有效那天才换。

换一次：拿发送锁 → 打开群 → 右上角「…」→ 群信息面板里找到「群二维码」点开 → 截二维码小窗解码
  → 新码有效期比仓库里的晚才换：裁出二维码（裁出来解不对就用链接重新生成），README 那张再加上群名和有效期
  → 建分支、开 PR、用 token 主人的管理员身份直接合并（不等 CI；浮屿 ci.yml 对只改这几个文件的改动不跑）。

只点 OCR 认出来、文字完全等于「群二维码」的那一行（同一个面板里还有「退出群聊」「清空聊天记录」）。
整个界面操作都拿着 Sender 的锁，不会和发消息抢界面；有人在网页版微信里操作时不动手。
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

import numpy as np

from .sender import SendError, _ocr, _Shot

if TYPE_CHECKING:
    from .app import Bridge

log = logging.getLogger("fuxi.groupqr")

COMMUNITY_DIR = "docs/assets/community"
COMMUNITY_JSON = COMMUNITY_DIR + "/community.json"
README_QR = "docs/assets/readme/wechat-group.png"
URL_PREFIX = "https://weixin.qq.com/g/"
RETRY_EVERY = 1800
CHECK_EVERY = 6 * 3600
QR_SIZE = 480
FONT = "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"
_TIME = re.compile(r"^([01]?\d|2[0-4]):([0-5]\d)$")
_EXPIRY = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*前")


class QrError(Exception):
    pass


# ---------- 纯函数（有单元测试） ----------
def decode(img) -> tuple[str, np.ndarray | None]:
    """从截图里找微信群码，返回 (链接, 四个角点)；找不到返回 ("", None)。先 zxing，再 OpenCV。"""
    try:
        import zxingcpp

        for r in zxingcpp.read_barcodes(img):
            if r.text.startswith(URL_PREFIX):
                p = r.position
                pts = np.array([[p.top_left.x, p.top_left.y], [p.top_right.x, p.top_right.y],
                                [p.bottom_right.x, p.bottom_right.y], [p.bottom_left.x, p.bottom_left.y]], dtype=float)
                return r.text, pts
    except ImportError:
        pass
    import cv2

    det = cv2.QRCodeDetector()
    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    for src in (gray, 255 - gray):
        try:
            text, pts, _ = det.detectAndDecode(src)
        except cv2.error:
            continue
        if text and text.startswith(URL_PREFIX) and pts is not None:
            return text, pts.reshape(-1, 2).astype(float)
    return "", None


def crop_qr(img, pts) -> np.ndarray:
    """按角点裁出二维码（四周留一圈白边），转成白底黑码、QR_SIZE 见方的灰度图。"""
    import cv2

    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    x0, y0 = pts.min(axis=0)
    x1, y1 = pts.max(axis=0)
    side = max(x1 - x0, y1 - y0)
    pad = side * 0.06
    h, w = gray.shape[:2]
    a, b = int(max(0, x0 - pad)), int(max(0, y0 - pad))
    c, d = int(min(w, x1 + pad)), int(min(h, y1 + pad))
    crop = gray[b:d, a:c]
    if crop.size == 0:
        raise QrError("二维码位置不对，裁不出来")
    border = np.concatenate([crop[0], crop[-1], crop[:, 0], crop[:, -1]])
    if border.mean() < 128:
        crop = 255 - crop
    crop = cv2.resize(crop, (QR_SIZE, QR_SIZE), interpolation=cv2.INTER_AREA)
    return np.where(crop > 140, 255, np.where(crop < 100, 0, crop)).astype(np.uint8)


def render_qr(url: str) -> np.ndarray:
    """用链接重新生成一张白底黑码（裁图解不对时用）。"""
    import cv2

    m = cv2.QRCodeEncoder.create().encode(url)
    m = np.where(m > 127, 255, 0).astype(np.uint8)
    inner = QR_SIZE - 2 * 24
    scale = max(1, inner // m.shape[0])
    big = cv2.resize(m, (m.shape[1] * scale, m.shape[0] * scale), interpolation=cv2.INTER_NEAREST)
    out = np.full((QR_SIZE, QR_SIZE), 255, dtype=np.uint8)
    oy, ox = (QR_SIZE - big.shape[0]) // 2, (QR_SIZE - big.shape[1]) // 2
    out[oy:oy + big.shape[0], ox:ox + big.shape[1]] = big
    return out


def qr_png(img, url: str) -> bytes:
    """截图 → 仓库要的 PNG：优先裁原图（保留微信的样式），裁出来解不出同一个链接就重新生成。"""
    import cv2

    text, pts = decode(img)
    if text != url or pts is None:
        raise QrError("截图里没解出同一个链接")
    for make in (lambda: crop_qr(img, pts), lambda: render_qr(url)):
        try:
            out = make()
        except QrError:
            continue
        if decode(out)[0] == url:
            ok, png = cv2.imencode(".png", out)
            if ok:
                return png.tobytes()
    raise QrError("裁出来和重新生成的码都解不出原链接")


def expiry_from_text(text: str, today: dt.date) -> dt.date | None:
    """「该二维码7天内(12月3日前)有效」→ 最后有效的那天（12 月 2 日）。跨年时年份往后推。"""
    m = _EXPIRY.search(text.replace(" ", ""))
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    try:
        d = dt.date(today.year, month, day)
    except ValueError:
        return None
    if d < today - dt.timedelta(days=30):
        d = dt.date(today.year + 1, month, day)
    return d - dt.timedelta(days=1)


def wechat_group(data: dict) -> dict | None:
    """community.json 里第一个带二维码的微信群。"""
    for g in data.get("groups") or []:
        if g.get("platform") == "wechat" and g.get("qr"):
            return g
    return None


def patch_community(text: str, group_id: str, expires: str, today: str) -> str:
    """只改这个群的 expiresAt 和顶层 updatedAt，保留文件原来的排版。"""
    m = re.search(r'"id"\s*:\s*"%s"' % re.escape(group_id), text)
    if not m:
        raise QrError(f"community.json 里没有群 {group_id}")
    start = m.end()
    nxt = re.search(r'"id"\s*:', text[start:])
    end = start + nxt.start() if nxt else len(text)
    seg, n = re.subn(r'("expiresAt"\s*:\s*")\d{4}-\d{2}-\d{2}(")', r"\g<1>%s\g<2>" % expires, text[start:end], count=1)
    if n != 1:
        raise QrError(f"群 {group_id} 没有 expiresAt")
    text = text[:start] + seg + text[end:]
    text, n = re.subn(r'^(\s*"updatedAt"\s*:\s*")\d{4}-\d{2}-\d{2}(")', r"\g<1>%s\g<2>" % today, text, count=1, flags=re.M)
    if n != 1:
        raise QrError("community.json 没有 updatedAt")
    data = json.loads(text)
    g = next(x for x in data["groups"] if x.get("id") == group_id)
    if g.get("expiresAt") != expires or data.get("updatedAt") != today:
        raise QrError("改完的 community.json 对不上")
    return text


def readme_png(qr: np.ndarray, title: str, expires: str) -> bytes:
    """README 用的图：上面群名、中间二维码、下面有效期（应用里那张保持纯二维码，有效期由应用自己写）。"""
    import cv2
    from PIL import Image, ImageDraw, ImageFont

    pad, head, foot = 40, 92, 84
    w = QR_SIZE + 2 * pad
    canvas = Image.new("L", (w, head + QR_SIZE + foot), 255)
    canvas.paste(Image.fromarray(qr), (pad, head))
    draw = ImageDraw.Draw(canvas)
    for text, y, size, color in ((title, head // 2 + 6, 34, 20), (f"有效期至 {expires}", head + QR_SIZE + foot // 2 - 4, 30, 90)):
        font = ImageFont.truetype(FONT, size)
        while draw.textlength(text, font=font) > w - 24 and size > 16:
            size -= 2
            font = ImageFont.truetype(FONT, size)
        draw.text((w // 2, y), text, fill=color, font=font, anchor="mm")
    ok, png = cv2.imencode(".png", np.array(canvas))
    if not ok:
        raise QrError("README 图编码失败")
    return png.tobytes()


def slot_at(at: str, now: float) -> float | None:
    """「换码时间」今天对应的时刻；24:00 当 00:00；格式不对或空返回 None。"""
    m = _TIME.match((at or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h == 24:
        h, mi = 0, 0
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, mi, 0, 0, 0, -1))


def day_start(d: str) -> float:
    try:
        return time.mktime(time.strptime(d[:10], "%Y-%m-%d"))
    except ValueError:
        return 0.0


# ---------- GitHub ----------
class GitHub:
    """建分支、开 PR、直接合并。token 是仓库管理员的细粒度 token（只授权浮屿仓库，Contents + Pull requests 读写），
    管理员能绕过 main 规则集里「必须过 CI」的要求合并 PR。"""

    def __init__(self, repo_of, token: str | None = None, http=None):
        self.repo_of = repo_of
        self.token = token if token is not None else os.environ.get("FUXI_GITHUB_QR_TOKEN", "")
        self.http = http or self._http

    @property
    def ready(self) -> bool:
        return bool(self.token)

    def _http(self, method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "User-Agent": "fuxi-bridge", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "Authorization": "Bearer " + self.token,
            **({"Content-Type": "application/json"} if data else {})})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    raw = r.read()
                    return r.status, json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                try:
                    msg = json.loads(e.read() or b"{}").get("message", "")
                except ValueError:
                    msg = ""
                return e.code, {"message": msg}
            except (urllib.error.URLError, OSError) as e:
                if attempt == 2:
                    raise
                log.info("GitHub 请求网络抖动（%s），%d 秒后重试", e, 2 * (attempt + 1))
                time.sleep(2 * (attempt + 1))

    def call(self, method: str, path: str, body: dict | None = None, ok=(200, 201)) -> dict:
        if not self.token:
            raise QrError("没有配置 FUXI_GITHUB_QR_TOKEN")
        status, data = self.http(method, f"https://api.github.com/repos/{self.repo_of()}{path}", body)
        if status not in ok:
            raise QrError(f"GitHub {method} {path.split('?')[0]} 返回 {status}：{data.get('message', '')}")
        return data

    def get_file(self, path: str, ref: str = "main") -> bytes:
        data = self.call("GET", f"/contents/{path}?ref={ref}")
        if data.get("encoding") == "base64" and data.get("content"):
            return base64.b64decode(data["content"])
        blob = self.call("GET", f"/git/blobs/{data['sha']}")
        return base64.b64decode(blob["content"])

    def publish(self, files: dict[str, bytes], branch: str, title: str, body: str) -> dict:
        """在 main 上加一个提交到新分支，开 PR 并合并。返回 {number, url, merged, error}。"""
        head = self.call("GET", "/git/ref/heads/main")["object"]["sha"]
        base_tree = self.call("GET", f"/git/commits/{head}")["tree"]["sha"]
        tree = []
        for path, content in files.items():
            blob = self.call("POST", "/git/blobs", {"content": base64.b64encode(content).decode(), "encoding": "base64"})
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        new_tree = self.call("POST", "/git/trees", {"base_tree": base_tree, "tree": tree})["sha"]
        commit = self.call("POST", "/git/commits", {"message": f"{title}\n\n{body}", "tree": new_tree, "parents": [head]})["sha"]
        self.call("POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit})
        pr = self.call("POST", "/pulls", {"title": title, "head": branch, "base": "main", "body": body})
        out = {"number": pr["number"], "url": pr.get("html_url", ""), "merged": False, "error": ""}
        try:
            self.call("PUT", f"/pulls/{pr['number']}/merge", {
                "merge_method": "squash", "sha": commit,
                "commit_title": f"{title} (#{pr['number']})", "commit_message": body})
            out["merged"] = True
        except QrError as e:
            out["error"] = str(e)
        return out


# ---------- 主体 ----------
class GroupQr:
    def __init__(self, bridge: "Bridge", root: str, gh: GitHub | None = None):
        self.b = bridge
        self.root = root
        self.gh = gh or GitHub(lambda: self.b.cfg.get("knowledge_repo") or "TixXin/Fuyu")
        os.makedirs(root, mode=0o700, exist_ok=True)
        self._busy = threading.Lock()
        self.state: dict = self._load()

    def _path(self) -> str:
        return os.path.join(self.root, "state.json")

    def _load(self) -> dict:
        try:
            with open(self._path(), encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {"checked_at": 0.0, "tried_at": 0.0, "expires": "", "url": "", "done_day": "",
                "last_pr": "", "last_error": "", "error_day": ""}

    def _save(self) -> None:
        p = self._path()
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(p + ".tmp", p)

    def group(self) -> str:
        return (self.b.cfg.get("wechat_qr_group") or "").strip()

    # ---- 读仓库里现在的码 ----
    def published(self) -> tuple[str, dict, str]:
        """(community.json 原文, 群那一条, 仓库里那张码的链接)。"""
        text = self.gh.get_file(COMMUNITY_JSON).decode("utf-8")
        g = wechat_group(json.loads(text))
        if not g:
            raise QrError("community.json 里没有带二维码的微信群")
        url = ""
        try:
            import cv2

            raw = np.frombuffer(self.gh.get_file(f"{COMMUNITY_DIR}/{g['qr']}"), dtype=np.uint8)
            img = cv2.imdecode(raw, cv2.IMREAD_COLOR)
            if img is not None:
                url = decode(img)[0]
        except QrError as e:
            log.warning("读仓库里的群码失败：%s", e)
        return text, g, url

    # ---- 什么时候动手 ----
    def scheduled(self, now: float) -> bool:
        """今天的换码时间到了、今天还没换过。"""
        slot = slot_at(self.b.cfg.get("wechat_qr_time") or "", now)
        return slot is not None and now >= slot and self.state.get("done_day") != time.strftime("%Y-%m-%d", time.localtime(now))

    def due(self, now: float) -> bool:
        st = self.state
        if now - float(st.get("tried_at") or 0) < RETRY_EVERY:
            return False
        if self.scheduled(now):
            return True
        if st.get("expires") and now - float(st.get("checked_at") or 0) < CHECK_EVERY:
            return now >= day_start(st["expires"])
        return True

    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        if not self.group() or not self.gh.ready or self.b.halted or not self.due(now):
            return
        idle = self.b.sender.human_idle()
        if idle is not None and idle < 120:
            return
        if not self._busy.acquire(blocking=False):
            return
        try:
            self.refresh(now)
        except Exception as e:
            self._fail(now, e)
        finally:
            self._busy.release()

    def _fail(self, now: float, e: Exception) -> None:
        log.warning("换群码失败：%s", e)
        self.state["last_error"] = f"{time.strftime('%m-%d %H:%M', time.localtime(now))} {e}"
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        tell = self.state.get("error_day") != day
        self.state["error_day"] = day
        self._save()
        if tell:
            (getattr(self.b, "notify_owner", None) or self.b.tell_owner)(
                f"【换码失败】浮屿用户群二维码没换成：{e}\n"
                f"每 30 分钟会再试；/换码 预演 看截图，或按浮屿 docs/features/community.md 手动换。"
            )

    def refresh(self, now: float, force: bool = False) -> str:
        """检查一次，需要就换。返回一句话结果。"""
        st = self.state
        st["tried_at"] = now
        text, g, old_url = self.published()
        st["checked_at"], st["expires"] = now, g.get("expiresAt") or ""
        if old_url:
            st["url"] = old_url
        self._save()
        nightly = self.scheduled(now)
        if not force and not nightly and now < day_start(st["expires"]):
            return f"仓库里的码有效到 {st['expires']}，还不用换"
        today = dt.date.fromtimestamp(now)
        shot = self.capture()
        if shot["url"] == old_url or (not force and shot["expires"] and st["expires"]
                                      and shot["expires"].isoformat() <= st["expires"]):
            if nightly:
                st["done_day"] = today.isoformat()
                self._save()
            return f"浮汐拿到的码{shot['expires_text'] or '没读到有效期'}，不比仓库里的（有效到 {st['expires']}）新，不用换"
        exp = shot["expires"] or today + dt.timedelta(days=6)
        png = qr_png(shot["img"], shot["url"])
        files = {COMMUNITY_JSON: patch_community(text, g["id"], exp.isoformat(), today.isoformat()).encode("utf-8"),
                 f"{COMMUNITY_DIR}/{g['qr']}": png, README_QR: self._readme(png, g, exp)}
        title = f"docs(community): refresh the WeChat group QR code through {exp.isoformat()}"
        body = (f"浮汐自动换的浮屿用户群微信二维码：微信里新码显示「{shot['expires_text'] or '7 天内有效'}」，"
                f"expiresAt 填 {exp.isoformat()}。\n\n不跑 CI：只改二维码图片和 community.json，由仓库管理员 token 直接合并。")
        res = self.gh.publish(files, f"community/wechat-qr-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}", title, body)
        had_err = bool(st.get("error_day"))
        st.update(url=shot["url"], expires=exp.isoformat(), last_error="", error_day="", done_day=today.isoformat(),
                  last_pr=f"#{res['number']} {res['url']}")
        self._save()
        if not res["merged"]:
            raise QrError(f"PR #{res['number']} 开好了但没合并：{res['error']}（{res['url']}）")
        msg = f"浮屿用户群二维码换好了，有效到 {exp.isoformat()}，已合入 PR #{res['number']}：{res['url']}"
        log.info(msg)
        if not nightly or had_err:
            fn = (getattr(self.b, "notify_owner", None) or self.b.tell_owner) if nightly else self.b.tell_owner
            fn(("【换码已恢复】" if had_err else "【换码】") + msg)
        return msg

    @staticmethod
    def _readme(png: bytes, g: dict, exp: dt.date) -> bytes:
        """README 那张加上群名和有效期；没装中文字体等出错时退回纯二维码。"""
        import cv2

        name = g.get("name")
        name = (name.get("zh-CN") or name.get("en") or "") if isinstance(name, dict) else str(name or "")
        try:
            qr = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
            return readme_png(qr, f"群聊：{name}" if name else "微信群", exp.isoformat())
        except Exception as e:
            log.warning("README 群码加字失败，用纯二维码：%s", e)
            return png

    # ---- 微信界面 ----
    def capture(self) -> dict:
        """拿着发送锁打开群二维码，返回 {url, expires, expires_text, img}；界面复原后再放锁。"""
        s = self.b.sender
        group = self.group()
        with s._lock:
            try:
                s.open_chat(group)
                w = s.window()
                ww, hh = _geometry(s, w)
                before = {x[0] for x in s._windows()}
                try:
                    return self._open_qr(s, w, ww, hh, before)
                finally:
                    self._restore(s, w, ww, before)
            finally:
                s.forget_chat()

    PANEL_X = 0.6

    @classmethod
    def _panel_boxes(cls, s, w: str, ww: int) -> list[tuple[int, int, str, float]]:
        """只识别右侧群信息面板那一块（整窗识别一次要 2~3 秒）。"""
        x0 = int(ww * cls.PANEL_X)
        img = np.ascontiguousarray(s._capture(w).bgr[:, x0:])
        try:
            res, _ = _ocr()(img, use_cls=False)
        except Exception as e:
            raise QrError(f"OCR 失败：{e}") from e
        out = []
        for box, txt, score in res or []:
            t = str(txt or "").strip()
            if t and float(score) >= 0.7:
                out.append((int(sum(p[0] for p in box) / 4) + x0, int(sum(p[1] for p in box) / 4), t, float(score)))
        return out

    @staticmethod
    def _panel_open(boxes) -> bool:
        return any(t.replace(" ", "") in ("搜索群成员", "群聊名称", "群二维码", "我在本群的昵称") for _, _, t, _ in boxes)

    @classmethod
    def _panel_px(cls, s, w: str, ww: int) -> np.ndarray:
        return s._capture(w).bgr[150::6, int(ww * cls.PANEL_X)::6].astype(np.int16)

    @staticmethod
    def _moved(a: np.ndarray, b: np.ndarray) -> bool:
        return a.shape == b.shape and float((np.abs(a - b).max(axis=2) > 24).mean()) > 0.002

    @classmethod
    def _wait_panel(cls, s, w: str, ww: int, before: np.ndarray, timeout: float = 2.0) -> bool:
        """等面板那块画面变了、再停住（相邻两帧不动），代替固定等待。"""
        deadline = time.monotonic() + timeout
        last, changed = before, False
        while time.monotonic() < deadline:
            time.sleep(0.08)
            cur = cls._panel_px(s, w, ww)
            if changed and not cls._moved(cur, last):
                return True
            changed = changed or cls._moved(cur, before)
            last = cur
        return changed

    def _show_bottom(self, s, w: str, ww: int, hh: int) -> list:
        """点「…」，把面板滚到底（「群二维码」在群成员头像下面），识别一次。"""
        before = self._panel_px(s, w, ww)
        s._click_in(w, ww - 64, 112)
        self._wait_panel(s, w, ww, before)
        s._run("xdotool", "mousemove", "--window", w, str(ww - 256), str(int(hh * 0.83)))
        before = self._panel_px(s, w, ww)
        s._run("xdotool", "click", "--repeat", "20", "--delay", "8", "5")
        self._wait_panel(s, w, ww, before, timeout=1.0)
        return self._panel_boxes(s, w, ww)

    @staticmethod
    def _new_windows(s, w: str, before: set) -> list[tuple[str, int, int]]:
        """点「群二维码」后新出来的窗口；太小的（提示、菜单之类）不算。"""
        return [x for x in s._windows() if x[0] not in before and x[0] != w and x[1] >= 300 and x[2] >= 300]

    @classmethod
    def _wait_popup(cls, s, w: str, before: set, timeout: float = 3.0) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            new = cls._new_windows(s, w, before)
            if new:
                return new[0][0]
            time.sleep(0.1)
        return None

    @staticmethod
    def _grab(s, popup: str | None):
        """截二维码小窗；小窗刚映射、还没画好时 xwd 会失败，当作还没好。"""
        try:
            return np.ascontiguousarray(s._capture(popup).bgr) if popup else _root_shot(s)
        except (SendError, QrError):
            return None

    @staticmethod
    def _expiry_words(img, pts) -> str:
        """只识别二维码下面那两行字（「该二维码7天内(12月3日前)有效…」），识别不到再整张图识别。"""
        y1 = int(pts[:, 1].max()) if pts is not None else 0
        crops = [img[y1:y1 + max(120, img.shape[0] // 5)]] if y1 else []
        for crop in crops + [img]:
            try:
                res, _ = _ocr()(np.ascontiguousarray(crop), use_cls=False)
            except Exception as e:
                log.warning("读群码有效期 OCR 失败：%s", e)
                continue
            words = "".join(str(t) for _, t, _ in (res or []))
            if _EXPIRY.search(words.replace(" ", "")):
                return words
        return ""

    def _open_qr(self, s, w: str, ww: int, hh: int, before: set) -> dict:
        self._panel_shown = False
        boxes = self._show_bottom(s, w, ww, hh)
        if not self._panel_open(boxes):
            boxes = self._show_bottom(s, w, ww, hh)
        if not self._panel_open(boxes):
            raise QrError("点「…」没打开群信息面板")
        self._panel_shown = True
        hits = [b for b in boxes if b[2].replace(" ", "") == "群二维码"]
        if len(hits) != 1:
            raise QrError("群信息面板里没找到「群二维码」（浮汐要在群里；群开了邀请确认时只有群主/管理员有）")
        s._click_in(w, hits[0][0], hits[0][1])
        popup = self._wait_popup(s, w, before)
        url, pts, img = "", None, None
        deadline = time.monotonic() + 4
        while True:
            img = self._grab(s, popup)
            if img is not None:
                url, pts = decode(img)
            if url or time.monotonic() > deadline:
                break
            time.sleep(0.1)
        log.info("群码小窗 %s：%s", popup or "无（整屏）", "解出链接" if url else "没解出")
        if not url or img is None:
            raise QrError("点开「群二维码」后截图里没解出群码")
        words = self._expiry_words(img, pts)
        m = _EXPIRY.search(words.replace(" ", ""))
        return {"url": url, "img": img, "expires_text": m.group(0) + "有效" if m else "",
                "expires": expiry_from_text(words, dt.date.today())}

    def _restore(self, s, w: str, ww: int, before: set) -> None:
        """关掉二维码小窗和群信息面板。一律点按钮，不按 Esc：Esc 落到主窗口上会把它收进托盘。"""
        for pid, pw, _ph in self._new_windows(s, w, before):
            try:
                s._run("xdotool", "mousemove", "--window", pid, str(pw - 46), "34", "click", "1")
            except SendError as e:
                log.warning("关二维码小窗失败：%s", e)
                continue
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline and any(y[0] == pid for y in s._windows()):
                time.sleep(0.05)
            if any(y[0] == pid for y in s._windows()):
                log.warning("二维码小窗 %s 点 × 没关掉", pid)
        try:
            w = s.window()
            shown = getattr(self, "_panel_shown", False) or self._panel_open(self._panel_boxes(s, w, ww))
            if shown:
                before_px = self._panel_px(s, w, ww)
                s._click_in(w, ww - 64, 112)
                self._wait_panel(s, w, ww, before_px, timeout=0.8)
        except (SendError, QrError) as e:
            log.warning("关群信息面板失败：%s", e)
        self._panel_shown = False

    # ---- 遥控 /换码 ----
    def status(self) -> str:
        st = self.state
        if not self.group():
            return "没设置换码群（/设置 换码群 浮屿用户交流群）。"
        lines = [f"换码群：{self.group()}",
                 f"仓库：{self.b.cfg.get('knowledge_repo') or 'TixXin/Fuyu'}（token {'已配置' if self.gh.ready else '没配置 FUXI_GITHUB_QR_TOKEN'}）",
                 f"仓库里的码有效到：{st.get('expires') or '未知'}",
                 f"每天换码时间：{self.b.cfg.get('wechat_qr_time') or '不定时（码到期那天才换）'}"]
        if st.get("tried_at"):
            lines.append("上次检查：" + time.strftime("%m-%d %H:%M", time.localtime(float(st["tried_at"]))))
        if st.get("last_pr"):
            lines.append(f"上次换码：{st['last_pr']}")
        if st.get("last_error"):
            lines.append(f"上次出错：{st['last_error']}")
        lines.append("/换码 预演：只截图解码发给你；/换码 立即：现在就换（不比有效期）")
        return "\n".join(lines)

    def command(self, arg: str) -> str:
        cmd = arg.strip().lower()
        if cmd in ("预演", "dry", "try"):
            return self._spawn(self._dry_run, "预演")
        if cmd in ("立即", "now", "force"):
            return self._spawn(lambda: self.b.tell_owner("【换码】" + self.refresh(time.time(), force=True)), "检查")
        return self.status()

    def _spawn(self, fn, what: str) -> str:
        if not self.group():
            return "没设置换码群（/设置 换码群 浮屿用户交流群）。"

        def go():
            if not self._busy.acquire(timeout=60):
                self.b.tell_owner(f"【换码】{what}没做：上一次还没完")
                return
            try:
                fn()
            except Exception as e:
                log.warning("换码%s出错：%s", what, e)
                self.b.tell_owner(f"【换码】{what}出错：{e}")
            finally:
                self._busy.release()
        threading.Thread(target=go, name="groupqr", daemon=True).start()
        return f"开始{what}，要动一下微信界面，十几秒后回报。"

    def _dry_run(self) -> None:
        from .sending import SendJob

        shot = self.capture()
        exp = shot["expires"] or dt.date.today() + dt.timedelta(days=6)
        png = self._readme(qr_png(shot["img"], shot["url"]), {"name": self.group()}, exp)
        f = tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir=self.root)
        f.write(png)
        f.close()
        try:
            text = (f"【换码预演】{self.group()}：{shot['expires_text'] or '没读到有效期'}"
                    f"（expiresAt 会填 {exp}）。下面是 README 会用的图（应用里那张只有二维码），扫一下确认能进群。")
            job = self.b.send(SendJob(chat=self.b.owner_chat(), text=text, image=f.name, source="control"))
            if not job.ok:
                log.warning("换码预演结果没发出去：%s", job.error)
        finally:
            os.unlink(f.name)


def _geometry(s, w: str) -> tuple[int, int]:
    geo = s._run("xdotool", "getwindowgeometry", w)
    try:
        ww, hh = (int(v) for v in geo.rsplit("Geometry:", 1)[1].strip().split("x"))
    except (IndexError, ValueError) as e:
        raise QrError(f"读不到窗口大小：{geo!r}") from e
    return ww, hh


def _root_shot(s):
    r = subprocess.run(["xwd", "-root", "-silent"], env=s.env, capture_output=True, timeout=15)
    if r.returncode != 0 or not r.stdout:
        raise QrError("整屏截图失败")
    return np.ascontiguousarray(_Shot(r.stdout).bgr)
