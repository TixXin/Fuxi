"""白名单、节奏控制、消息解析。"""

import os
import tempfile
import time
import unittest

from fuxi_bridge import messages
from fuxi_bridge.allow import AllowList
from fuxi_bridge.app import Bridge


class AllowListTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "allow.txt")
        self.changes = []

    def test_seed_reload_and_semantics(self):
        a = AllowList(self.path, "filehelper, g@chatroom", on_change=self.changes.append)
        self.assertTrue(a.allows("filehelper") and a.allows("g@chatroom"))
        self.assertFalse(a.allows("someone"))
        time.sleep(0.01)
        with open(self.path, "w") as f:
            f.write("# 注释\nfilehelper   # 文件传输助手\n\n")
        self.assertTrue(a.reload())
        self.assertFalse(a.allows("g@chatroom"))
        self.assertFalse(a.reload())             # 没变化
        with open(self.path, "w") as f:
            f.write("")
        a.reload()
        self.assertFalse(a.allows("filehelper"))  # 空 = 谁都不回
        a.set(["*"], {})
        self.assertTrue(a.allows("anyone"))
        self.assertGreaterEqual(len(self.changes), 4)

    def test_scope_and_blocked(self):
        a = AllowList(self.path, "filehelper, g1@chatroom")
        a.name_of = {"wxid_bad": "讨厌鬼", "g2@chatroom": "吵闹群"}.get
        self.assertFalse(a.allows("wxid_x") or a.allows("g2@chatroom"))
        self.assertFalse(a.open)
        a.scope = "private"
        self.assertTrue(a.allows("wxid_x") and a.open)
        self.assertFalse(a.allows("g2@chatroom"))
        self.assertTrue(a.allows("g1@chatroom"))      # 名单里的群照回
        a.scope = "groups"
        self.assertTrue(a.allows("g2@chatroom"))
        self.assertFalse(a.allows("wxid_x"))
        a.scope = "all"
        a.blocked = {"讨厌鬼", "g1@chatroom", "filehelper"}
        self.assertFalse(a.allows("wxid_bad"))         # 按名字拉黑
        self.assertFalse(a.allows("g1@chatroom"))      # 黑名单优先于白名单
        self.assertTrue(a.allows("filehelper"))        # 文件传输助手不受影响（遥控要用）
        self.assertEqual(a.brief(), "全部私聊和群（名单 2 个），3 个不回")

    def test_scope_setting_syncs_kernel(self):
        from unittest import mock

        b = Bridge.__new__(Bridge)
        b.allowlist = AllowList(self.path, "filehelper")
        b.kernel_ready = True
        b.kernel = mock.Mock()
        from fuxi_bridge import settings
        b.cfg = settings.Settings(os.path.join(os.path.dirname(self.path), "settings.json"), {})
        b.images = mock.Mock()
        b.apply_settings()
        b.kernel.set_whitelist.assert_not_called()   # 默认就是只回名单，不用同步
        b.cfg.set("reply_scope", "private")
        changed = b.apply_settings()
        self.assertIn("回复范围", changed)
        b.kernel.set_whitelist.assert_called_with(["filehelper"], open_=True)
        self.assertTrue(b.allowlist.allows("wxid_someone"))
        b.cfg.set("blocked_chats", "wxid_someone")
        b.apply_settings()
        self.assertFalse(b.allowlist.allows("wxid_someone"))

    def test_seed_only_when_missing(self):
        with open(self.path, "w") as f:
            f.write("filehelper\n")
        a = AllowList(self.path, "other")
        self.assertEqual(a.items, ["filehelper"])


def bare_bridge(**kw) -> Bridge:
    b = object.__new__(Bridge)   # 不连微信/内核，只测纯逻辑
    b.quiet, b.rate_chat, b.rate_global = "", 2, 3
    import collections
    b.sent_times = collections.deque(maxlen=100)
    for k, v in kw.items():
        setattr(b, k, v)
    return b


class PolicyTest(unittest.TestCase):
    def test_quiet_hours_across_midnight(self):
        b = bare_bridge(quiet="23:30-07:00")
        day = time.mktime((2026, 1, 1, 12, 0, 0, 0, 0, -1))
        night = time.mktime((2026, 1, 1, 2, 0, 0, 0, 0, -1))
        late = time.mktime((2026, 1, 1, 23, 45, 0, 0, 0, -1))
        self.assertFalse(b.in_quiet_hours(day))
        self.assertTrue(b.in_quiet_hours(night))
        self.assertTrue(b.in_quiet_hours(late))
        self.assertFalse(bare_bridge(quiet="").in_quiet_hours(night))

    def test_container_timezone(self):
        # 安静时段按本地时间判断，镜像必须是北京时间
        self.assertEqual(time.strftime("%z"), "+0800")

    def test_rate_limits(self):
        b = bare_bridge()
        now = time.time()
        b.sent_times.extend([(now, "a"), (now, "a")])
        self.assertFalse(b.rate_ok("a"))          # 单会话上限 2
        self.assertTrue(b.rate_ok("b"))
        b.sent_times.append((now, "b"))
        self.assertFalse(b.rate_ok("c"))          # 全局上限 3
        b.sent_times.clear()
        b.sent_times.extend([(now - 120, "a")] * 5)
        self.assertTrue(b.rate_ok("a"))           # 一分钟前的不算

    def test_typing_delay_bounds(self):
        self.assertTrue(0.8 <= Bridge.typing_delay("嗯") <= 1.8)
        self.assertTrue(Bridge.typing_delay("长" * 500) <= 6.8)


class MessageParseTest(unittest.TestCase):
    def test_at_users(self):
        src = "<msgsource><atuserlist><![CDATA[wxid_a,wxid_b]]></atuserlist></msgsource>"
        self.assertEqual(messages.at_users(src), ["wxid_a", "wxid_b"])
        self.assertEqual(messages.at_users(""), [])

    def test_decode_plain_and_trailer(self):
        self.assertEqual(messages._decode("你好".encode() + b"\x01\x00junk", None), "你好")
        self.assertEqual(messages._decode(None, "备用".encode()), "备用")

    def test_self_wxid_from_root(self):
        self.assertEqual(messages.self_wxid_from_root("/x/xwechat_files/wxid_abc_1a2b/db_storage"), "wxid_abc")


if __name__ == "__main__":
    unittest.main()
