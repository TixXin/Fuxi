"""行为设置：settings.json 生成/热加载/校验，Bridge 联动，遥控 /设置。"""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from fuxi_bridge import control, settings
from fuxi_bridge.allow import AllowList
from fuxi_bridge.app import Bridge


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.path = os.path.join(self.d, "settings.json")

    def test_seed_from_legacy_env_once(self):
        s = settings.Settings(self.path, {"FUXI_VISION": "1", "FUXI_CARE": "0", "FUXI_QUIET": "",
                                          "FUXI_OWNER_WXIDS": "wxid_m, wxid_n", "FUXI_RATE_CHAT": "4"})
        self.assertEqual((s.get("vision"), s.get("care_on"), s.get("rate_chat")), ("all", False, 4))
        self.assertEqual(s.get("owners"), ["wxid_m", "wxid_n"])
        self.assertEqual(s.get("quiet"), "")                       # 空环境变量不搬
        with open(self.path) as f:
            self.assertNotIn("quiet", json.load(f))
        s2 = settings.Settings(self.path, {"FUXI_CARE": "1"})     # 文件已在：环境变量不再起作用
        self.assertFalse(s2.get("care_on"))

    def test_parse(self):
        it = settings.BY_KEY
        self.assertEqual(settings.parse(it["vision"], "只看私聊"), "private")
        self.assertTrue(settings.parse(it["care_on"], "开"))
        self.assertEqual(settings.parse(it["self_names"], "鲸鲸，小鲸、 鲸鲸"), ["鲸鲸", "小鲸"])
        self.assertEqual(settings.parse(it["quiet"], "无"), "")
        for key, bad in (("quiet", "半夜"), ("rate_chat", "0"), ("care_on", "也许"), ("vision", "都行")):
            with self.assertRaises(ValueError):
                settings.parse(it[key], bad)

    def test_hot_reload_keeps_old_on_bad_file(self):
        s = settings.Settings(self.path, {})
        with open(self.path, "w") as f:
            json.dump({"rate_global": 9, "nonsense": 1, "rate_chat": "x"}, f)
        os.utime(self.path, (time.time() + 5, time.time() + 5))
        self.assertTrue(s.reload())
        self.assertEqual((s.get("rate_global"), s.get("rate_chat")), (9, 6))
        with open(self.path, "w") as f:
            f.write("{坏")
        os.utime(self.path, (time.time() + 10, time.time() + 10))
        self.assertFalse(s.reload())
        self.assertEqual(s.get("rate_global"), 9)

    def test_kernel_items_not_stored(self):
        s = settings.Settings(self.path, {})
        with self.assertRaises(ValueError):
            s.set("stickers", "开")


def bridge(d):
    b = object.__new__(Bridge)
    b.cfg = settings.Settings(os.path.join(d, "settings.json"), {})
    b.kernel = mock.Mock()
    b.kernel.config.return_value = {"features": {"stickers": True}}
    b.kernel_ready = False                 # 和真实启动顺序一样：先读设置，内核就绪后才联动
    b.images = mock.Mock()
    b.names_cache = {"wxid_m": "主人"}
    b.apply_settings()
    b.kernel_ready = True
    return b


class BridgeSettingsTest(unittest.TestCase):
    def test_apply_and_side_effects(self):
        b = bridge(tempfile.mkdtemp())
        self.assertEqual((b.settle, b.rate_chat, b.vision_mode), (1.5, 6, "all"))
        b.set_setting("settle_ms", "800")
        self.assertEqual(b.settle, 0.8)
        b.set_setting("owners", "wxid_m")
        b.kernel.set_master.assert_called_once_with(["wxid_m"])
        self.assertTrue(b.get_setting("stickers"))
        b.set_setting("stickers", "关")
        b.kernel.patch_config.assert_called_once_with(("features", "stickers"), False)
        b.set_setting("master_always_reply", "关")
        b.kernel.patch_config.assert_called_with(("group", "masterAlwaysReply"), False)
        self.assertEqual((b.get_setting("fuyu_qa_pct"), b.get_setting("fuyu_chat_pct"), b.get_setting("fuyu_chat_cooldown")), (90, 20, 240))
        self.assertEqual((b.get_setting("fuyu_qa_delay"), b.get_setting("group_settle_ms"), b.group_settle), (8, 3500, 3.5))


class ControlSettingsTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        b = bridge(d)
        b.allowlist = AllowList(os.path.join(d, "allow.txt"), "filehelper")
        b.reader = mock.Mock()
        b.reader.find_chats.side_effect = lambda n: [{"username": "wxid_m", "name": "主人", "remark": ""}] \
            if n == "主人" else [{"username": "g@chatroom", "name": "群", "remark": ""}] if n == "群" else []
        self.b, self.c = b, control.Control(b)

    def test_list_show_set(self):
        out = self.c.handle("/设置")
        self.assertIn("- 看图：全部", out)
        self.assertIn("- 表情包：开", out)
        self.assertIn("只看私聊", self.c.handle("/设置 看图"))
        self.assertEqual(self.c.handle("/设置 看图 只看私聊"), "已设置 看图：只看私聊")
        self.assertEqual(self.b.vision_mode, "private")
        self.assertIn("只能是", self.c.handle("/设置 看图 随便"))
        self.assertIn("没有「颜色」这项", self.c.handle("/设置 颜色 红"))

    def test_owner_by_name(self):
        self.assertEqual(self.c.handle("/设置 主人 主人"), "已设置 主人：主人")
        self.assertEqual(self.b.masters, ["wxid_m"])
        self.assertIn("不是一个人", self.c.handle("/设置 主人 群"))


if __name__ == "__main__":
    unittest.main()
