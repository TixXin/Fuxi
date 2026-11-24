"""反馈群营业：进群消息解析、欢迎合并与真 @、冷场每天一次、营业时段、群小结前后不发、修复通知（预览/直接发）。"""

import os
import tempfile
import time
import unittest
from unittest import mock

from fuxi_bridge import feedback as fb
from fuxi_bridge import outreach as ot
from fuxi_bridge import settings
from fuxi_bridge.messages import Message

DAY = time.mktime((2026, 11, 24, 0, 0, 0, 0, 0, -1))
G = "g@chatroom"

SCAN = """g@chatroom:
<sysmsg type="sysmsgtemplate"><sysmsgtemplate><content_template type="tmpl_type_profile">
<plain><![CDATA[]]></plain><template><![CDATA["$adder$"通过扫描"$from$"分享的二维码加入群聊]]></template>
<link_list><link name="adder" type="link_profile"><memberlist><member><username><![CDATA[user1]]></username>
<nickname><![CDATA[昵称不再换啦]]></nickname></member></memberlist></link>
<link name="from" type="link_profile"><memberlist><member><username><![CDATA[wxid_owner]]></username>
<nickname><![CDATA[煜哥]]></nickname></member></memberlist></link></link_list></content_template></sysmsgtemplate></sysmsg>"""

INVITE = """<sysmsg type="sysmsgtemplate"><sysmsgtemplate><content_template type="tmpl_type_profile">
<template><![CDATA["$username$"邀请"$names$"加入了群聊]]></template><link_list>
<link name="username"><memberlist><member><username>wxid_owner</username><nickname>煜哥</nickname></member></memberlist></link>
<link name="names"><memberlist><member><username>u2</username><nickname>小红</nickname></member>
<member><username>u3</username><nickname>小刚</nickname></member></memberlist></link></link_list>
</content_template></sysmsgtemplate></sysmsg>"""


def msg(text, typ="text", ts=DAY + 10 * 3600, sender="wxid_a", n=1):
    return Message(chat=G, local_id=n, sort_seq=n, type=typ, sender=sender, is_self=sender == "wxid_bot",
                   create_time=int(ts), text=text, source="", shard="s")


class Chunk:
    def __init__(self, source, title, text):
        self.source, self.title, self.text = source, title, text


class FakeFuyu:
    releases = [{"tag_name": "v0.3.14", "prerelease": False, "published_at": "2026-11-23T12:00:00Z",
                 "body": "## 更新内容\n\n### 电池\n\n- **电池插件**：剩余时间不再显示 --\n- 修好了全屏提醒\n"}]

    def status(self, n):
        return {1236: "#1236「电池」已修复：PR #1237 合并于 2026-11-23，包含在 v0.3.14（2026-11-23 发布）"}.get(
            n, f"#{n}「x」还没修")


def bridge():
    from fuxi_bridge.app import Bridge
    d = tempfile.mkdtemp()
    b = object.__new__(Bridge)
    b.cfg = settings.Settings(os.path.join(d, "s.json"), {})
    b.cfg.set("feedback_groups", "浮屿用户交流群")
    b.names_cache = {G: "浮屿用户交流群"}
    b.self_wxid = "wxid_bot"
    b._allowed_chat = lambda c: c == G
    b.send = mock.Mock(return_value=mock.Mock(ok=True, error=""))
    b.reader = mock.Mock()
    b.reader.member_name.side_effect = lambda chat, w: {"user1": "阿杰", "u2": "小红", "u3": ""}.get(w, "")
    b.kernel = mock.Mock()
    b.kernel.complete.return_value = "「最近电池插件的剩余时间修好啦，用浮屿有啥问题直接在群里说或者 @我～」"
    b.fuyu = FakeFuyu()
    b.feedback = fb.Feedback(os.path.join(d, "fb"), complete=None, fuyu=b.fuyu)
    b.knowledge = mock.Mock()
    b.knowledge.index.chunks = [Chunk("docs/releases/v1.md", "v1 更新内容", "x" * 100),
                                Chunk("docs/features/clip.md", "剪贴板 › 搜索", "剪贴板历史可以按关键词搜索，" * 5)]
    b.announcer = mock.Mock()
    b.outreach = ot.Outreach(b, os.path.join(d, "out"))
    return b


class ParseTest(unittest.TestCase):
    def test_join_messages(self):
        self.assertEqual(ot.parse_join(SCAN), [{"wxid": "user1", "name": "昵称不再换啦"}])
        self.assertEqual([j["name"] for j in ot.parse_join(INVITE)], ["小红", "小刚"], "邀请人不算新人")
        self.assertEqual(ot.parse_join("普通消息"), [])
        self.assertEqual(ot.parse_join("<sysmsg><template>xx 修改群名为</template></sysmsg>"), [])

    def test_window_and_points(self):
        self.assertTrue(ot.in_window("10:00-21:30", DAY + 10 * 3600))
        self.assertFalse(ot.in_window("10:00-21:30", DAY + 22 * 3600))
        self.assertTrue(ot.in_window("22:00-02:00", DAY + 1 * 3600), "跨午夜")
        self.assertEqual(ot.release_points(FakeFuyu.releases[0]["body"]), ["电池插件：剩余时间不再显示 --", "修好了全屏提醒"])


class WelcomeTest(unittest.TestCase):
    def test_single_join_real_at_after_wait(self):
        b = bridge()
        b.outreach.observe(msg(SCAN, typ="system"))
        b.outreach.tick(DAY + 10 * 3600 + 30)
        b.send.assert_not_called()                       # 等 1 分钟，把一起进群的合在一起
        b.outreach.tick(DAY + 10 * 3600 + 70)
        job = b.send.call_args.args[0]
        self.assertEqual((job.chat, job.at), (G, "阿杰"))   # 用群昵称真 @
        self.assertTrue(job.text.startswith("欢迎新朋友！我是浮汐"))
        b.outreach.tick(DAY + 10 * 3600 + 200)
        self.assertEqual(b.send.call_count, 1)

    def test_many_joins_text_at_and_outside_hours(self):
        b = bridge()
        b.outreach.observe(msg(INVITE, typ="system", ts=DAY + 23 * 3600))   # 晚上 11 点进群
        b.outreach.tick(DAY + 23 * 3600 + 120)
        b.send.assert_not_called()                                            # 营业时段外不发
        b.outreach.tick(DAY + 86400 + 10 * 3600)                              # 第二天 10 点（11 小时内）
        job = b.send.call_args.args[0]
        self.assertEqual(job.at, "")
        self.assertIn("欢迎 @小红 @小刚！", job.text)


class IdleTest(unittest.TestCase):
    def test_once_per_day_after_quiet(self):
        b = bridge()
        b.outreach.observe(msg("在吗", ts=DAY + 10 * 3600))
        b.outreach.tick(DAY + 11 * 3600)
        b.send.assert_not_called()                                    # 才安静 1 小时
        b.outreach.tick(DAY + 12 * 3600 + 60)
        job = b.send.call_args.args[0]
        self.assertEqual(job.chat, G)
        self.assertEqual(job.text, "最近电池插件的剩余时间修好啦，用浮屿有啥问题直接在群里说或者 @我～")
        b.outreach.tick(DAY + 16 * 3600)
        self.assertEqual(b.send.call_count, 1, "每天最多一次")

    def test_not_near_summary_or_after_hours(self):
        b = bridge()
        b.outreach.observe(msg("在吗", ts=DAY + 17 * 3600))
        b.outreach.tick(DAY + 19 * 3600 + 30 * 60)                    # 19:30，离 20:00 小结不到 1 小时
        b.send.assert_not_called()
        b.outreach.tick(DAY + 21 * 3600 + 40 * 60)                    # 21:40 已过营业时段
        b.send.assert_not_called()

    def test_materials_and_fallback(self):
        b = bridge()
        mats = b.outreach._materials(DAY + 10 * 3600)
        self.assertIn("v0.3.14", mats["fixes"])
        self.assertIn("剪贴板", mats["tip"], "只拿 README / docs/features 当小技巧")
        self.assertIn("最近群里还没有人提反馈", mats["invite"])
        b.kernel.complete.side_effect = RuntimeError("模型挂了")
        self.assertEqual(b.outreach.idle_text(DAY), ot.IDLE_FALLBACK)

    def test_switch_off(self):
        b = bridge()
        b.cfg.set("outreach", "关")
        b.outreach.observe(msg("在吗", ts=DAY + 10 * 3600))
        b.outreach.tick(DAY + 15 * 3600)
        b.send.assert_not_called()


class FixNotifyTest(unittest.TestCase):
    def setUp(self):
        self.b = bridge()
        base = {"detail": "", "platform": "", "count": 1, "quotes": [], "chats": [G], "issue_score": 0.5,
                "first_seen": DAY, "last_seen": DAY}
        self.b.feedback.items = [
            {**base, "id": 1, "category": "bug", "title": "电池剩余时间显示 --", "status": "known", "issue": 1236,
             "reporters": [{"wxid": "user1", "name": "昵称不再换啦"}, {"wxid": "u2", "name": "小红"}]},
            {**base, "id": 2, "category": "bug", "title": "还没修的", "status": "known", "issue": 9, "reporters": []},
            {**base, "id": 3, "category": "bug", "title": "忽略的", "status": "ignored", "issue": 1236, "reporters": []},
        ]

    def test_preview_by_default(self):
        self.b.outreach.on_release("v0.3.14")
        key, chat, text, image = self.b.announcer.create.call_args.args
        self.assertEqual((key, chat, image), ("fix-v0.3.14-g@chatroom", G, None))
        self.assertEqual(self.b.announcer.create.call_args.kwargs["at"], "阿杰")
        self.assertTrue(text.startswith("@小红 浮屿 v0.3.14 发布了"))
        self.assertIn("1. 电池剩余时间显示 --", text)
        self.assertNotIn("还没修的", text)
        self.assertNotIn("忽略的", text)
        self.assertEqual(self.b.feedback.items[0]["status"], "fixed")
        self.assertIn("已在 v0.3.14 修复", self.b.feedback.public_state(self.b.feedback.items[0]))
        self.b.outreach.on_release("v0.3.14")
        self.assertEqual(self.b.announcer.create.call_count, 1, "通知过的不再通知")

    def test_direct_and_off(self):
        self.b.cfg.set("fix_notify", "直接发")
        self.b.outreach.on_release("v0.3.14")
        job = self.b.send.call_args.args[0]
        self.assertEqual((job.chat, job.at), (G, "阿杰"))
        self.b.cfg.set("fix_notify", "关闭")
        self.b.feedback.items[0].pop("fixed_in")
        self.b.outreach.on_release("v0.3.14")
        self.assertEqual(self.b.send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
