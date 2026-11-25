"""发版公告：预览 → /发布 才发群，或直接发；幂等键；过期；失败不重试。"""

import os
import tempfile
import time
import unittest
from unittest import mock

from fuxi_bridge import announce, control


class FakeBridge:
    def __init__(self):
        self.sent = []
        self.fail_on = None
        self.allowlist = mock.Mock()
        self.allowlist.allows.return_value = False
        self.names_cache = {}

    def name_of(self, chat):
        return {"g@chatroom": "浮屿用户交流群"}.get(chat, chat)

    def send(self, job):
        self.sent.append((job.chat, job.text, bool(job.image), job.source))
        job.ok = self.fail_on is None or not self.fail_on(job)
        job.error = "" if job.ok else "进不去会话"
        return job


class AnnounceTest(unittest.TestCase):
    def setUp(self):
        self.b = FakeBridge()
        self.root = tempfile.mkdtemp()
        self.a = announce.Announcer(self.b, self.root)

    def test_preview_then_publish(self):
        it, created = self.a.create("fuyu-v1.2.3", "g@chatroom", "浮屿 v1.2.3 已更新", b"png", ".png")
        self.assertTrue(created)
        self.assertEqual([s[0] for s in self.b.sent], ["filehelper"] * 3)      # 说明、图、正文
        self.assertIn(f"/发布 {it['code']}", self.b.sent[0][1])
        self.b.sent.clear()
        it = self.a.publish()
        self.assertEqual(it["status"], "sent")
        self.assertEqual(self.b.sent, [("g@chatroom", None, True, "api"), ("g@chatroom", "浮屿 v1.2.3 已更新", False, "api")])
        self.assertFalse(os.path.exists(os.path.join(self.root, os.path.basename(it["image"]))))

    def test_direct_skips_preview(self):
        it, created = self.a.create("fuyu-v1.2.4", "g@chatroom", "浮屿 v1.2.4 已更新", b"png", ".png", confirm=False)
        self.assertTrue(created)
        self.assertEqual(it["status"], "sent")
        self.assertEqual(self.b.sent, [("g@chatroom", None, True, "api"), ("g@chatroom", "浮屿 v1.2.4 已更新", False, "api")])
        self.b.sent.clear()
        it, created = self.a.create("fuyu-v1.2.4", "g@chatroom", "again", None, confirm=False)
        self.assertEqual((created, it["status"], self.b.sent), (False, "sent", []))

    def test_direct_failure_recorded(self):
        self.b.fail_on = lambda j: True
        it, _ = self.a.create("y", "g@chatroom", "字", None, confirm=False)
        self.assertEqual(it["status"], "failed")
        self.assertEqual(self.a.pending(), [])

    def test_idempotent_key(self):
        self.a.create("k", "g@chatroom", "hi", None)
        n = len(self.b.sent)
        it, created = self.a.create("k", "g@chatroom", "hi again", None)
        self.assertFalse(created)
        self.assertEqual(len(self.b.sent), n)
        self.a.publish()
        it, created = announce.Announcer(self.b, self.root).create("k", "g@chatroom", "hi", None)  # 重启后仍记得
        self.assertEqual((created, it["status"]), (False, "sent"))

    def test_needs_code_when_several(self):
        a1, _ = self.a.create("a", "g@chatroom", "一", None)
        self.a.create("b", "g@chatroom", "二", None)
        with self.assertRaises(announce.AnnounceError):
            self.a.publish()
        self.assertEqual(self.a.cancel(a1["code"])["status"], "cancelled")
        self.assertEqual(self.a.publish()["text"], "二")

    def test_expire(self):
        it, _ = self.a.create("old", "g@chatroom", "旧的", None)
        it["created"] -= announce.TTL + 1
        self.assertEqual(self.a.pending(), [])
        self.assertEqual(self.a.get("old")["status"], "expired")

    def test_failed_half_way_not_retried(self):
        self.a.create("x", "g@chatroom", "字", b"img")
        self.b.fail_on = lambda j: j.text == "字"
        it = self.a.publish()
        self.assertEqual(it["status"], "failed")
        with self.assertRaises(announce.AnnounceError):
            self.a.publish(it["code"])

    def test_requires_key_and_content(self):
        with self.assertRaises(announce.AnnounceError):
            self.a.create("", "g@chatroom", "x", None)
        with self.assertRaises(announce.AnnounceError):
            self.a.create("k2", "g@chatroom", None, None)


class AnnounceControlTest(unittest.TestCase):
    def test_commands(self):
        b = FakeBridge()
        b.announcer = announce.Announcer(b, tempfile.mkdtemp())
        c = control.Control(b)
        self.assertEqual(c.handle("/待发"), "没有等确认的公告。")
        it, _ = b.announcer.create("k", "g@chatroom", "发版啦", None)
        self.assertIn(f"#{it['code']}", c.handle("/待发"))
        self.assertIn("没有等确认的 #000", c.handle("/发布 000"))
        self.assertEqual(c.handle(f"/发布 #{it['code']}"), f"#{it['code']} 已发到「浮屿用户交流群」。")
        self.assertEqual(c.handle("/取消"), "没有等确认的公告。")


if __name__ == "__main__":
    unittest.main()
