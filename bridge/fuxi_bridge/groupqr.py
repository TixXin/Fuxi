"""浮屿用户群二维码每周自动换（微信群码 7 天过期，浮屿仓库 main 上的码要跟着换）。

什么时候动手：仓库 main 上 docs/assets/community/community.json 里这个群的 expiresAt（码最后有效的那天）
到了，或者上次换到的码满 7 天了，每 30 分钟试一次：
  拿发送锁 → 打开群 → 右上角「…」→ 群信息面板里找到「群二维码」点开 → 整屏截图解码
  → 链接和仓库里那张不一样才换：裁出二维码（裁出来解不对就用链接重新生成）
  → 建分支、开 PR、用 token 主人的管理员身份直接合并（不等 CI；浮屿 ci.yml 对只改这几个文件的改动不跑）。
链接没变说明微信还没换新码（旧码还在有效期里），下次再试。每个群成员拿到的群码链接都不一样（仓库里那张可能是主人手机上截的），
所以链接不同、但有效期不比仓库里的晚，也不换。

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
LIFETIME = 7 * 86400
QR_SIZE = 480
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
        return {"checked_at": 0.0, "tried_at": 0.0, "expires": "", "url": "", "url_since": 0.0,
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
    def due(self, now: float) -> bool:
        st = self.state
        if now - float(st.get("tried_at") or 0) < RETRY_EVERY:
            return False
        if st.get("expires") and now - float(st.get("checked_at") or 0) < CHECK_EVERY:
            start = day_start(st["expires"])
            if st.get("url_since"):
                start = min(start, float(st["url_since"]) + LIFETIME - 600)
            return now >= start
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
            self.b.tell_owner(f"【换码失败】浮屿用户群二维码没换成：{e}\n"
                              f"每 30 分钟会再试；/换码 预演 看截图，或按浮屿 docs/features/community.md 手动换。")

    def refresh(self, now: float, force: bool = False) -> str:
        """检查一次，需要就换。返回一句话结果。"""
        st = self.state
        st["tried_at"] = now
        text, g, old_url = self.published()
        st["checked_at"], st["expires"] = now, g.get("expiresAt") or ""
        if old_url and old_url != st.get("url"):
            st["url"], st["url_since"] = old_url, 0.0
        self._save()
        if not force and now < day_start(st["expires"]) and not (
                st.get("url_since") and now >= float(st["url_since"]) + LIFETIME - 600):
            return f"仓库里的码有效到 {st['expires']}，还不用换"
        shot = self.capture()
        if shot["url"] == old_url:
            return f"微信里还是同一个码（{shot['expires_text'] or '没读到有效期'}），旧码没过期，稍后再试"
        if shot["expires"] and st["expires"] and shot["expires"].isoformat() <= st["expires"]:
            return f"浮汐的码{shot['expires_text']}，不比仓库里的（有效到 {st['expires']}）新，稍后再试"
        today = dt.date.fromtimestamp(now)
        exp = shot["expires"] or today + dt.timedelta(days=6)
        png = qr_png(shot["img"], shot["url"])
        new_json = patch_community(text, g["id"], exp.isoformat(), today.isoformat())
        files = {COMMUNITY_JSON: new_json.encode("utf-8"), f"{COMMUNITY_DIR}/{g['qr']}": png, README_QR: png}
        title = f"docs(community): refresh the WeChat group QR code through {exp.isoformat()}"
        body = (f"浮汐自动换的浮屿用户群微信二维码：旧码到期，微信里新码显示「{shot['expires_text'] or '7 天内有效'}」，"
                f"expiresAt 填 {exp.isoformat()}。\n\n不跑 CI：只改二维码图片和 community.json，由仓库管理员 token 直接合并。")
        res = self.gh.publish(files, f"community/wechat-qr-{today:%Y%m%d-%H%M}", title, body)
        st.update(url=shot["url"], url_since=now, expires=exp.isoformat(), last_error="", error_day="",
                  last_pr=f"#{res['number']} {res['url']}")
        self._save()
        if not res["merged"]:
            raise QrError(f"PR #{res['number']} 开好了但没合并：{res['error']}（{res['url']}）")
        msg = f"浮屿用户群二维码换好了，有效到 {exp.isoformat()}，已合入 PR #{res['number']}：{res['url']}"
        log.info(msg)
        self.b.tell_owner("【换码】" + msg)
        return msg

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

    @staticmethod
    def _panel_boxes(s, w: str, ww: int) -> list[tuple[int, int, str, float]]:
        return [b for b in s._ocr_boxes(w) if b[0] > ww * 0.6]

    @staticmethod
    def _panel_open(boxes) -> bool:
        return any(t.replace(" ", "") in ("搜索群成员", "群聊名称", "群二维码", "我在本群的昵称") for _, _, t, _ in boxes)

    def _open_qr(self, s, w: str, ww: int, hh: int, before: set) -> dict:
        if not self._panel_open(self._panel_boxes(s, w, ww)):
            s._click_in(w, ww - 64, 112)
            time.sleep(1.0)
        hit = None
        for attempt in range(3):
            boxes = self._panel_boxes(s, w, ww)
            if attempt == 0 and not self._panel_open(boxes):
                raise QrError("点「…」没打开群信息面板")
            hits = [b for b in boxes if b[2].replace(" ", "") == "群二维码"]
            if len(hits) == 1:
                hit = hits[0]
                break
            s._run("xdotool", "mousemove", "--window", w, str(ww - 256), str(int(hh * 0.83)))
            for _ in range(8):
                s._run("xdotool", "click", "5")
            time.sleep(0.6)
        if not hit:
            raise QrError("群信息面板里没找到「群二维码」（浮汐要在群里；群开了邀请确认时只有群主/管理员有）")
        s._click_in(w, hit[0], hit[1])
        deadline = time.monotonic() + 5
        while True:
            time.sleep(0.8)
            img = _root_shot(s)
            url, _ = decode(img)
            if url or time.monotonic() > deadline:
                break
        if not url:
            raise QrError("点开「群二维码」后截图里没解出群码")
        try:
            res, _ = _ocr()(img, use_cls=False)
            words = "".join(str(t) for _, t, _ in (res or []))
        except Exception as e:
            log.warning("读群码有效期 OCR 失败：%s", e)
            words = ""
        m = _EXPIRY.search(words.replace(" ", ""))
        return {"url": url, "img": img, "expires_text": m.group(0) + "有效" if m else "",
                "expires": expiry_from_text(words, dt.date.today())}

    def _restore(self, s, w: str, ww: int, before: set) -> None:
        """关掉二维码小窗和群信息面板。不在聊天里按 Esc（会把主窗口收进托盘）。"""
        for x in s._windows():
            if x[0] not in before and x[0] != w:
                try:
                    s._run("xdotool", "windowactivate", "--sync", x[0])
                    s._run("xdotool", "key", "--window", x[0], "Escape")
                    time.sleep(0.5)
                except SendError as e:
                    log.warning("关二维码小窗失败：%s", e)
        try:
            w = s.window()
            if self._panel_open(self._panel_boxes(s, w, ww)):
                s._click_in(w, ww - 64, 112)
                time.sleep(0.6)
        except SendError as e:
            log.warning("关群信息面板失败：%s", e)

    # ---- 遥控 /换码 ----
    def status(self) -> str:
        st = self.state
        if not self.group():
            return "没设置换码群（/设置 换码群 浮屿用户交流群）。"
        lines = [f"换码群：{self.group()}",
                 f"仓库：{self.b.cfg.get('knowledge_repo') or 'TixXin/Fuyu'}（token {'已配置' if self.gh.ready else '没配置 FUXI_GITHUB_QR_TOKEN'}）",
                 f"仓库里的码有效到：{st.get('expires') or '未知'}"]
        if st.get("tried_at"):
            lines.append("上次检查：" + time.strftime("%m-%d %H:%M", time.localtime(float(st["tried_at"]))))
        if st.get("last_pr"):
            lines.append(f"上次换码：{st['last_pr']}")
        if st.get("last_error"):
            lines.append(f"上次出错：{st['last_error']}")
        lines.append("/换码 预演：只截图解码发给你；/换码 立即：现在就检查并换")
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
        png = qr_png(shot["img"], shot["url"])
        f = tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir=self.root)
        f.write(png)
        f.close()
        try:
            same = shot["url"] == self.state.get("url")
            text = (f"【换码预演】{self.group()}：{shot['expires_text'] or '没读到有效期'}"
                    f"（expiresAt 会填 {shot['expires'] or '今天 + 6 天'}）。"
                    f"{'和仓库里的是同一个码。' if same else '和仓库里记的不是同一个码。'}下面是会提交的图，扫一下确认能进群。")
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
