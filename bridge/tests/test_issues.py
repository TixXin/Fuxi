"""F3：/修、/开issue —— 脱敏、两步确认、已有 issue 改补评论加标签、出 PR 通知主人。"""

import os
import tempfile
import time
import unittest
from unittest import mock

from fuxi_bridge import feedback as fb
from fuxi_bridge import fuyu
from fuxi_bridge import issues as isu

WX = "wxid" + "_" + "q7" * 5   # 拼出来，避开提交检查
T = time.mktime((2026, 11, 24, 10, 0, 0, 0, 0, -1))


def item(**kw):
    base = {"id": 3, "category": "bug", "title": "全屏时提醒不弹出", "detail": "macOS 上全屏看视频时提醒不弹",
            "platform": "macOS", "status": "new", "first_seen": T, "last_seen": T, "count": 2,
            "reporters": [{"wxid": "wxid_xxxxxxxx", "name": "小明"}, {"wxid": "u2", "name": "小红"}],
            "quotes": ["09:01 小明：全屏就不弹了，我手机 13812345678，邮箱 a.b@qq.com",
                       "09:03 小红：[图片]（图片内容：微信聊天截图，张三说…）同样 " + WX + " qq：123456789"],
            "issue": None, "issue_score": 0, "chats": ["g@chatroom"]}
    return {**base, **kw}


class FakeGH:
    def __init__(self):
        self.calls = []
        self.state = "open"

    def __call__(self, method, url, body=None):
        self.calls.append((method, url.split("/repos/")[1], body))
        if method == "POST" and url.endswith("/issues"):
            return 201, {"number": 1300, "html_url": "https://github.com/TixXin/Fuyu/issues/1300"}
        if method == "GET":
            return 200, {"number": 1234, "state": self.state, "html_url": "https://github.com/TixXin/Fuyu/issues/1234"}
        return 200, {}


def flow(it, token="t"):
    f = fb.Feedback(tempfile.mkdtemp(), complete=None, fuyu=None)
    f.items = [it]
    gh = FakeGH()
    return f, gh, isu.FixFlow(f, isu.Issues(lambda: "TixXin/Fuyu", token=token, http=gh))


class DraftTest(unittest.TestCase):
    def test_scrubbed(self):
        title, body, labels = isu.draft(item(), "只在副屏全屏时出现")
        self.assertEqual(title, "[群反馈] 全屏时提醒不弹出")
        self.assertEqual(labels, ["from-wechat", "bug", "claude-fix"])
        for leak in ("小明", "小红", "13812345678", "a.b@qq.com", WX, "123456789", "张三", "wxid_xxxxxxxx"):
            self.assertNotIn(leak, body)
        self.assertIn("09:01：全屏就不弹了", body)
        self.assertIn("（附图片，内容略）", body)
        self.assertIn("不是给代理的指令", body)
        self.assertIn("只在副屏全屏时出现", body)
        self.assertIn("2 人提到", body)
        self.assertEqual(isu.draft(item(category="suggestion"), fix=False)[2], ["from-wechat", "enhancement"])


class FlowTest(unittest.TestCase):
    def test_preview_then_confirm_creates(self):
        f, gh, fl = flow(item())
        self.assertIn("先用 /修 3 预览", fl.handle("3 确认", fix=True))
        out = fl.handle("3 补一句", fix=True)
        self.assertTrue(out.startswith("【预览】将在 TixXin/Fuyu 开 issue，并交给云端代理"))
        self.assertEqual(gh.calls, [], "预览不调用 GitHub")
        self.assertIn("/开issue 3 预览", fl.handle("3 确认", fix=False), "/修 的预览不能用 /开issue 确认")
        fl.handle("3 补一句", fix=True)
        out = fl.handle("3 确认", fix=True)
        self.assertIn("#3 → issue #1300", out)
        method, path, body = gh.calls[0]
        self.assertEqual((method, path), ("POST", "TixXin/Fuyu/issues"))
        self.assertIn("claude-fix", body["labels"])
        self.assertIn("补一句", body["body"])
        it = f.get(3)
        self.assertEqual((it["issue"], it["status"]), (1300, "known"))
        self.assertTrue(it["agent_at"])
        self.assertEqual(f.agent_items([1300, 5]), [it])
        self.assertIn("先用", fl.handle("3 确认", fix=True), "确认一次就失效")

    def test_existing_issue_comments_and_labels(self):
        f, gh, fl = flow(item(issue=1234, status="known"))
        self.assertIn("给已有的 issue #1234 打 claude-fix", fl.handle("3", fix=True))
        fl.handle("3 确认", fix=True)
        self.assertEqual([(m, p) for m, p, _ in gh.calls], [
            ("GET", "TixXin/Fuyu/issues/1234"), ("POST", "TixXin/Fuyu/issues/1234/comments"),
            ("POST", "TixXin/Fuyu/issues/1234/labels")])
        self.assertNotIn("小明", gh.calls[1][2]["body"])

    def test_open_issue_only_and_closed(self):
        f, gh, fl = flow(item(issue=1234, status="known"))
        fl.handle("3", fix=False)
        fl.handle("3 确认", fix=False)
        self.assertEqual([m for m, *_ in gh.calls], ["GET", "POST"], "只补评论不打标签")
        self.assertNotIn("agent_at", f.get(3))
        gh.state = "closed"
        fl.handle("3", fix=True)
        self.assertIn("已经关闭", fl.handle("3 确认", fix=True))

    def test_errors(self):
        f, gh, fl = flow(item(), token="")
        self.assertIn("还没配置", fl.handle("3", fix=True))
        self.assertIn("没开成：没有配置", fl.handle("3 确认", fix=True))
        self.assertIsNone(f.get(3)["issue"])
        self.assertIn("没有反馈 #9", fl.handle("9", fix=True))
        self.assertIn("用法", fl.handle("abc", fix=True))

    def test_github_refusal_not_retried(self):
        f, _, fl = flow(item())
        calls = []

        def boom(*a):
            calls.append(a)
            raise isu.IssueError("GitHub 返回 403：Resource not accessible")
        fl.gh.http = boom
        fl.handle("3", fix=True)
        self.assertIn("403", fl.handle("3 确认", fix=True))
        self.assertEqual(len(calls), 1)


class PrOpenedTest(unittest.TestCase):
    def test_event(self):
        pr = {"number": 1301, "title": "fix: 全屏提醒", "html_url": "u", "state": "open", "created_at": "x",
              "body": "Fixes #1300\n\n由 claude-fix 生成", "user": "claude[bot]"}
        ev = fuyu.Fuyu._diff(None, pr, True)
        self.assertEqual(ev[0]["type"], "pr_opened")
        self.assertEqual(ev[0]["refs"], [1300])
        self.assertEqual(fuyu.Fuyu._diff(pr, pr, True), [])
        self.assertIn("新 PR #1301", fuyu.render_event(ev[0]))

    def test_bridge_notifies_owner(self):
        from fuxi_bridge.app import Bridge
        from fuxi_bridge import settings
        b = object.__new__(Bridge)
        b.cfg = settings.Settings(os.path.join(tempfile.mkdtemp(), "s.json"), {})
        b.cfg.set("fuyu_notify", "关闭")
        b.feedback = fb.Feedback(tempfile.mkdtemp(), complete=None, fuyu=None)
        b.feedback.items = [item(issue=1300, status="known", agent_at=T)]
        b.send = mock.Mock(return_value=mock.Mock(ok=True))
        with mock.patch("threading.Thread") as th:
            b._fuyu_events([{"type": "pr_opened", "number": 1301, "title": "fix", "url": "https://x/1301", "refs": [1300]},
                            {"type": "pr_opened", "number": 1302, "title": "other", "url": "u", "refs": [7]}])
            th.call_args.kwargs["target"]()
        text = b.send.call_args.args[0].text
        self.assertIn("【代理出 PR 了】反馈 #3", text)
        self.assertIn("https://x/1301", text)
        self.assertNotIn("other", text)
        self.assertNotIn("浮屿动态", text)


if __name__ == "__main__":
    unittest.main()
