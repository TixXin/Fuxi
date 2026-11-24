"""P1：appmsg 解析、引用 payload、文件传输助手遥控、搜索提速。全部用合成数据。"""

import collections
import os
import tempfile
import unittest
from unittest import mock

import numpy as np

from fuxi_bridge import care, control, messages, timing
from fuxi_bridge.allow import AllowList
from fuxi_bridge.app import Bridge
from fuxi_bridge.messages import Message, parse_appmsg
from fuxi_bridge.sender import Sender, section_name, title_crop

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


class MediaTest(unittest.TestCase):
    def test_placeholders(self):
        pm = messages.parse_media
        self.assertEqual(pm("image", '<?xml version="1.0"?><msg><img aeskey="x"/></msg>'), "[图片]")
        self.assertEqual(pm("voice", '<msg><voicemsg voicelength="4600" length="9"/></msg>'), "[语音 5秒]")
        self.assertEqual(pm("voice", "坏数据"), "[语音]")
        self.assertEqual(pm("video", '<msg><videomsg playlength="12"/></msg>'), "[视频 12秒]")
        self.assertEqual(pm("emoji", '<msg><emoji md5="a"/></msg>'), "[动画表情]")
        self.assertEqual(pm("location", '<msg><location x="1" poiname="西湖" label="杭州市西湖区"/></msg>'),
                         "[位置] 西湖 杭州市西湖区")
        self.assertEqual(pm("card", '<?xml version="1.0"?>\n<msg username="wxid_c" nickname="小红"/>'), "[名片] 小红")

    def test_private_chat_sender_prefix(self):
        # 私聊里自己发的图片内容也带「wxid:\n」前缀
        self.assertEqual(messages.parse_media("voice", 'wxid_a:\n<msg><voicemsg voicelength="900"/></msg>'),
                         "[语音 1秒]")

    def test_media_is_accepted(self):
        from fuxi_bridge import app
        for k in ("image", "voice", "emoji"):
            self.assertIn(k, app.ACCEPT_TYPES)


class RevokeTest(unittest.TestCase):
    def setUp(self):
        import hashlib
        import sqlite3
        self.tmp = tempfile.mkdtemp()
        path = os.path.join(self.tmp, "v.db")
        con = sqlite3.connect(path)
        table = "Msg_" + hashlib.md5(b"g@chatroom").hexdigest()
        con.execute(f'CREATE TABLE "{table}" (local_id, local_type, message_content, compress_content)')
        con.executemany(f'INSERT INTO "{table}" VALUES (?,?,?,?)', [
            (1, 1, "还在", None),
            (2, 10000, '<sysmsg type="revokemsg"><revokemsg><content>"小明" 撤回了一条消息</content></revokemsg></sysmsg>', None),
        ])
        con.commit()
        con.close()

        def connect(rel):
            c = sqlite3.connect(path)
            c.row_factory = sqlite3.Row
            return c
        self.reader = object.__new__(messages.MessageReader)
        self.reader.cache = mock.Mock(connect=connect)

    def test_revoked_and_missing_rows(self):
        ms = [msg(local_id=1), msg(local_id=2), msg(local_id=3)]
        self.assertEqual(self.reader.revoked(ms), {ms[1].msg_id, ms[2].msg_id})

    def dispatch(self, revoked_seq):
        b = bare_bridge()
        b.chat_locks = collections.defaultdict(__import__("threading").Lock)
        b.paused = False
        b.reader.revoked = mock.Mock(side_effect=revoked_seq)
        b.kernel = mock.Mock()
        b.kernel.webhook.return_value = {"action": "reply", "bubbles": ["好的"]}
        b.kernel.send_permission.return_value = (True, "")
        b.rate_ok = lambda chat: True
        b.first_delay = lambda text, spent: 0
        b.sent_times = collections.deque()
        b.send = mock.Mock(return_value=mock.Mock(ok=True, t_sent=None))
        tr = timing.Trace(0, 0)
        return b, tr

    def test_revoked_before_kernel_is_dropped(self):
        m1, m2 = msg(local_id=1, text="你好"), msg(local_id=2, text="骂人的话")
        b, tr = self.dispatch([{m2.msg_id}, set()])
        b._dispatch_traced("g@chatroom", [m1, m2], tr)
        self.assertEqual(b.kernel.webhook.call_args[0][0]["text"], "你好")
        b.send.assert_called_once()

    def test_all_revoked_skips_kernel(self):
        m = msg(local_id=1, text="发错了")
        b, tr = self.dispatch([{m.msg_id}])
        b._dispatch_traced("g@chatroom", [m], tr)
        b.kernel.webhook.assert_not_called()

    def test_revoked_while_thinking_cancels_reply(self):
        m = msg(local_id=1, text="发错了")
        b, tr = self.dispatch([set(), {m.msg_id}])
        b._dispatch_traced("g@chatroom", [m], tr)
        b.kernel.webhook.assert_called_once()
        b.send.assert_not_called()


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
        b.care_on = False
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


class CareTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        b = object.__new__(Bridge)
        b.halted, b.paused, b.pending = "", False, {}
        b.in_quiet_hours = lambda: False
        b.names_cache = {"wxid_m": "主人"}
        b.allowlist = AllowList(os.path.join(d, "allow.txt"), "wxid_m,filehelper")
        b.reader = mock.Mock()
        b.reader.sessions.return_value = [{"username": "wxid_old", "last_timestamp": 0}]
        b.kernel = mock.Mock()
        b.kernel.config.return_value = {"care": {"quietAfterMinutes": 20}}
        b.kernel.care_due.return_value = {"due": True, "target": {"wxid": "wxid_m"}}
        b.kernel.send_permission.return_value = (True, "")
        b.kernel.care_text.return_value = {"text": "在干啥呢"}
        b.send = mock.Mock(return_value=mock.Mock(ok=True))
        self.b, self.path = b, os.path.join(d, "care.json")
        self.c = care.Care(b, self.path)

    def test_sends_records_and_persists(self):
        self.assertEqual(self.c.tick(), "sent")
        self.assertEqual(self.b.send.call_args[0][0].text, "在干啥呢")
        self.b.kernel.care_sent.assert_called_once()
        self.assertEqual(care.Care(self.b, self.path).sent_today, 1)
        # 第二次把上次时间和今天次数交给内核判断
        self.c.tick()
        last, n, _ = self.b.kernel.care_due.call_args[0]
        self.assertGreater(last, 0)
        self.assertEqual(n, 1)

    def test_active_chats_are_reported_and_rechecked(self):
        now = __import__("time").time()
        self.b.reader.sessions.side_effect = [[{"username": "wxid_x", "last_timestamp": now}],
                                              [{"username": "wxid_m", "last_timestamp": now}]]
        self.assertEqual(self.c.tick(), "生成期间对方来了消息")
        self.assertEqual(self.b.kernel.care_due.call_args[0][2], ["wxid_x"])
        self.b.send.assert_not_called()

    def test_gates(self):
        self.b.kernel.care_due.return_value = {"due": False, "reason": "too_soon", "waitMinutes": 30}
        self.assertEqual(self.c.tick(), "离上次太近（还要 30 分钟）")
        self.b.kernel.care_due.return_value = {"due": True, "target": {"wxid": "wxid_z"}}
        self.assertIn("不在白名单", self.c.tick())
        self.b.paused = True
        self.assertEqual(self.c.tick(), "已暂停")
        self.b.send.assert_not_called()
        self.assertIn("当前不发：已暂停", self.c.brief())


class SpeedTest(unittest.TestCase):
    def test_section_name_tolerates_icon_noise(self):
        names = Sender.SECTIONS_ALL
        self.assertEqual(section_name("君群聊", names), "群聊")            # 线上实测的误识别
        self.assertEqual(section_name("聊天记录", names), "聊天记录")
        self.assertEqual(section_name("六搜索网络结果膚", names), "搜索网络结果")
        self.assertEqual(section_name("功能", names), "功能")
        self.assertEqual(section_name("浮屿用户交流群", names), "")        # 结果行不是分区标题
        self.assertEqual(section_name("某某群聊天群", names), "")          # 分区名不在开头附近

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

    def test_open_errors_abort_instead_of_halt(self):
        from fuxi_bridge.sender import SendError
        s = object.__new__(Sender)
        s.open_chat = mock.Mock(side_effect=SendError("xwd 截图失败：BadMatch"))
        with self.assertRaises(SendError) as cm:
            s._open_or_abort("某群")
        self.assertIn("放弃发送", str(cm.exception))   # 桥接据此只放弃这一条，不熔断

    def test_open_stats(self):
        s = object.__new__(Sender)
        s.open_times = collections.deque([(0, 1.0, "搜索"), (0, 2.0, "搜索"), (0, 0.1, "已在会话")])
        self.assertEqual(s.open_stats(), {"n": 2, "p50": 2.0, "max": 2.0, "last": 0.1})


class SendRetryTest(unittest.TestCase):
    def _bridge(self, send_effects, verified=True):
        from fuxi_bridge.app import SendJob
        b = object.__new__(Bridge)
        b.halted, b.last_abort, b.last_send_ok = "", None, True
        b.sent_hashes = collections.deque(maxlen=10)
        b.names_cache = {"g@chatroom": "测试群"}
        b.sender = mock.Mock(last_open=0.0)
        b.sender.send_text.side_effect = send_effects
        b._verify = lambda job, t0: verified
        b.RETRY_DELAY = 0
        return b, SendJob(chat="g@chatroom", text="你好")

    def _run(self, b, job):
        # 队列取完这一条后抛 StopIteration 结束 worker 的死循环
        b.sendq = mock.Mock()
        b.sendq.get.side_effect = [job, StopIteration]
        with mock.patch("fuxi_bridge.app.time.sleep", lambda x: None):
            with self.assertRaises(StopIteration):
                b._send_worker()

    def test_abort_is_retried_once_without_halt(self):
        from fuxi_bridge.sender import SendError
        b, job = self._bridge([SendError("xwd 截图失败，放弃发送"), None])
        self._run(b, job)
        self.assertTrue(job.ok and job.verified)
        self.assertEqual((job.error, b.halted), ("", ""))
        self.assertEqual(b.sender.send_text.call_count, 2)

    def test_error_after_enter_halts_and_is_not_retried(self):
        from fuxi_bridge.sender import SendError
        b, job = self._bridge([SendError("xdotool 失败"), None])
        self._run(b, job)
        self.assertTrue(b.halted)
        self.assertEqual(b.sender.send_text.call_count, 1)

    def test_verify_ignores_whitespace(self):
        from fuxi_bridge.app import _squash
        self.assertEqual(_squash("你好 \n 世界\u2005"), _squash("你好世界"))


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
