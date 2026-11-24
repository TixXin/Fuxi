"""定时发送、模板、异步发送：到点发、错过不补发、重启不丢、缺变量不发半截。"""

import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from fuxi_bridge import events, schedule
from fuxi_bridge.schedule import ScheduleError, render


def fake_bridge(ok=True, error=""):
    b = mock.Mock()
    b.events = events.Events()
    b.name_of.side_effect = lambda x: {"wxid_a": "阿青", "g@chatroom": "测试群"}.get(x, x)
    b.sent = []

    def send(job):
        b.sent.append(job)
        job.ok, job.verified, job.error = ok, ok or None, error
        return job
    b.send.side_effect = send
    return b


class RenderTest(unittest.TestCase):
    def test_vars_and_builtins(self):
        ts = time.mktime((2026, 11, 25, 9, 5, 0, 0, 0, -1))
        self.assertEqual(render("{name}，{date}{weekday} {time} 开会", {"name": "阿青"}, ts), "阿青，11月25日周三 09:05 开会")
        self.assertEqual(render("集合 {{不是变量}}"), "集合 {不是变量}")
        with self.assertRaises(ScheduleError) as c:
            render("{name} 和 {who}")
        self.assertIn("name、who", str(c.exception))

    def test_templates_store(self):
        d = tempfile.mkdtemp()
        t = schedule.Templates(os.path.join(d, "t.json"))
        t.put("早安", "早，{name}！今天{weekday}")
        self.assertEqual(t.list(), [{"name": "早安", "text": "早，{name}！今天{weekday}", "vars": ["name"]}])
        self.assertEqual(schedule.Templates(os.path.join(d, "t.json")).get("早安"), "早，{name}！今天{weekday}")
        with self.assertRaises(ScheduleError):
            t.put("a/b", "x")
        t.delete("早安")
        with self.assertRaises(ScheduleError):
            t.get("早安")


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.b = fake_bridge()
        self.b.templates = schedule.Templates(os.path.join(self.dir, "t.json"))
        self.s = schedule.Scheduler(self.b, self.dir)

    def test_sends_when_due_and_persists(self):
        now = time.time()
        it = self.s.add("wxid_a", now + 60, text="到点了", source="api")
        self.assertEqual((it["status"], it["name"]), ("pending", "阿青"))
        self.s.tick(now)                                   # 还没到
        self.assertEqual(self.b.sent, [])
        again = schedule.Scheduler(self.b, self.dir)       # 重启后还在
        self.assertEqual(again.list()["pending"][0]["id"], it["id"])
        self.s.tick(now + 61)
        self.assertEqual([(j.chat, j.text, j.source) for j in self.b.sent], [("wxid_a", "到点了", "schedule")])
        self.assertEqual(self.s.list()["recent"][0]["status"], "sent")
        self.s.tick(now + 120)                             # 不重发
        self.assertEqual(len(self.b.sent), 1)

    def test_missed_is_not_sent_late(self):
        now = time.time()
        self.s.add("wxid_a", now + 10, text="早安")
        self.s.tick(now + 10 + schedule.GRACE + 1)
        self.assertEqual(self.b.sent, [])
        r = self.s.list()["recent"][0]
        self.assertEqual(r["status"], "missed")
        self.b.tell_owner.assert_called_once()

    def test_template_rendered_at_send_time_and_checked_up_front(self):
        self.b.templates.put("会议", "{who}，{time} 开会")
        at = time.mktime((2026, 11, 25, 14, 30, 0, 0, 0, -1))
        with mock.patch.object(schedule.time, "time", lambda: at - 3600):
            with self.assertRaises(ScheduleError):
                self.s.add("g@chatroom", at, template="会议")              # 缺 who：现在就拒绝
            self.s.add("g@chatroom", at, template="会议", vars={"who": "大家"})
        self.s.tick(at + 1)
        self.assertEqual(self.b.sent[-1].text, "大家，14:30 开会")

    def test_image_then_text_and_cancel(self):
        now = time.time()
        it = self.s.add("wxid_a", now + 5, text="看图", image=b"\x89PNG", ext=".png")
        img = [i for i in self.s.items if i["id"] == it["id"]][0]["image"]
        self.assertTrue(os.path.exists(img))
        self.s.tick(now + 6)
        self.assertEqual([bool(j.image) for j in self.b.sent], [True, False])
        self.assertFalse(os.path.exists(img))                            # 发完删图
        it2 = self.s.add("wxid_a", now + 100, text="取消我")
        self.assertEqual(self.s.cancel(it2["id"])["status"], "cancelled")
        with self.assertRaises(ScheduleError):
            self.s.cancel(it2["id"])

    def test_failure_tells_owner(self):
        b = fake_bridge(ok=False, error="发送已熔断：x")
        b.templates = self.b.templates
        s = schedule.Scheduler(b, tempfile.mkdtemp())
        now = time.time()
        s.add("wxid_a", now + 1, text="x")
        s.tick(now + 2)
        self.assertEqual(s.list()["recent"][0]["status"], "failed")
        self.assertIn("没发出去", b.tell_owner.call_args[0][0])

    def test_validation(self):
        now = time.time()
        with self.assertRaises(ScheduleError):
            self.s.add("wxid_a", now - 3600, text="过去")
        with self.assertRaises(ScheduleError):
            self.s.add("wxid_a", now + 91 * 86400, text="太远")
        with self.assertRaises(ScheduleError):
            self.s.add("wxid_a", now + 60, text=" ")

    def test_restart_mid_send_marks_failed(self):
        now = time.time()
        it = self.s.add("wxid_a", now + 1, text="x")
        data = json.load(open(os.path.join(self.dir, "schedule.json")))
        data[0]["status"] = "sending"
        json.dump(data, open(os.path.join(self.dir, "schedule.json"), "w"))
        s2 = schedule.Scheduler(self.b, self.dir)
        self.assertEqual(s2.list()["recent"][0]["status"], "failed")
        self.assertEqual(s2.add("wxid_a", now + 60, text="y")["id"], it["id"] + 1)   # 编号接着往下


class AsyncJobsTest(unittest.TestCase):
    def test_submit_and_get(self):
        from fuxi_bridge.app import SendJob
        b = fake_bridge()
        jobs = schedule.AsyncJobs(b, keep=2)
        cleaned = threading.Event()
        rec = jobs.submit("wxid_a", lambda: [SendJob(chat="wxid_a", text="hi")], "api", cleaned.set)
        self.assertTrue(cleaned.wait(2))
        for _ in range(50):
            if jobs.get(rec["id"])["status"] == "done":
                break
            time.sleep(0.02)
        self.assertEqual({k: jobs.get(rec["id"])[k] for k in ("status", "ok", "verified")},
                         {"status": "done", "ok": True, "verified": True})
        with self.assertRaises(ScheduleError):
            jobs.get("nope")


class ParseAtTest(unittest.TestCase):
    def test_formats(self):
        from fuxi_bridge import handlers
        ts = time.mktime((2026, 11, 25, 9, 0, 0, 0, 0, -1))
        self.assertEqual(handlers.parse_at("2026-11-25 09:00"), ts)
        self.assertEqual(handlers.parse_at("2026-11-25T09:00:00"), ts)
        self.assertEqual(handlers.parse_at(ts), ts)
        with self.assertRaises(handlers.HandlerError):
            handlers.parse_at("明天早上")
