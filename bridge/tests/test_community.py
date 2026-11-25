"""社区 PR & 插件市场上新播报测试：首扫基线、过滤本人与 bot、新 PR 私聊、合并群播报+营业时段、去重持久化。"""

import json
import os
import tempfile
import threading
import time
import unittest

from fuxi_bridge import community, settings
from fuxi_bridge.app import Bridge
from fuxi_bridge.sending import SendJob


def make_bridge():
    d = tempfile.mkdtemp()
    b = object.__new__(Bridge)
    b.self_wxid = "wxid_bot"
    b.kernel_ready = False
    b.paused = False
    b.masters = ["wxid_owner"]
    b.cfg = settings.Settings(os.path.join(d, "settings.json"), {})
    b.cfg.set("owners", "wxid_owner")
    b.cfg.set("announce_to", "g@chatroom")
    b.cfg.set("open_hours", "00:00-23:59")
    b.cfg.set("community_notify", True)
    b.cfg.set("plugins_repo", "TixXin/fuyu-plugins")
    b.names_cache = {"g@chatroom": "浮屿用户交流群", "wxid_owner": "煜哥"}
    b._sent = []
    b._sent_ev = threading.Event()
    b._quiet = False

    b.owner_chat = lambda: "wxid_owner"
    b.resolve_chat = lambda s: s
    b._allowed_chat = lambda c: True
    b.in_quiet_hours = lambda: b._quiet
    b.name_of = lambda c: b.names_cache.get(c, c)

    def fake_send(job: SendJob):
        job.ok = True
        b._sent.append((job.chat, job.text))
        b._sent_ev.set()
        return job

    b.send = fake_send
    return b, d


def wait_sent(b, count: int, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while len(b._sent) < count and time.monotonic() < deadline:
        time.sleep(0.02)
    return list(b._sent)


def pr_payload(num: int, title: str, user: str, state: str = "open", draft: bool = False,
               merged_at: str | None = None, body: str = "FreeControl 手机投屏控制插件"):
    return {
        "number": num,
        "title": title,
        "user": {"login": user},
        "state": state,
        "draft": draft,
        "merged_at": merged_at,
        "html_url": f"https://github.com/TixXin/fuyu-plugins/pull/{num}",
        "body": body,
    }


class CommunityWatcherTest(unittest.TestCase):
    def test_baseline_and_filter_owner_bots_ignore(self):
        b, d = make_bridge()
        b.cfg.set("community_ignore_users", "ci-helper")
        responses = [
            # 首次同步：历史 PR 不应触发任何通知
            [pr_payload(1, "历史插件", "alice", state="closed", merged_at="2026-01-01T00:00:00Z")],
            # 第二次：包含作者本人、bot、忽略名单、以及一位外部贡献者
            [
                pr_payload(1, "历史插件", "alice", state="closed", merged_at="2026-01-01T00:00:00Z"),
                pr_payload(2, "我自己的 PR", "TixXin"),
                pr_payload(3, "依赖升级", "dependabot[bot]"),
                pr_payload(4, "自动任务", "claude"),
                pr_payload(5, "被忽略账号", "ci-helper"),
                pr_payload(6, "新增 FreeControl 插件", "pdone", body="为浮屿接入 FreeControl 投屏控制"),
            ],
        ]
        idx = {"i": 0}

        def fake_http(url, etag=""):
            data = responses[idx["i"]]
            idx["i"] += 1
            return 200, {"ETag": f"e{idx['i']}"}, json.dumps(data).encode()

        cw = community.CommunityWatcher(b, os.path.join(d, "comm"), token="", http=fake_http)
        cw.sync_plugins("TixXin/fuyu-plugins", now=1000.0)
        time.sleep(0.05)
        self.assertEqual(b._sent, [])

        cw.sync_plugins("TixXin/fuyu-plugins", now=2000.0)
        sent = wait_sent(b, 1)
        self.assertEqual(len(sent), 1)
        chat, text = sent[0]
        self.assertEqual(chat, "wxid_owner")
        self.assertIn("插件市场新 PR", text)
        self.assertIn("@pdone", text)
        self.assertIn("FreeControl", text)
        self.assertIn("让 mac 看下插件市场 #6 PR", text)

    def test_draft_to_ready_and_merge_group_announcement_and_open_hours(self):
        b, d = make_bridge()
        responses = [
            [],
            # Draft PR 不立即发
            [pr_payload(10, "Draft 插件", "devA", state="open", draft=True)],
            # Draft 转 Ready → 私聊主人
            [pr_payload(10, "Draft 插件", "devA", state="open", draft=False)],
            # 合并（但此时处于静默时段，应暂存群播报，只私聊主人）
            [pr_payload(10, "Draft 插件", "devA", state="closed", draft=False, merged_at="2026-11-25T10:00:00Z")],
        ]
        idx = {"i": 0}

        def fake_http(url, etag=""):
            data = responses[idx["i"]]
            idx["i"] += 1
            return 200, {}, json.dumps(data).encode()

        cw = community.CommunityWatcher(b, os.path.join(d, "comm"), token="", http=fake_http)
        cw.sync_plugins("TixXin/fuyu-plugins", now=100.0)
        cw.sync_plugins("TixXin/fuyu-plugins", now=200.0)
        time.sleep(0.05)
        self.assertEqual(b._sent, [])

        # Draft -> Ready
        cw.sync_plugins("TixXin/fuyu-plugins", now=300.0)
        sent = wait_sent(b, 1)
        self.assertEqual(sent[0][0], "wxid_owner")
        self.assertIn("#10", sent[0][1])

        # 合并时处于静默时段：私聊主人立刻发，群播报暂存
        b._quiet = True
        cw.sync_plugins("TixXin/fuyu-plugins", now=400.0)
        sent = wait_sent(b, 2)
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[1][0], "wxid_owner")
        self.assertIn("插件已上架合并", sent[1][1])
        self.assertEqual(len(cw.state["pending_group"]), 1)

        # 静默结束，flush_group 发群播报
        b._quiet = False
        flushed = cw.flush_group(now=500.0)
        self.assertEqual(flushed, 1)
        sent = wait_sent(b, 3)
        self.assertEqual(sent[2][0], "g@chatroom")
        self.assertIn("浮屿插件市场上新啦", sent[2][1])
        self.assertIn("@devA", sent[2][1])
        self.assertEqual(cw.state["pending_group"], [])

    def test_main_repo_external_pr_events_and_dedup(self):
        b, d = make_bridge()
        cw = community.CommunityWatcher(b, os.path.join(d, "comm"), token="")
        evts = [
            {"type": "pr_opened", "number": 1300, "title": "feat: add international holidays",
             "user": "TixXin", "draft": False, "url": "https://github.com/TixXin/Fuyu/pull/1300", "body": ""},
            {"type": "pr_opened", "number": 1301, "title": "feat: support linux tray",
             "user": "contrib1", "draft": False, "url": "https://github.com/TixXin/Fuyu/pull/1301", "body": "补充 Linux 托盘图标支持"},
            {"type": "pr_merged", "number": 1301, "title": "feat: support linux tray",
             "user": "contrib1", "draft": False, "url": "https://github.com/TixXin/Fuyu/pull/1301", "body": "补充 Linux 托盘图标支持"},
        ]
        cw.on_main_events(evts, "TixXin/Fuyu", now=1000.0)
        sent = wait_sent(b, 2)
        chats = [c for c, _ in sent]
        self.assertIn("wxid_owner", chats)
        self.assertIn("g@chatroom", chats)
        owner_msg = next(t for c, t in sent if c == "wxid_owner")
        group_msg = next(t for c, t in sent if c == "g@chatroom")
        self.assertIn("主仓库外部新 PR", owner_msg)
        self.assertIn("外部 PR 已合并", owner_msg)
        self.assertNotIn("#1300", owner_msg)  # 过滤主人自己的 PR
        self.assertIn("感谢社区贡献者为浮屿带来新更新", group_msg)
        self.assertIn("@contrib1", group_msg)

        # 重复喂相同事件应全部去重，不再发任何消息
        before = len(b._sent)
        cw.on_main_events(evts, "TixXin/Fuyu", now=1100.0)
        time.sleep(0.05)
        self.assertEqual(len(b._sent), before)


if __name__ == "__main__":
    unittest.main()
