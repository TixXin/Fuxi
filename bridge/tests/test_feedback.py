"""群反馈收集（F1）：收集、识别补写、模型整理并合并、反查反馈人、查重、摘要、群小结、定时。合成数据，不联网。"""

import json
import tempfile
import time
import unittest

from fuxi_bridge import feedback as fb

DAY = time.mktime((2026, 11, 24, 0, 0, 0, 0, 0, -1))   # 本地时间 2026-11-24 00:00


class FakeFuyu:
    def find(self, text, k=1):
        if "闪退" in text:
            return [(0.3, {"number": 1234, "title": "设置页闪退", "state": "closed"}),
                    (0.2, {"number": 77, "title": "设置页卡顿", "state": "open"})]
        return [(0.1, {"number": 1, "title": "无关", "state": "open"})]

    def status(self, n):
        return "#1234「设置页闪退」已修复：PR #1235 合并于 2026-11-23，包含在 v0.3.13（2026-11-23 发布）"


class FakeModel:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, system, text):
        self.prompts.append((system, text))
        return self.answers.pop(0)


def make(model):
    return fb.Feedback(tempfile.mkdtemp(), complete=model, fuyu=FakeFuyu())


def add(f, n, text, sender="wxid_a", name="小明", role="", ts=None):
    f.add(f"g:{n}", ts or DAY + 9 * 3600 + n * 60, "g@chatroom", sender, name, text, role)


class CollectTest(unittest.TestCase):
    def test_classify_merge_and_reporters(self):
        model = FakeModel(
            json.dumps({"items": [
                {"ref": None, "category": "bug", "title": "设置页一打开就闪退", "detail": "Windows 11，v0.3.12",
                 "platform": "Windows", "messages": [1, 2], "answered": False},
                {"ref": None, "category": "question", "title": "怎么关掉开机自启", "messages": [3, 4],
                 "answered": True},
                {"category": "chat", "title": "闲聊"},          # 非法类别丢掉
            ]}),
            json.dumps({"matches": [{"id": 1, "issue": 1234}, {"id": 2, "issue": 999}]}),   # 999 不在候选里，丢掉
            "好的：" + json.dumps({"items": [{"ref": 1, "category": "bug", "title": "设置页闪退",
                                             "messages": [1], "answered": False}]}))
        f = make(model)
        add(f, 1, "设置页一点就闪退了")
        add(f, 2, "我也是，Windows 11", sender="wxid_b", name="小红")
        add(f, 3, "开机自启在哪关", sender="wxid_c", name="小刚")
        add(f, 4, "设置 → 通用 → 开机自启", sender="wxid_me", name="煜哥", role="owner")
        f.annotate("g:1", "设置页一点就闪退了（图片内容：报错弹窗）")
        msg = f.classify(now=DAY + 12 * 3600)
        self.assertIn("新反馈 2 条", msg)
        self.assertIn("报错弹窗", model.prompts[0][1], "识别补写要替换原文")
        self.assertIn("煜哥（主人）", model.prompts[0][1])
        bug, q = f.items
        self.assertEqual((bug["id"], bug["status"], bug["issue"]), (1, "known", 1234))
        self.assertEqual([r["name"] for r in bug["reporters"]], ["小明", "小红"])
        self.assertEqual(q["status"], "answered")
        self.assertEqual([r["wxid"] for r in q["reporters"]], ["wxid_c"], "主人不算反馈人")
        # 第二轮：只交新消息，并入已有条目
        self.assertEqual(f.classify(now=DAY + 12 * 3600), "没有新消息")
        add(f, 10, "又闪退了", sender="wxid_d", name="小李")
        f.classify(now=DAY + 13 * 3600)
        self.assertEqual(len(f.items), 2)
        self.assertEqual(len(f.items[0]["reporters"]), 3)
        self.assertIn("#1 [bug] 设置页一打开就闪退", model.prompts[2][0], "已有条目要给模型看")
        self.assertIn("候选 #1234（已关闭）设置页闪退", model.prompts[1][1])
        self.assertNotIn("怎么关掉开机自启", model.prompts[1][1], "提问不去对 issue")
        # 落盘后重新加载
        again = fb.Feedback(f.root, complete=model, fuyu=FakeFuyu())
        self.assertEqual(len(again.items), 2)
        self.assertEqual(again.meta["next_id"], 3)

    def test_bad_model_output_keeps_messages(self):
        f = make(FakeModel("我不知道", json.dumps({"items": []})))
        add(f, 1, "有个问题")
        with self.assertRaises(ValueError):
            f.classify(now=DAY + 12 * 3600)
        self.assertIn("上次出错", f.brief())
        self.assertIn("整理了 1 条", f.classify(now=DAY + 12 * 3600), "失败的批次下次重来")

    def test_recent_messages_wait(self):
        f = make(FakeModel())
        add(f, 1, "刚发的", ts=time.time())
        self.assertEqual(f.classify(), "没有新消息", "30 秒内的消息可能还在补识别")


class MatchTest(unittest.TestCase):
    def item(self):
        return {"id": 5, "category": "bug", "title": "设置页闪退", "detail": "", "status": "new", "issue": None,
                "quotes": ["09:00 小明：设置页闪退"]}

    def test_model_says_no_match(self):
        f = make(FakeModel(json.dumps({"matches": [{"id": 5, "issue": None}]})))
        it = self.item()
        f._match([it])
        self.assertIsNone(it["issue"])
        self.assertEqual(it["status"], "new")

    def test_model_fails_falls_back_to_high_lexical_only(self):
        f = make(FakeModel("坏了"))
        it = self.item()
        f._match([it])
        self.assertIsNone(it["issue"], "字面分 0.3 不够 0.5，不配")


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.f = make(FakeModel())
        base = {"detail": "", "platform": "Windows", "count": 1, "quotes": ["09:01 小明：闪退"], "chats": ["g@chatroom"],
                "issue": None, "issue_score": 0}
        t = DAY + 10 * 3600
        self.f.items = [
            {**base, "id": 1, "category": "bug", "title": "设置页闪退", "status": "known", "issue": 1234,
             "first_seen": t, "last_seen": t, "reporters": [{"wxid": "a", "name": "小明"}, {"wxid": "b", "name": "小红"}]},
            {**base, "id": 2, "category": "suggestion", "title": "希望支持农历", "status": "new",
             "first_seen": t, "last_seen": t, "reporters": [{"wxid": "a", "name": "小明"}]},
            {**base, "id": 3, "category": "question", "title": "怎么关开机自启", "status": "answered",
             "first_seen": t, "last_seen": t, "reporters": []},
            {**base, "id": 4, "category": "bug", "title": "旧问题", "status": "new",
             "first_seen": t - 3 * 86400, "last_seen": t - 3 * 86400, "reporters": []},
        ]

    def test_digest_for_owner(self):
        d = self.f.digest(since=DAY)
        self.assertIn("共 3 条", d)
        self.assertIn("#1 [问题] 设置页闪退（2 人） → issue #1234", d)
        self.assertIn("包含在 v0.3.13", d)
        self.assertNotIn("旧问题", d)
        self.assertIn("没有新的反馈", self.f.digest(since=DAY + 86400))

    def test_group_summary_is_public_safe(self):
        s = self.f.group_summary("2026-11-24")
        self.assertTrue(s.startswith("【今日反馈小结】11月24日"))
        self.assertIn("设置页闪退 —— 已在 v0.3.13 修复，更新到最新版即可", s)
        self.assertIn("希望支持农历 —— 已记录", s)
        self.assertIn("怎么关开机自启 —— 已在群里解答", s)
        for secret in ("小明", "小红", "#1234", "issue", "旧问题"):
            self.assertNotIn(secret, s)
        self.f.ignore(2)
        self.assertNotIn("农历", self.f.group_summary("2026-11-24"))
        self.assertEqual(self.f.group_summary("2026-11-25"), "", "没有反馈不发")

    def test_detail_and_listing(self):
        d = self.f.detail(1)
        self.assertIn("反馈人：小明、小红", d)
        self.assertIn("09:01 小明：闪退", d)
        self.assertEqual(self.f.detail(99), "没有 #99")
        self.assertIn("#2 [建议] 希望支持农历 → 新", self.f.listing(days=10_000))

    def test_due_once_per_day_within_window(self):
        noon = DAY + 12 * 3600
        self.assertFalse(self.f.due("digest", "12:00", noon - 60))
        self.assertTrue(self.f.due("digest", "12:00", noon + 60))
        self.f.mark("digest", noon + 60)
        self.assertFalse(self.f.due("digest", "12:00", noon + 120), "一天只发一次")
        self.assertFalse(self.f.due("group", "20:00", DAY + 23 * 3600), "晚了 3 小时不补发")
        self.assertTrue(self.f.due("group", "20:00", DAY + 21 * 3600))
        self.assertFalse(self.f.due("group", "", DAY + 21 * 3600), "空 = 关闭")


class WiringTest(unittest.TestCase):
    """接进桥接：收集、定时发摘要和群小结、遥控指令。"""

    def setUp(self):
        import os
        from unittest import mock
        from fuxi_bridge import control, settings
        from fuxi_bridge.app import Bridge
        d = tempfile.mkdtemp()
        b = object.__new__(Bridge)
        b.cfg = settings.Settings(os.path.join(d, "s.json"), {})
        b.cfg.set("feedback_groups", "浮屿用户交流群")
        b.names_cache = {"g@chatroom": "浮屿用户交流群", "h@chatroom": "别的群", "wxid_a": "小明", "wxid_me": "煜哥"}
        b.masters = ["wxid_me"]
        b.self_wxid = "wxid_bot"
        b.allowlist = mock.Mock(items=["g@chatroom"])
        b._allowed_chat = lambda c: c == "g@chatroom"
        b.send = mock.Mock(return_value=mock.Mock(ok=True, error=""))
        model = FakeModel(json.dumps({"items": [{"ref": None, "category": "bug", "title": "设置页闪退",
                                                 "messages": [1], "answered": False}]}),
                          json.dumps({"matches": [{"id": 1, "issue": 1234}]}))
        b.feedback = fb.Feedback(os.path.join(d, "fb"), complete=model, fuyu=FakeFuyu())
        b.outreach = mock.Mock()
        b._agent_watch = mock.Mock()
        self.b, self.c, self.mock = b, control.Control(b), mock

    def msg(self, n, text, sender="wxid_a", chat="g@chatroom", ts=None):
        from fuxi_bridge.messages import Message
        return Message(chat=chat, local_id=n, sort_seq=n, type="text", sender=sender, is_self=sender == "wxid_bot",
                       create_time=int(ts or DAY + 9 * 3600 + n), text=text, source="", shard="s")

    def test_collect_roles_and_group_filter(self):
        b = self.b
        self.assertTrue(b.is_feedback_group("g@chatroom"))
        self.assertFalse(b.is_feedback_group("h@chatroom"))
        b._collect([self.msg(1, "设置页闪退"), self.msg(2, "我看看", sender="wxid_me"), self.msg(3, "收到", sender="wxid_bot")])
        recs = b.feedback._inbox(0)
        self.assertEqual([(r["name"], r["role"]) for r in recs], [("小明", ""), ("煜哥", "owner"), ("浮汐", "bot")])

    def test_tick_sends_digest_and_summary(self):
        b = self.b
        b._collect([self.msg(1, "设置页一点就闪退")])
        b._feedback_tick(now=DAY + 12 * 3600 + 30)            # 中午：整理 + 摘要私聊主人
        job = b.send.call_args.args[0]
        self.assertEqual(job.chat, "wxid_me")
        self.assertIn("#1 [问题] 设置页闪退", job.text)
        b._feedback_tick(now=DAY + 12 * 3600 + 90)
        self.assertEqual(b.send.call_count, 1, "一天只发一次")
        b._feedback_tick(now=DAY + 20 * 3600 + 30)            # 晚上：群小结发到反馈群
        job = b.send.call_args.args[0]
        self.assertEqual(job.chat, "g@chatroom")
        self.assertIn("设置页闪退 —— 已在 v0.3.13 修复", job.text)
        self.assertNotIn("小明", job.text)

    def test_no_groups_no_work(self):
        self.b.cfg.set("feedback_groups", "")
        self.b._feedback_tick(now=DAY + 12 * 3600 + 30)
        self.b.send.assert_not_called()

    def test_commands(self):
        self.b._collect([self.msg(1, "设置页一点就闪退")])
        self.assertIn("新反馈 1 条", self.c.feedback("整理"))
        self.assertIn("#1 [问题] 设置页闪退", self.c.feedback(""))
        self.assertIn("反馈人：小明", self.c.feedback("1"))
        self.assertIn("【今日反馈小结】", self.c.feedback("小结")) if time.strftime("%Y-%m-%d") == "2026-11-24" else None
        self.assertIn("已忽略", self.c.ignore("#1"))
        self.assertEqual(self.c.ignore("x"), "用法：/忽略 3")


if __name__ == "__main__":
    unittest.main()
