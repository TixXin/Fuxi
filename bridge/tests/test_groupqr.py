"""每周换群二维码：解码 / 裁图 / 有效期解析 / community.json 只改两处 / 什么时候动手 / GitHub 开 PR 并直接合并。"""

import base64
import datetime as dt
import json
import os
import tempfile
import time
import unittest

import cv2
import numpy as np

from fuxi_bridge import groupqr, settings

URL = "https://weixin.qq.com/g/AwYAAFakeGroupCode0123456789abcdef"
URL2 = "https://weixin.qq.com/g/AwYAANewGroupCode9876543210fedcba"

COMMUNITY = """{
  "version": 1,
  "updatedAt": "2026-11-25",
  "groups": [
    {
      "id": "wechat-users",
      "platform": "wechat",
      "name": { "zh-CN": "浮屿用户交流群", "en": "Fuyu users" },
      "qr": "wechat-group.png",
      "expiresAt": "2026-12-02"
    },
    {
      "id": "qq-users",
      "platform": "qq",
      "name": { "zh-CN": "Fuyu-浮屿交流群", "en": "Fuyu QQ group" },
      "qr": "qq-group.png",
      "number": "610594458"
    }
  ]
}
"""


def screen_with_qr(url: str, dark: bool = False) -> np.ndarray:
    """模拟一张整屏截图：灰底上一张白卡片，中间是群码；dark 时整体反色（深色模式）。"""
    qr = groupqr.render_qr(url)
    img = np.full((900, 1400), 200, dtype=np.uint8)
    img[150:150 + 600, 400:400 + 600] = 255
    img[210:210 + 480, 460:460 + 480] = qr
    if dark:
        img = 255 - img
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def png(url: str) -> bytes:
    return cv2.imencode(".png", groupqr.render_qr(url))[1].tobytes()


class PureTest(unittest.TestCase):
    def test_decode_and_crop_keep_link(self):
        for dark in (False, True):
            img = screen_with_qr(URL, dark)
            url, pts = groupqr.decode(img)
            self.assertEqual(url, URL)
            out = groupqr.crop_qr(img, pts)
            self.assertEqual(out.shape, (groupqr.QR_SIZE, groupqr.QR_SIZE))
            self.assertGreater(int(out[2, 2]), 200, "白底黑码")
            self.assertEqual(groupqr.decode(out)[0], URL)

    def test_qr_png_roundtrip(self):
        raw = groupqr.qr_png(screen_with_qr(URL), URL)
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(groupqr.decode(img)[0], URL)

    def test_qr_png_rejects_other_link(self):
        with self.assertRaises(groupqr.QrError):
            groupqr.qr_png(screen_with_qr(URL), URL2)

    def test_decode_ignores_non_wechat_codes(self):
        qr = groupqr.render_qr("https://example.com/not-a-group")
        self.assertEqual(groupqr.decode(cv2.cvtColor(qr, cv2.COLOR_GRAY2BGR))[0], "")

    def test_expiry(self):
        today = dt.date(2026, 11, 26)
        self.assertEqual(groupqr.expiry_from_text("该二维码7天内(12月3日前)有效，重新进入将更新", today), dt.date(2026, 12, 2))
        self.assertEqual(groupqr.expiry_from_text("该二维码7天内（1 月 2 日前）有效", dt.date(2026, 12, 28)), dt.date(2027, 1, 1))
        self.assertEqual(groupqr.expiry_from_text("12月1日前", today), dt.date(2026, 11, 30))
        self.assertIsNone(groupqr.expiry_from_text("扫一扫上面的二维码图案，加入群聊", today))

    def test_patch_community_only_touches_two_fields(self):
        out = groupqr.patch_community(COMMUNITY, "wechat-users", "2026-12-09", "2026-12-03")
        diff = [(a, b) for a, b in zip(COMMUNITY.splitlines(), out.splitlines()) if a != b]
        self.assertEqual(len(diff), 2)
        data = json.loads(out)
        self.assertEqual(data["updatedAt"], "2026-12-03")
        self.assertEqual(data["groups"][0]["expiresAt"], "2026-12-09")
        self.assertNotIn("expiresAt", data["groups"][1])
        with self.assertRaises(groupqr.QrError):
            groupqr.patch_community(COMMUNITY, "qq-users", "2026-12-09", "2026-12-03")

    def test_readme_png_has_title_and_date_and_still_scans(self):
        raw = groupqr.readme_png(groupqr.render_qr(URL), "群聊：浮屿用户交流群", "2026-12-09")
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
        self.assertGreater(img.shape[0], img.shape[1], "上下各多一行字")
        self.assertEqual(groupqr.decode(img)[0], URL)
        self.assertLess(int(img[:90].min()), 128, "上面有群名")
        self.assertLess(int(img[-80:].min()), 160, "下面有有效期")

    def test_slot_at(self):
        now = time.mktime((2026, 11, 27, 15, 0, 0, 0, 0, -1))
        self.assertEqual(groupqr.slot_at("00:00", now), time.mktime((2026, 11, 27, 0, 0, 0, 0, 0, -1)))
        self.assertEqual(groupqr.slot_at("24:00", now), groupqr.slot_at("0:00", now))
        self.assertIsNone(groupqr.slot_at("", now))
        self.assertIsNone(groupqr.slot_at("25:00", now))

    def test_wechat_group(self):
        self.assertEqual(groupqr.wechat_group(json.loads(COMMUNITY))["id"], "wechat-users")


class FakeGitHub:
    def __init__(self, merge_status: int = 200):
        self.files = {groupqr.COMMUNITY_JSON: COMMUNITY.encode(), "docs/assets/community/wechat-group.png": png(URL)}
        self.calls = []
        self.merge_status = merge_status
        self.blobs = {}

    def __call__(self, method, url, body=None):
        path = url.split("/repos/TixXin/Fuyu", 1)[1]
        self.calls.append((method, path.split("?")[0], body))
        if method == "GET" and path.startswith("/contents/"):
            name = path[len("/contents/"):].split("?")[0]
            return 200, {"encoding": "base64", "content": base64.b64encode(self.files[name]).decode(), "sha": "x"}
        if path == "/git/ref/heads/main":
            return 200, {"object": {"sha": "main1"}}
        if path == "/git/commits/main1":
            return 200, {"tree": {"sha": "tree1"}}
        if path == "/git/blobs":
            sha = f"b{len(self.blobs)}"
            self.blobs[sha] = base64.b64decode(body["content"])
            return 201, {"sha": sha}
        if path == "/git/trees":
            self.tree = body["tree"]
            return 201, {"sha": "tree2"}
        if path == "/git/commits":
            return 201, {"sha": "c2"}
        if path == "/git/refs":
            return 201, {}
        if path == "/pulls":
            return 201, {"number": 1300, "html_url": "https://github.com/TixXin/Fuyu/pull/1300"}
        if path == "/pulls/1300/merge":
            if self.merge_status == 200:
                self.files.update({t["path"]: self.blobs[t["sha"]] for t in self.tree})
            return self.merge_status, {"message": "Merged" if self.merge_status == 200 else "Repository rule violations found"}
        return 404, {"message": "Not Found"}


class FakeSender:
    def human_idle(self):
        return None


def make(http):
    d = tempfile.mkdtemp()
    b = type("B", (), {})()
    b.cfg = settings.Settings(os.path.join(d, "settings.json"), {})
    b.cfg.set("wechat_qr_group", "浮屿用户交流群")
    b.cfg.set("wechat_qr_time", "")
    b.halted = ""
    b.sender = FakeSender()
    b.told = []
    b.tell_owner = b.told.append
    gh = groupqr.GitHub(lambda: "TixXin/Fuyu", token="t", http=http)
    return groupqr.GroupQr(b, os.path.join(d, "qr"), gh), b


T_BEFORE = time.mktime((2026, 11, 28, 10, 0, 0, 0, 0, -1))
T_DUE = time.mktime((2026, 12, 3, 11, 0, 0, 0, 0, -1))


class FlowTest(unittest.TestCase):
    def test_not_due_before_expiry(self):
        http = FakeGitHub()
        qr, b = make(http)
        qr.capture = lambda: self.fail("没到期不该动微信界面")
        self.assertIn("还不用换", qr.refresh(T_BEFORE))
        self.assertEqual(qr.state["url"], URL, "读出了仓库里那张码的链接")
        self.assertFalse(qr.due(T_BEFORE + 60))
        self.assertFalse(qr.due(T_BEFORE + 3600))

    def test_same_code_waits(self):
        http = FakeGitHub()
        qr, b = make(http)
        qr.capture = lambda: {"url": URL, "img": screen_with_qr(URL), "expires": None, "expires_text": ""}
        self.assertIn("不用换", qr.refresh(T_DUE))
        self.assertFalse(any(c[1] == "/pulls" for c in http.calls))
        self.assertFalse(qr.due(T_DUE + 600))
        self.assertTrue(qr.due(T_DUE + groupqr.RETRY_EVERY + 1))

    def test_other_members_code_with_same_expiry_waits(self):
        http = FakeGitHub()
        qr, b = make(http)
        qr.capture = lambda: {"url": URL2, "img": screen_with_qr(URL2), "expires": dt.date(2026, 12, 2),
                              "expires_text": "12月3日前有效"}
        self.assertIn("不比仓库里的", qr.refresh(T_DUE))
        self.assertFalse(any(c[1] == "/pulls" for c in http.calls))

    def test_new_code_opens_pr_and_merges(self):
        http = FakeGitHub()
        qr, b = make(http)
        qr.capture = lambda: {"url": URL2, "img": screen_with_qr(URL2), "expires": dt.date(2026, 12, 9),
                              "expires_text": "12月10日前有效"}
        msg = qr.refresh(T_DUE)
        self.assertIn("2026-12-09", msg)
        self.assertIn("#1300", msg)
        merge = [c for c in http.calls if c[1] == "/pulls/1300/merge"][0][2]
        self.assertEqual(merge["merge_method"], "squash")
        self.assertTrue(merge["commit_title"].startswith("docs(community): refresh the WeChat group QR code through 2026-12-09"))
        tree = [c for c in http.calls if c[1] == "/git/trees"][0][2]
        self.assertEqual(tree["base_tree"], "tree1")
        paths = {t["path"]: http.blobs[t["sha"]] for t in tree["tree"]}
        self.assertEqual(set(paths), {groupqr.COMMUNITY_JSON, "docs/assets/community/wechat-group.png", groupqr.README_QR})
        app_img = cv2.imdecode(np.frombuffer(paths["docs/assets/community/wechat-group.png"], np.uint8), cv2.IMREAD_GRAYSCALE)
        self.assertEqual(app_img.shape, (groupqr.QR_SIZE, groupqr.QR_SIZE), "应用里那张保持正方形纯二维码")
        data = json.loads(paths[groupqr.COMMUNITY_JSON])
        self.assertEqual(data["groups"][0]["expiresAt"], "2026-12-09")
        img = cv2.imdecode(np.frombuffer(paths[groupqr.README_QR], np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(groupqr.decode(img)[0], URL2)
        self.assertEqual(qr.state["url"], URL2)
        self.assertTrue(b.told and "换好了" in b.told[0])
        self.assertFalse(qr.due(T_DUE + 3600))
        qr.capture = lambda: self.fail("刚换过的码还没到期，不该动微信界面")
        self.assertIn("还不用换", qr.refresh(T_DUE + 3 * 86400))
        self.assertEqual(qr.state["expires"], "2026-12-09")
        self.assertTrue(qr.due(time.mktime((2026, 12, 9, 0, 1, 0, 0, 0, -1))))

    def test_nightly_refresh_once_a_day(self):
        http = FakeGitHub()
        qr, b = make(http)
        b.cfg.set("wechat_qr_time", "00:00")
        night = time.mktime((2026, 11, 28, 0, 5, 0, 0, 0, -1))
        qr.capture = lambda: {"url": URL2, "img": screen_with_qr(URL2), "expires": dt.date(2026, 12, 4),
                              "expires_text": "12月5日前有效"}
        self.assertTrue(qr.due(night))
        self.assertIn("#1300", qr.refresh(night))
        self.assertEqual(qr.state["done_day"], "2026-11-28")
        self.assertEqual(b.told, [], "每晚换成功不私聊")
        self.assertFalse(qr.due(night + 3600))
        qr.capture = lambda: self.fail("今天换过了，白天只查仓库、不动微信界面")
        self.assertIn("还不用换", qr.refresh(night + 20 * 3600))
        self.assertTrue(qr.due(night + 86400))

    def test_nightly_same_expiry_marks_done_without_pr(self):
        http = FakeGitHub()
        qr, b = make(http)
        b.cfg.set("wechat_qr_time", "00:00")
        noon = time.mktime((2026, 11, 26, 12, 0, 0, 0, 0, -1))
        qr.capture = lambda: {"url": URL2, "img": screen_with_qr(URL2), "expires": dt.date(2026, 12, 2),
                              "expires_text": "12月3日前有效"}
        self.assertIn("不用换", qr.refresh(noon))
        self.assertFalse(any(c[1] == "/pulls" for c in http.calls))
        self.assertEqual(qr.state["done_day"], "2026-11-26")
        self.assertFalse(qr.due(noon + groupqr.RETRY_EVERY + 1))

    def test_merge_refused_reports(self):
        http = FakeGitHub(merge_status=405)
        qr, b = make(http)
        qr.capture = lambda: {"url": URL2, "img": screen_with_qr(URL2), "expires": None, "expires_text": ""}
        with self.assertRaises(groupqr.QrError) as e:
            qr.refresh(T_DUE)
        self.assertIn("没合并", str(e.exception))

    def test_tick_reports_failure_once_a_day(self):
        http = FakeGitHub()
        qr, b = make(http)

        def boom():
            raise groupqr.QrError("群信息面板里没找到「群二维码」")
        qr.capture = boom
        qr.tick(T_DUE)
        qr.tick(T_DUE + groupqr.RETRY_EVERY + 1)
        self.assertEqual(len(b.told), 1)
        self.assertIn("换码失败", b.told[0])
        self.assertIn("群二维码", qr.state["last_error"])

    def test_tick_off_without_group_or_token(self):
        http = FakeGitHub()
        qr, b = make(http)
        b.cfg.set("wechat_qr_group", "")
        qr.tick(T_DUE)
        self.assertEqual(http.calls, [])
        b.cfg.set("wechat_qr_group", "浮屿用户交流群")
        qr.gh.token = ""
        qr.tick(T_DUE)
        self.assertEqual(http.calls, [])


if __name__ == "__main__":
    unittest.main()
