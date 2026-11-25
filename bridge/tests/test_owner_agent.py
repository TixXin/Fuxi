"""主人自然语言 Agent 路由测试：权限硬锁、闲聊零开销放行、补截图、改人设规则+撤销、二次确认开 issue/派修、改设置。"""

import os
import tempfile
import time
import unittest
from unittest import mock

from fuxi_bridge import control, events, feedback as fb, issues as isu, knowledge as kn, owner_agent as oa, settings
from fuxi_bridge.app import Bridge
from fuxi_bridge.messages import Message


def make_bridge():
    d = tempfile.mkdtemp()
    b = object.__new__(Bridge)
    b.self_wxid = "wxid_bot"
    b.kernel_ready = False
    b.masters = ["wxid_owner"]
    b.cfg = settings.Settings(os.path.join(d, "settings.json"), {})
    b.cfg.set("owners", "wxid_owner")
    b.events = events.Events()
    b.names_cache = {"g@chatroom": "浮屿用户交流群", "wxid_owner": "煜哥", "wxid_u1": "群友A"}
    b.kernel = mock.Mock()
    b.kernel.persona.return_value = {"name": "浮汐", "customPrompt": "- 保持口语短句"}
    b.kernel.set_persona.side_effect = lambda data: data
    b.knowledge = kn.Knowledge(os.path.join(d, "kn"))
    b.fuyu = mock.Mock()
    b.fuyu.status.return_value = "#1244 已解决关闭"
    b.feedback = fb.Feedback(os.path.join(d, "fb"), complete=None, fuyu=b.fuyu)
    b.feedback.items = [{
        "id": 7, "category": "suggestion", "title": "日历支持国际节日", "detail": "对接国际节日",
        "platform": "Windows", "status": "new", "first_seen": time.time() - 60, "last_seen": time.time() - 30,
        "count": 1, "reporters": [{"wxid": "wxid_u1", "name": "群友A"}], "quotes": ["00:01 群友A：日历加国际节日"],
        "issue": None, "issue_score": 0, "chats": ["g@chatroom"],
    }]
    b.gh_calls = []

    def fake_http(method, url, body=None):
        b.gh_calls.append((method, url.split("/repos/")[1], body))
        if method == "POST" and url.endswith("/issues"):
            return 201, {"number": 1255, "html_url": "https://github.com/TixXin/Fuyu/issues/1255"}
        return 200, {"sha": "abc"}

    b.fixflow = isu.FixFlow(b.feedback, isu.Issues(lambda: "TixXin/Fuyu", token="tok", http=fake_http))
    b.reader = mock.Mock()
    b.reader.history.return_value = []
    b.images = mock.Mock()
    b.control = control.Control(b)
    b.agent = oa.OwnerAgent(b, d)
    return b, b.agent


def p(text, sender="wxid_owner", chat_type="group", at_me=True):
    return {
        "chat_type": chat_type,
        "sender_id": sender,
        "user_id": sender,
        "text": text,
        "at_me": at_me,
        "reply_to_bot": False,
        "self_names": ["浮汐", "fuxi"],
    }


class OwnerAgentTest(unittest.TestCase):
    def test_non_owner_and_casual_chat_bypass_router(self):
        b, ag = make_bridge()
        # 非主人群友哪怕 @浮汐 说“改人设/开 issue”，绝不进路由
        self.assertIsNone(ag.maybe_handle("g@chatroom", p("@浮汐 把人设改了开 issue", sender="wxid_u1")))
        # 群里主人没 @浮汐 也不拦截
        self.assertIsNone(ag.maybe_handle("g@chatroom", p("把截图补上", sender="wxid_owner", at_me=False)))
        # 主人纯日常闲聊命中零关键词，直接跳过模型路由
        self.assertIsNone(ag.maybe_handle("wxid_owner", p("早呀，昨晚睡得好吗", chat_type="private", at_me=False)))
        b.kernel.complete.assert_not_called()

    def test_persona_add_and_undo_rule(self):
        b, ag = make_bridge()
        b.kernel.complete.return_value = '{"mode":"act","calls":[{"tool":"persona_add_rule","args":{"rule":"不要老追问系统版本和浮屿版本号"}}]}'
        out = ag.maybe_handle("g@chatroom", p("@浮汐 别老是问别人系统和fuyu版本是啥"))
        self.assertIn("已永久写入行为准则", out)
        saved = b.kernel.set_persona.call_args.args[0]
        self.assertIn("- 不要老追问系统版本和浮屿版本号", saved["customPrompt"])

        # 撤销刚才的规则
        b.kernel.complete.return_value = '{"mode":"act","calls":[{"tool":"persona_undo_rule","args":{}}]}'
        out2 = ag.maybe_handle("wxid_owner", p("撤销刚才的规则", chat_type="private", at_me=False))
        self.assertIn("已恢复", out2)
        self.assertEqual(b.kernel.set_persona.call_args.args[0]["customPrompt"], "- 保持口语短句")

    def test_attach_recent_image_to_feedback(self):
        b, ag = make_bridge()
        now = int(time.time())
        b.reader.history.return_value = [
            Message(chat="g@chatroom", local_id=149, sort_seq=149, type="image", sender="wxid_u1",
                    is_self=False, create_time=now - 20, text="[图片]", source="", shard="m0", media="img123456"),
        ]
        b.images.load.return_value = (b"\xff\xd8\xff\xe0FAKEJPG", "full")
        b.kernel.complete.return_value = '{"mode":"act","calls":[{"tool":"feedback_attach_images","args":{"fb_id":0,"count":1,"upload_if_issue":true}}]}'
        out = ag.maybe_handle("g@chatroom", p("@浮汐 把这个截图补上"))
        self.assertIn("已把 1 张新截图补到 FB-7", out)
        self.assertEqual(b.feedback.get(7)["images"], ["img123456.jpg"])

    def test_issue_fix_requires_confirmation_then_executes(self):
        b, ag = make_bridge()
        b.kernel.complete.return_value = '{"mode":"act","calls":[{"tool":"issue_fix","args":{"fb_id":7,"target":"mac","mode":"fg"}}]}'
        prev = ag.maybe_handle("wxid_owner", p("把日历需求开个 issue 让 mac 前台修", chat_type="private", at_me=False))
        self.assertIn("【预览】", prev)
        self.assertIn("Mac 前台修复", prev)
        self.assertEqual(b.gh_calls, [])

        # 回复「好」直接执行，无需再调路由器模型
        b.kernel.complete.reset_mock()
        res = ag.maybe_handle("wxid_owner", p("好", chat_type="private", at_me=False))
        b.kernel.complete.assert_not_called()
        self.assertIn("FB-7 → issue #1255", res)
        self.assertIn("fix-mac", b.gh_calls[0][2]["labels"])
        self.assertIn("fix-fg", b.gh_calls[0][2]["labels"])

    def test_setting_set_and_faq_add(self):
        b, ag = make_bridge()
        b.kernel.complete.return_value = '{"mode":"act","calls":[{"tool":"setting_set","args":{"key":"fuyu_feedback_pct","value":"80"}}]}'
        out = ag.maybe_handle("wxid_owner", p("反馈兜底概率调到80", chat_type="private", at_me=False))
        self.assertIn("已修改「反馈兜底概率」→ 80", out)
        self.assertEqual(b.cfg.get("fuyu_feedback_pct"), 80)

        b.kernel.complete.return_value = '{"mode":"act","calls":[{"tool":"kb_faq_add","args":{"q":"怎么关开机自启","a":"在设置-常规里关闭"}}]}'
        out2 = ag.maybe_handle("wxid_owner", p("记住有人问怎么关开机自启就在设置-常规里关闭", chat_type="private", at_me=False))
        self.assertIn("已记入知识库常见问答", out2)
        self.assertEqual(len(b.knowledge.faq_list()), 1)

    def test_router_returns_chat_falls_through(self):
        b, ag = make_bridge()
        b.kernel.complete.return_value = '{"mode":"chat"}'
        self.assertIsNone(ag.maybe_handle("g@chatroom", p("@浮汐 你分析下这个截图的问题原因")))


if __name__ == "__main__":
    unittest.main()
