"""P1：appmsg 解析、引用 payload、文件传输助手遥控、搜索提速。全部用合成数据。"""

import collections
import os
import tempfile
import unittest
from unittest import mock

import numpy as np

from fuxi_bridge import control, messages, timing
from fuxi_bridge.allow import AllowList
from fuxi_bridge.app import Bridge
from fuxi_bridge.messages import Message, parse_appmsg
from fuxi_bridge.sender import Sender, title_crop

SELF = "wxid_self"


def appmsg(inner: str, atype: str, title: str = "") -> str:
    return (f'<?xml version="1.0"?>\n<msg><appmsg appid="" sdkver="0"><title>{title}</title>'
            f"<des></des><type>{atype}</type>{inner}</appmsg><fromusername>x</fromusername></msg>")


class AppMsgTest(unittest.TestCase):
    def test_quote_text(self):
        xml = appmsg("<refermsg><type>1</type><svrid>1</svrid><fromusr>g@chatroom</fromusr>"
                     "<chatusr>wxid_a</chatusr><displayname>小明</displayname>"
                     "<content>明天几点集合</content></refermsg>", "57", "八点吧")
        r = parse_appmsg(xml)
        self.assertEqual(r["kind"], "quote")
        self.assertEqual(r["text"], "八点吧")
        self.assertEqual(r["quote"], {"sender": "wxid_a", "name": "小明", "text": "明天几点集合"})

    def test_quote_of_image_and_nested_share(self):
        img = appmsg("<refermsg><type>3</type><chatusr>wxid_a</chatusr><content>&lt;msg&gt;&lt;img/&gt;"
                     "&lt;/msg&gt;</content></refermsg>", "57", "这是啥")
        self.assertEqual(parse_appmsg(img)["quote"]["text"], "[图片]")
        nested = "&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;某篇文章&lt;/title&gt;&lt;/appmsg&gt;&lt;/msg&gt;"
        share = appmsg(f"<refermsg><type>49</type><chatusr>wxid_a</chatusr><content>{nested}</content>"
                       "</refermsg>", "57", "看看")
        self.assertEqual(parse_appmsg(share)["quote"]["text"], "[分享] 某篇文章")

    def test_link_file_pat_and_skips(self):
        link = parse_appmsg(appmsg("<url>https://example.com/a</url>", "5", "标题"))
        self.assertEqual(link["kind"], "link")
        self.assertIn("https://example.com/a", link["text"])
        f = parse_appmsg(appmsg("<appattach><totallen>2048</totallen></appattach>", "6", "a.pdf"))
        self.assertEqual(f["text"], "[文件] a.pdf（2 KB）")
        pat = parse_appmsg(appmsg("<patinfo><pattedusername>wxid_self</pattedusername></patinfo>", "62",
                                  '"小明" 拍了拍我'))
        self.assertEqual((pat["kind"], pat["patted"]), ("pat", "wxid_self"))
        self.assertIsNone(parse_appmsg(appmsg("", "2000", "转账")))  # 转账不交给模型
        self.assertIsNone(parse_appmsg("not xml"))
        self.assertIsNone(parse_appmsg("<msg><appmsg><type>5"))      # 截断的 XML

    def test_group_prefix_is_stripped_before_parse(self):
        # poll() 会先剥掉「wxid:\n」前缀；quote 里被引用的文本也可能带前缀
        xml = appmsg("<refermsg><type>1</type><chatusr>wxid_a</chatusr>"
                     "<content>wxid_a:\n原话</content></refermsg>", "57", "回复")
        self.assertEqual(parse_appmsg(xml)["quote"]["text"], "原话")


def msg(**kw) -> Message:
    base = dict(chat="g@chatroom", local_id=1, sort_seq=1, type="text", sender="wxid_a", is_self=False,
                create_time=0, text="", source="", shard="message_0.db")
    return Message(**(base | kw))


def bare_bridge() -> Bridge:
    b = object.__new__(Bridge)
    b.self_wxid = SELF
    b.extra_names = ["鲸鲸"]
    b.names_cache = {"wxid_a": "小明", "g@chatroom": "测试群", SELF: "我"}
    b.reader = mock.Mock(self_names=lambda: [])
    return b


class PayloadTest(unittest.TestCase):
    def test_quote_of_bot_sets_reply_to_bot(self):
        b = bare_bridge()
        m = msg(type="quote", text="真的吗", quote={"sender": SELF, "name": "鲸鲸", "text": "我会飞"})
        p = b._payload("g@chatroom", [m])
        self.assertTrue(p["reply_to_bot"])
        self.assertEqual(p["text"], "真的吗\n（引用 你 的消息：我会飞）")
        self.assertFalse(p["at_me"])

    def test_at_only_counts_own_words(self):
        b = bare_bridge()
        m = msg(type="quote", text="哈哈", quote={"sender": "wxid_b", "name": "小红", "text": "@鲸鲸 你好"})
        p = b._payload("g@chatroom", [m])
        self.assertFalse(p["at_me"])
        self.assertFalse(p["reply_to_bot"])
        self.assertEqual(p["mentioned_names"], [])
        self.assertIn("引用 小红 的消息", p["text"])

    def test_pat_me_is_at_me(self):
        b = bare_bridge()
        p = b._payload("g@chatroom", [msg(type="pat", text="[拍一拍] 小明 拍了拍我", patted=SELF)])
        self.assertTrue(p["at_me"])


class ControlTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        b = object.__new__(Bridge)
        b.self_wxid = SELF
        b.halted, b.quiet, b.started = "", "", 0
        b.paused_path = os.path.join(d, "paused")
        b.paused = False
        b.names_cache = {"wxid_a": "小明", "filehelper": "文件传输助手"}
        b.allowlist = AllowList(os.path.join(d, "allow.txt"), "filehelper")
        b.reader = mock.Mock()
        b.reader.find_chats.side_effect = lambda n: (
            [{"username": "wxid_a", "name": "小明", "remark": ""}] if n == "小明"
            else [{"username": "x1", "name": "重名", "remark": ""}, {"username": "x2", "name": "重名", "remark": ""}]
            if n == "重名" else [])
        b.sender = mock.Mock()
        b.sender.open_stats.return_value = {"n": 3, "p50": 1.4, "max": 1.9, "last": 1.2}
        b.problems = lambda: []
        b.latency = timing.LatencyStats()
        self.b, self.c = b, control.Control(b)

    def test_only_self_in_filehelper(self):
        self.assertTrue(control.is_command(msg(chat="filehelper", is_self=True, text="/状态"), SELF))
        self.assertFalse(control.is_command(msg(chat="filehelper", is_self=False, text="/状态"), SELF))
        self.assertFalse(control.is_command(msg(chat="wxid_a", is_self=True, text="/状态"), SELF))
        self.assertFalse(control.is_command(msg(chat="filehelper", is_self=True, text="/状态栏怎么调"), SELF))
        self.assertFalse(control.is_command(msg(chat="filehelper", is_self=True, text="随便聊"), SELF))

    def test_pause_resume_persist(self):
        self.b.halted = "测试熔断"
        self.c.handle("/暂停")
        self.assertTrue(self.b.paused and os.path.exists(self.b.paused_path))
        out = self.c.handle("/恢复")
        self.assertFalse(self.b.paused or os.path.exists(self.b.paused_path))
        self.assertEqual(self.b.halted, "")
        self.assertIn("测试熔断", out)

    def test_allow_deny(self):
        self.assertIn("已允许「小明」", self.c.handle("/允许 小明"))
        self.assertIn("wxid_a", self.b.allowlist.items)
        self.assertIn("已在白名单", self.c.handle("/允许 小明"))
        self.assertIn("匹配到 2 个", self.c.handle("/允许 重名"))
        self.assertIn("没找到", self.c.handle("/允许 不存在"))
        self.assertIn("已禁止", self.c.handle("/禁止 小明"))
        self.assertEqual(self.b.allowlist.items, ["filehelper"])

    def test_status_and_help(self):
        self.assertIn("中位 1.4 秒", self.c.handle("/状态"))
        self.assertIn("/测试", self.c.handle("/帮助"))


class SpeedTest(unittest.TestCase):
    def test_title_crop_drops_right_icons(self):
        img = np.full((70, 790, 3), 245, np.uint8)
        img[20:45, 25:190] = 30      # 标题文字
        img[25:40, 700:730] = 60     # 右侧图标
        c = title_crop(img)
        self.assertEqual(c.shape, (25 + 16, 165 + 16, 3))
        self.assertIsNone(title_crop(np.full((70, 790, 3), 245, np.uint8)))

    def _sender_with_frames(self, frames, section="功能"):
        s = object.__new__(Sender)
        seq = iter(frames)
        s._capture = lambda w: mock.Mock(search_state=lambda: next(seq))
        s._section_of = lambda shot, y: section(y) if callable(section) else section
        return s

    def _run_wait(self, s):
        with mock.patch("fuxi_bridge.sender.time.sleep", lambda x: None):
            clock = iter(i * 0.1 for i in range(1000))
            with mock.patch("fuxi_bridge.sender.time.monotonic", lambda: next(clock)):
                return s._wait_results("w", max_wait=3)

    ROW_A = {"y0": 257, "y1": 284}
    ROW_B = {"y0": 431, "y1": 458}

    def test_wait_results_waits_for_stable_frames(self):
        partial = ((335, (1,)), [self.ROW_A])
        full_ambiguous = ((1079, (1, 2)), [self.ROW_A, self.ROW_B])
        s = self._sender_with_frames([partial, partial] + [full_ambiguous] * 200)
        # 前两帧相同但还没到 RESULT_MIN，不能采信那个「唯一匹配」
        self.assertIsNone(self._run_wait(s))

    def test_wait_results_returns_when_stable(self):
        s = self._sender_with_frames([((1079, (1, 2)), [self.ROW_A])] * 50)
        self.assertEqual(self._run_wait(s), (400, 270))

    def test_chat_record_section_is_ignored(self):
        frames = [((1079, (1, 2)), [self.ROW_A, self.ROW_B])] * 50
        s = self._sender_with_frames(frames, section=lambda y: "功能" if y < 400 else "聊天记录")
        self.assertEqual(self._run_wait(s), (400, 270))
        s = self._sender_with_frames(frames, section=lambda y: "联系人" if y < 400 else "群聊")
        self.assertIsNone(self._run_wait(s))          # 两个可点击分区都有 → 不唯一
        s = self._sender_with_frames([((1079, (1,)), [self.ROW_A])] * 50, section="")
        self.assertIsNone(self._run_wait(s))          # 认不出分区 → 不点

    def test_open_stats(self):
        s = object.__new__(Sender)
        s.open_times = collections.deque([(0, 1.0, "搜索"), (0, 2.0, "搜索"), (0, 0.1, "已在会话")])
        self.assertEqual(s.open_stats(), {"n": 2, "p50": 2.0, "max": 2.0, "last": 0.1})


class TimingTest(unittest.TestCase):
    def test_first_delay_subtracts_model_time(self):
        text = "x" * 30                                     # 打字约 5.8~6.6 秒
        self.assertLessEqual(Bridge.first_delay(text, 10), 0.8)
        self.assertGreaterEqual(Bridge.first_delay(text, 10), 0.3)
        d = Bridge.first_delay(text, 2)
        self.assertTrue(3.8 <= d <= 4.6, d)

    def test_trace_and_stats(self):
        t = timing.Trace(msg_time=100.0, seen=100.6)
        t.add("settle", 1.5)
        t.add("kernel", 4.0)
        for i in range(2):
            t.add("typing", 2.0)
            t.add("open", 1.4 if i == 0 else 0.1)
            t.add("paste", 0.4)
            t.add("verify", 1.0)
        t.bubble_sent(110.0)
        t.bubble_sent(115.0)
        r = t.result()
        self.assertEqual((r["first"], r["total"], r["bubbles"], r["discover"]), (10.0, 15.0, 2, 0.6))
        self.assertIn("首条 10.0s", t.summary())
        st = timing.LatencyStats()
        st.add(r)
        st.add(timing.Trace(0, 0).result())   # 没发出的不计
        s = st.summary()
        self.assertEqual((s["n"], s["first"], s["typing"], s["verify"]), (1, 10.0, 2.0, 1.0))
        self.assertIn("首条中位 10.0 秒", st.brief())


if __name__ == "__main__":
    unittest.main()
