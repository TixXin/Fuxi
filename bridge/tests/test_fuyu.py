"""浮屿仓库同步（F2）：分页、ETag、增量、变化事件、「修了没」推断、查重检索。合成数据，不联网。"""

import json
import tempfile
import unittest

from fuxi_bridge import fuyu


def rel(tag, at, pre=False, draft=False):
    return {"tag_name": tag, "name": tag, "published_at": at, "prerelease": pre, "draft": draft,
            "html_url": f"https://github.com/o/r/releases/tag/{tag}", "body": "更新内容"}


def issue(n, title, state="open", at="2026-11-01T12:00:00Z", closed=None, reason=None, body=""):
    return {"number": n, "title": title, "state": state, "state_reason": reason, "created_at": at,
            "updated_at": closed or at, "closed_at": closed, "html_url": f"https://github.com/o/r/issues/{n}",
            "comments": 0, "labels": [{"name": "bug"}], "user": {"login": "u"}, "body": body}


def pr(n, title, merged=None, body="", state="closed"):
    d = issue(n, title, state=state, closed=merged, body=body)
    d["pull_request"] = {"merged_at": merged}
    d["html_url"] = f"https://github.com/o/r/pull/{n}"
    return d


class FakeGitHub:
    """按 URL 前缀返回数据；releases 支持 ETag，issues 支持分页。"""

    def __init__(self):
        self.releases = []
        self.pages = [[]]
        self.etag = '"e1"'
        self.calls = []

    def __call__(self, url, etag=""):
        self.calls.append(url)
        if "/releases" in url:
            if etag == self.etag:
                return 304, {}, b""
            return 200, {"ETag": self.etag}, json.dumps(self.releases).encode()
        page = 1
        if "page=" in url and "&page=" in url:
            page = int(url.rsplit("&page=", 1)[1])
        headers = {}
        if page < len(self.pages):
            headers["Link"] = f'<https://api.github.com/repos/o/r/issues?x=1&page={page + 1}>; rel="next"'
        return 200, headers, json.dumps(self.pages[page - 1]).encode()


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.gh = FakeGitHub()
        self.f = fuyu.Fuyu(self.dir, repo="o/r", token="", http=self.gh)

    def test_first_sync_pages_and_no_events(self):
        self.gh.releases = [rel("v1.0.0", "2026-11-10T12:00:00Z"), rel("v1.1.0-beta.1", "2026-11-12T12:00:00Z", pre=True),
                            rel("v9", "2026-11-13T12:00:00Z", draft=True)]
        self.gh.pages = [[issue(1, "崩溃")], [pr(2, "修复崩溃", merged="2026-11-09T12:00:00Z", body="fixes #1")]]
        seen = []
        self.f.on_events = seen.append
        msg = self.f.sync()
        self.assertIn("2 个版本、1 个 issue、1 个 PR", msg)
        self.assertEqual(seen, [], "第一次全量同步不算变化")
        self.assertEqual([r["tag_name"] for r in self.f.releases], ["v1.1.0-beta.1", "v1.0.0"])
        # 落盘后重新加载一致
        again = fuyu.Fuyu(self.dir, repo="o/r", token="", http=self.gh)
        self.assertEqual(set(again.issues), {1})
        self.assertEqual(set(again.prs), {2})

    def test_incremental_events_and_etag(self):
        self.gh.releases = [rel("v1.0.0", "2026-11-10T12:00:00Z")]
        self.gh.pages = [[issue(1, "崩溃"), pr(3, "做功能", state="open")]]
        self.f.sync()
        self.gh.calls.clear()
        self.gh.pages = [[issue(1, "崩溃", state="closed", closed="2026-11-11T12:00:00Z", reason="completed"),
                          issue(4, "新问题"), pr(3, "做功能", merged="2026-11-11T12:00:00Z")]]
        seen = []
        self.f.on_events = seen.append
        self.f.sync()
        types = sorted(e["type"] for e in seen[0])
        self.assertEqual(types, ["issue_closed", "issue_opened", "pr_merged"])
        self.assertTrue(any("since=" in u for u in self.gh.calls), "第二次要带 since 增量拉")
        # releases 没变：304，不产生事件；新版本出现时产生 new_release
        self.gh.releases = [rel("v1.1.0", "2026-11-12T12:00:00Z"), rel("v1.0.0", "2026-11-10T12:00:00Z")]
        self.gh.etag = '"e2"'
        seen.clear()
        self.f.sync()
        self.assertEqual([e["type"] for e in seen[0]], ["new_release"])
        self.assertEqual(len(self.f.recent_events(10)), 4)

    def test_repo_change_resets(self):
        self.gh.pages = [[issue(1, "a")]]
        self.f.sync()
        self.gh.pages = [[issue(7, "b")]]
        self.f.sync("o/other")
        self.assertEqual(set(self.f.issues), {7})

    def test_failure_recorded(self):
        def boom(url, etag=""):
            raise RuntimeError("GitHub 限流，12:00 恢复")
        self.f.http = boom
        with self.assertRaises(RuntimeError):
            self.f.sync()
        self.assertIn("限流", self.f.brief())


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.f = fuyu.Fuyu(tempfile.mkdtemp(), repo="o/r", token="", http=lambda *a: None)
        self.f.releases = [fuyu._release(r) for r in [
            rel("v1.2.0", "2026-11-20T12:00:00Z"), rel("v1.2.0-beta.1", "2026-11-15T12:00:00Z", pre=True),
            rel("v1.1.0", "2026-11-10T12:00:00Z")]]
        items = [issue(10, "[B93] 全屏时提醒看不到"), issue(11, "设置页闪退"), issue(12, "电池图标不对"),
                 issue(13, "不想做的", state="closed", closed="2026-11-05T12:00:00Z", reason="not_planned"),
                 issue(14, "最新的问题")]
        prs = [pr(20, "Mac 全屏改发系统通知", merged="2026-11-08T12:00:00Z", body="对应 B93"),
               pr(21, "修设置页", merged="2026-11-12T12:00:00Z", body="修复 #11"),
               pr(22, "电池", merged="2026-11-21T12:00:00Z", body="Closes #12"),
               pr(23, "还在做", state="open", body="fix #14")]
        self.f.issues = {i["number"]: fuyu._item(i) for i in items}
        self.f.prs = {p["number"]: fuyu._item(p) for p in prs}

    def test_fixed_in_release(self):
        self.assertIn("PR #20 合并于 2026-11-08，包含在 v1.1.0", self.f.status(10))   # 靠 [B93] 编号关联
        self.assertIn("包含在 v1.2.0", self.f.status(11))   # 正式版 v1.2.0 在合并之后
        self.assertIn("还没发版", self.f.status(12))
        self.assertIn("不计划处理", self.f.status(13))
        self.assertIn("还没修", self.f.status(14))   # PR 没合并不算修了
        self.assertIn("还在进行中", self.f.status(23))
        self.assertIn("已合并", self.f.status(22))

    def test_beta_only(self):
        self.f.releases = [r for r in self.f.releases if r["tag_name"] != "v1.2.0"]
        self.assertIn("测试版 v1.2.0-beta.1 已包含，正式版还没发", self.f.status(11))

    def test_find_and_version(self):
        hits = self.f.find("设置页一打开就闪退")
        self.assertEqual(hits[0][1]["number"], 11)
        self.assertEqual(self.f.find(""), [])
        brief = self.f.version_brief()
        self.assertIn("最新正式版 v1.2.0", brief)
        self.assertNotIn("测试版", brief, "测试版比正式版旧时不提")

    def test_render_event(self):
        self.assertEqual(fuyu.render_event({"type": "new_release", "tag": "v1", "prerelease": True}), "发布了测试版 v1")
        self.assertIn("#3", fuyu.render_event({"type": "pr_merged", "number": 3, "title": "x"}))


class WiringTest(unittest.TestCase):
    """接进桥接后的 /浮屿 指令和动态通知。"""

    def setUp(self):
        import os
        from unittest import mock
        from fuxi_bridge import control, settings
        from fuxi_bridge.app import Bridge
        d = tempfile.mkdtemp()
        b = object.__new__(Bridge)
        b.cfg = settings.Settings(os.path.join(d, "s.json"), {})
        st = StatusTest()
        st.setUp()
        b.fuyu = st.f
        b.send = mock.Mock(return_value=mock.Mock(ok=True))
        b.outreach = mock.Mock()
        self.b, self.c, self.mock = b, control.Control(b), mock

    def test_command(self):
        self.assertIn("已修复", self.c.fuyu("#11"))
        self.assertIn("#11", self.c.fuyu("搜 设置页闪退"))
        out = self.c.fuyu("")
        self.assertIn("最新正式版 v1.2.0", out)
        self.assertIn("仓库 TixXin/Fuyu", out)

    def test_notify_release_only_by_default(self):
        events = [{"type": "pr_merged", "number": 3, "title": "x"},
                  {"type": "new_release", "tag": "v1.3.0", "prerelease": False}]
        with self.mock.patch("threading.Thread") as th:
            th.return_value.start = lambda: th.call_args.kwargs["target"]()
            self.b._fuyu_events(events)
            job = self.b.send.call_args.args[0]
            self.assertEqual(job.chat, "filehelper")
            self.assertIn("发布了正式版 v1.3.0", job.text)
            self.assertNotIn("PR #3", job.text)
            self.b.cfg.set("fuyu_notify", "关闭")
            self.b._fuyu_events(events)
            self.assertEqual(self.b.send.call_count, 1)
            self.b.cfg.set("fuyu_notify", "all")
            self.b._fuyu_events(events[:1])
            self.assertIn("PR #3", self.b.send.call_args.args[0].text)


if __name__ == "__main__":
    unittest.main()
