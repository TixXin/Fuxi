"""网页管理台：口令、防跨站、接口、服务端推送、静态文件；事件中心的计数与历史。"""

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

from fuxi_bridge import admin, events, handlers, settings, timing

TOKEN = "t0ken"


def fake_bridge():
    b = mock.Mock()
    b.events = events.Events()
    b.problems.return_value = []
    b.halted, b.paused, b.quiet = "", False, ""
    b.in_quiet_hours.return_value = False
    b.sender.window_state.return_value = "ok"
    b.keyring.keys = {"a": 1, "b": 2}
    b.kernel_ok_at = b.last_poll_ok_at = b.started = time.time()
    b.allowlist.items = ["filehelper", "g@chatroom"]
    b.latency = timing.LatencyStats()
    b.announcer.pending.return_value = [{"code": "482", "to": "浮屿用户交流群", "created": time.time()}]
    b.cfg = settings.Settings(os.path.join(tempfile.mkdtemp(), "s.json"), {})
    b.get_setting.side_effect = lambda k: b.cfg.get(k)
    b.set_setting.side_effect = lambda k, v: b.cfg.set(k, v)

    def set_paused(on):
        b.paused = on
    b.set_paused.side_effect = set_paused
    return b


class AdminServerTest(unittest.TestCase):
    def setUp(self):
        self.b = fake_bridge()
        self.static = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.static, "assets"))
        with open(os.path.join(self.static, "index.html"), "w") as f:
            f.write("<!doctype html><title>浮汐</title>")
        with open(os.path.join(self.static, "assets", "app-abc.js"), "w") as f:
            f.write("console.log(1)")
        with open(os.path.join(os.path.dirname(self.static), "secret.txt"), "w") as f:
            f.write("不该被读到")
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), admin.make_handler(self.b, TOKEN, self.static, ping=0.2))
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def req(self, method, path, body=None, headers=None, token=TOKEN, csrf=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"127.0.0.1:{self.port}"}
        if token is not None:
            h["X-Fuxi-Admin"] = token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
            if csrf:
                h["X-Requested-With"] = "fuxi-admin"
        h.update(headers or {})
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw), r
        except ValueError:
            return r.status, raw.decode(errors="replace"), r

    def test_token_required(self):
        for tok in (None, "", "wrong"):
            code, body, _ = self.req("GET", "/api/overview", token=tok)
            self.assertEqual(code, 401, tok)
            self.assertFalse(body["ok"])
        self.assertEqual(self.req("GET", "/", token=None)[0], 401)           # 静态文件也要口令

    def test_overview(self):
        self.b.events.publish("message", {"chat": "g@chatroom", "count": 3, "replies": ["好"]})
        self.b.events.publish("handoff", {"chat": "g@chatroom", "chat_name": "群", "sender": "小明", "question": "能导出吗"})
        code, body, r = self.req("GET", "/api/overview")
        self.assertEqual(code, 200)
        self.assertEqual(r.getheader("Cache-Control"), "private, no-store")
        self.assertEqual((body["state"], body["text"]), ("ok", "正常回复中"))
        self.assertEqual(body["services"]["wechat"]["detail"], "2 个库已解密")
        self.assertEqual((body["today"]["received"], body["today"]["replied"], body["today"]["handoffs"]), (3, 1, 1))
        self.assertEqual([t["kind"] for t in body["todo"]], ["announce", "handoff"])
        self.b.halted = "发错会话"
        self.assertEqual(self.req("GET", "/api/overview")[1]["state"], "bad")

    def test_writes_need_csrf_headers_and_same_origin(self):
        self.assertEqual(self.req("POST", "/api/pause", {}, csrf=False)[0], 403)
        self.assertFalse(self.b.paused)
        code, _, _ = self.req("POST", "/api/pause", {}, headers={"Origin": "https://evil.example"})
        self.assertEqual(code, 403)
        code, body, _ = self.req("POST", "/api/pause", {}, headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual((code, body["paused"]), (200, True))
        self.b.halted = "x"
        code, body, _ = self.req("POST", "/api/resume", {})
        self.assertEqual((body["paused"], body["halted"], body["was"]), (False, "", "x"))
        self.b.sender.forget_chat.assert_called_once()

    def test_settings(self):
        code, body, _ = self.req("GET", "/api/settings")
        self.assertIn("vision", [i["key"] for i in body["items"]])
        code, body, _ = self.req("POST", "/api/settings", {"rate_chat": 7})
        self.assertEqual(code, 200)
        self.assertEqual(self.b.cfg.get("rate_chat"), 7)
        code, body, _ = self.req("POST", "/api/settings", {"nope": 1})
        self.assertEqual((code, body["ok"]), (400, False))
        code, body, _ = self.req("POST", "/api/settings", {"quiet": "半夜"})
        self.assertEqual((code, body["key"]), (400, "quiet"))

    def test_messages_paging(self):
        for i in range(5):
            self.b.events.publish("message", {"chat": "c", "text": f"m{i}"})
        _, body, _ = self.req("GET", "/api/messages?limit=2")
        self.assertEqual([e["text"] for e in body["items"]], ["m4", "m3"])
        _, body, _ = self.req("GET", f"/api/messages?limit=10&before={body['items'][-1]['id']}")
        self.assertEqual([e["text"] for e in body["items"]], ["m2", "m1", "m0"])

    def test_sse(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/events", headers={"X-Fuxi-Admin": TOKEN})
        r = c.getresponse()
        self.assertEqual(r.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        self.assertEqual(r.readline(), b"retry: 3000\n")
        r.readline()
        self.assertEqual(r.readline(), b": ping\n")                         # 没事件时保活
        r.readline()
        self.b.events.publish("message", {"chat": "c", "text": "你好"})
        lines = [r.readline() for _ in range(3)]
        self.assertTrue(lines[1].startswith(b"event: message"))
        self.assertEqual(json.loads(lines[2][6:])["text"], "你好")
        r.close()
        c.close()
        deadline = time.time() + 3
        while self.b.events._subs and time.time() < deadline:             # 断开后取消订阅
            time.sleep(0.05)
        self.assertFalse(self.b.events._subs)

    def test_static(self):
        code, body, r = self.req("GET", "/")
        self.assertIn("浮汐", body)
        self.assertEqual(r.getheader("Cache-Control"), "private, no-store")
        code, body, r = self.req("GET", "/assets/app-abc.js")
        self.assertIn("immutable", r.getheader("Cache-Control"))
        code, body, _ = self.req("GET", "/settings/whatever")                 # 前端路由回退到首页
        self.assertIn("浮汐", body)
        code, body, _ = self.req("GET", "/../secret.txt")                     # 不能跳出目录
        self.assertNotIn("不该", body)
        self.assertEqual(self.req("GET", "/api/nope")[0], 404)

    def test_knowledge(self):
        from fuxi_bridge import knowledge as kn
        k = object.__new__(kn.Knowledge)
        k.meta = {"repo": "o/r", "synced_at": 1.0, "files": 2}
        k.index = kn.Index([kn.Chunk("docs/features/screenshot.md", "截图 › 贴图", "按 F3 把截图贴到屏幕上"),
                            kn.Chunk("docs/releases/v1.md", "v1", "修复若干问题")]
                           + [kn.Chunk("x", f"其他{i}", f"待办日历音乐{i}") for i in range(20)])
        k.floor = 1.0
        done = threading.Event()
        k.sync = lambda repo: (done.wait(2), "同步完成")[1]
        self.b.knowledge = k
        self.b.name_of.side_effect = lambda x: "我" if x == "wxid_z" else x
        _, body, _ = self.req("GET", "/api/knowledge")
        self.assertEqual((body["chunks"], body["sources"]["功能说明"]), (22, 1))
        code, body, _ = self.req("GET", "/api/knowledge/search?q=%E6%88%AA%E5%9B%BE%E6%80%8E%E4%B9%88%E8%B4%B4")
        self.assertEqual(code, 200)
        self.assertEqual(body["candidates"][0]["title"], "截图 › 贴图")
        self.assertEqual(body["picked"][0], "截图 › 贴图")
        self.assertIsNone(body["judged"])
        self.assertEqual(self.req("GET", "/api/knowledge/search?q=")[0], 400)
        self.assertEqual(self.req("POST", "/api/knowledge/sync", {})[0], 202)
        self.assertEqual(self.req("POST", "/api/knowledge/sync", {})[0], 409)     # 同步中不能再点
        q = self.b.events.subscribe()
        done.set()
        self.assertEqual(q.get(timeout=3)["kind"], "knowledge")
        self.b.cfg.set("owners", ["wxid_z", "某人"])
        _, body, _ = self.req("GET", "/api/settings")
        owners = next(i for i in body["items"] if i["key"] == "owners")
        self.assertEqual(owners["labels"], {"wxid_z": "我"})

    def test_allow(self):
        self.b.name_of.side_effect = lambda x: {"g@chatroom": "浮屿用户交流群", "wxid_a": "阿青"}.get(x, x)
        self.b.resolve_chat.side_effect = lambda n: "g@chatroom"
        self.b.cfg.set("assistant_groups", ["浮屿用户交流群"])
        saved = {}
        self.b.allowlist.set.side_effect = lambda items, names: (saved.update(names), setattr(self.b.allowlist, "items", items))
        _, body, _ = self.req("GET", "/api/allow")
        g = next(i for i in body["items"] if i["chat_id"] == "g@chatroom")
        self.assertEqual((g["name"], g["group"], g["roles"]), ("浮屿用户交流群", True, ["产品助手"]))
        code, body, _ = self.req("POST", "/api/allow", {"add": ["wxid_a"]})
        self.assertEqual(code, 200)
        self.assertIn("wxid_a", self.b.allowlist.items)
        code, body, _ = self.req("POST", "/api/allow", {"remove": ["wxid_a"]})
        self.assertEqual(code, 400)                                           # 移出要确认
        self.assertIn("wxid_a", self.b.allowlist.items)
        code, _, _ = self.req("POST", "/api/allow", {"remove": ["wxid_a"], "confirm": True})
        self.assertNotIn("wxid_a", self.b.allowlist.items)
        self.assertEqual(self.req("POST", "/api/allow", {"add": ["../x y"]})[0], 400)
        self.b.reader.sessions.return_value = [{"username": "gh_news", "last_timestamp": 3},
                                               {"username": "wxid_a", "last_timestamp": 2},
                                               {"username": "g@chatroom", "last_timestamp": 1}]
        _, body, _ = self.req("GET", "/api/chats/recent")
        self.assertEqual([(i["chat_id"], i["allowed"]) for i in body["items"]], [("wxid_a", False), ("g@chatroom", True)])
        self.assertNotIn("summary", body["items"][0])                         # 不带消息摘要
        self.b.reader.contacts.return_value = [{"username": "wxid_a", "name": "阿青", "remark": "", "alias": ""}]
        _, body, _ = self.req("GET", "/api/chats?q=%E9%98%BF")
        self.assertEqual(body["items"][0]["chat_id"], "wxid_a")

    def test_announce(self):
        from fuxi_bridge.announce import Announcer
        self.b.name_of.side_effect = lambda x: "浮屿用户交流群"
        self.b.send.side_effect = lambda job: setattr(job, "ok", True) or setattr(job, "error", "") or job
        self.b.allowlist.allows.return_value = True
        a = Announcer(self.b, tempfile.mkdtemp())
        self.b.announcer = a
        it, _ = a.create("fuyu-v1", "g@chatroom", "新版本来了", b"\x89PNG fake", ".png")
        _, body, _ = self.req("GET", "/api/announce")
        self.assertEqual((body["pending"][0]["code"], body["pending"][0]["has_image"]), (it["code"], True))
        code, data, r = self.req("GET", f"/api/announce/{it['code']}/image")
        self.assertEqual((code, r.getheader("Content-Type")), (200, "image/png"))
        self.assertEqual(self.req("POST", f"/api/announce/{it['code']}/publish", {})[0], 400)   # 要确认
        code, body, _ = self.req("POST", f"/api/announce/{it['code']}/publish", {"confirm": True})
        self.assertEqual((code, body["item"]["status"]), (200, "sent"))
        self.assertEqual(self.req("POST", f"/api/announce/{it['code']}/cancel", {})[0], 400)    # 已经发了
        self.assertEqual(self.req("GET", f"/api/announce/{it['code']}/image")[0], 404)

    def test_feedback(self):
        from fuxi_bridge.feedback import Feedback
        fb = object.__new__(Feedback)
        fb.items = [{"id": 1, "category": "bug", "title": "全屏时提醒看不到", "detail": "", "platform": "macOS",
                     "status": "new", "first_seen": time.time(), "last_seen": time.time(), "count": 2,
                     "reporters": [{"wxid": "wxid_a", "name": "阿青"}], "quotes": ["看不到提醒"], "issue": None}]
        fb.meta, fb.fuyu = {}, None
        fb._lock = threading.RLock()
        fb._save = lambda: None
        done = threading.Event()
        fb.classify = lambda: (done.wait(2), "整理了 3 条消息")[1]
        self.b.feedback = fb
        _, body, _ = self.req("GET", "/api/feedback")
        it = body["items"][0]
        self.assertEqual((it["category_zh"], it["reporters"], it["progress"]), ("问题", ["阿青"], "新问题，GitHub 上还没有对应 issue"))
        self.assertEqual(self.req("POST", "/api/feedback/1/ignore", {})[1]["status"], "ignored")
        self.assertEqual(self.req("GET", "/api/feedback")[1]["items"], [])
        self.assertEqual(len(self.req("GET", "/api/feedback?ignored=1")[1]["items"]), 1)
        self.assertEqual(self.req("POST", "/api/feedback/1/unignore", {})[1]["status"], "new")
        self.assertEqual(self.req("POST", "/api/feedback/9/ignore", {})[0], 404)
        self.assertEqual(self.req("POST", "/api/feedback/classify", {})[0], 202)
        self.assertEqual(self.req("POST", "/api/feedback/classify", {})[0], 409)
        done.set()


    def test_persona(self):
        calls = []

        def call(method, path, body=None, timeout=None):
            calls.append((method, path, body))
            if path == "/api/persona" and method == "GET":
                return 200, {"ok": True, "persona": {"name": "浮汐", "customPrompt": "x", "apiKey": "不该出去"},
                             "warnings": [], "llm": {"model": "m"}}
            if path == "/api/persona/presets":
                return 200, {"ok": True, "presets": [{"id": "persona-standard", "name": "标准", "file": "/a/b"}]}
            if path == "/api/persona" and body.get("customPrompt") == "太长":
                return 400, {"ok": False, "error": "人设太长：约 99999 token", "tokenCheck": {"tokens": 99999}}
            return 200, {"ok": True, "warnings": ["提示"], "applied": {"id": "persona-standard"}}
        self.b.kernel.call.side_effect = call
        _, body, _ = self.req("GET", "/api/persona")
        self.assertEqual(body["persona"], {"name": "浮汐", "customPrompt": "x"})        # 只放出人设字段
        self.assertEqual(body["presets"][0], {"id": "persona-standard", "name": "标准", "description": None, "chars": None})
        code, body, _ = self.req("POST", "/api/persona", {"patch": {"name": "小汐", "apiKey": "k"}})
        self.assertEqual((code, body["warnings"]), (200, ["提示"]))
        self.assertEqual(calls[-1], ("POST", "/api/persona", {"name": "小汐"}))        # 不认识的键不转发
        code, body, _ = self.req("POST", "/api/persona", {"patch": {"customPrompt": "太长"}})
        self.assertEqual((code, body["error"], body["tokenCheck"]), (400, "人设太长：约 99999 token", {"tokens": 99999}))
        self.assertEqual(self.req("POST", "/api/persona", {"patch": {}})[0], 400)
        self.assertEqual(self.req("POST", "/api/persona/preset", {"id": "persona-standard"})[0], 400)   # 要确认
        self.assertEqual(self.req("POST", "/api/persona/preset", {"id": "../x", "confirm": True})[0], 400)
        code, body, _ = self.req("POST", "/api/persona/preset", {"id": "persona-standard", "confirm": True})
        self.assertEqual((code, body["applied"]["id"]), (200, "persona-standard"))

    def test_send(self):
        self.b.name_of.side_effect = lambda x: "阿青"
        self.b.sender.probe_open.return_value = 1.234
        code, body, _ = self.req("POST", "/api/send", {"chat_id": "wxid_a", "dry_run": True})
        self.assertEqual((code, body["seconds"], body["name"]), (200, 1.23, "阿青"))
        self.b.send.assert_not_called()
        self.assertEqual(self.req("POST", "/api/send", {"chat_id": "wxid_a", "text": "在吗"})[0], 400)   # 要确认
        self.assertEqual(self.req("POST", "/api/send", {"chat_id": "a b", "text": "在吗", "confirm": True})[0], 400)
        self.assertEqual(self.req("POST", "/api/send", {"chat_id": "wxid_a", "text": " ", "confirm": True})[0], 400)
        self.b.send.side_effect = lambda job: setattr(job, "ok", True) or setattr(job, "verified", True) or job
        code, body, _ = self.req("POST", "/api/send", {"chat_id": "wxid_a", "text": "在吗", "confirm": True})
        self.assertEqual((code, body["verified"]), (200, True))
        job = self.b.send.call_args[0][0]
        self.assertEqual((job.chat, job.text, job.source), ("wxid_a", "在吗", "admin"))
        sent = self.b.events.recent(kind="sent")[0]
        self.assertEqual((sent["chat_name"], sent["text"], sent["ok"]), ("阿青", "在吗", True))
        self.assertEqual(self.b.events.counts()["received"], 0)                   # 手动发送不算进今天的数字
        self.b.send.side_effect = lambda job: setattr(job, "error", "发送已熔断：x") or job
        code, body, _ = self.req("POST", "/api/send", {"chat_id": "wxid_a", "text": "在吗", "confirm": True})
        self.assertEqual((code, body["error"]), (502, "没发出去：发送已熔断：x"))


    def test_schedule_and_templates(self):
        from fuxi_bridge import schedule
        d = tempfile.mkdtemp()
        self.b.templates = schedule.Templates(os.path.join(d, "t.json"))
        self.b.scheduler = schedule.Scheduler(self.b, d)
        self.b.name_of.side_effect = lambda x: "阿青"
        code, body, _ = self.req("POST", "/api/templates", {"name": "问候", "text": "{who}，{weekday}好"})
        self.assertEqual((code, body["items"][0]["vars"]), (200, ["who"]))
        _, body, _ = self.req("POST", "/api/templates/render", {"name": "问候", "vars": {"who": "阿青"}})
        self.assertTrue(body["text"].startswith("阿青，周"))
        self.assertEqual(self.req("POST", "/api/templates/render", {"name": "问候"})[0], 400)   # 缺变量
        at = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 3600))
        self.assertEqual(self.req("POST", "/api/schedule", {"chat_id": "wxid_a", "text": "x", "at": at})[0], 400)  # 要确认
        code, body, _ = self.req("POST", "/api/schedule", {"chat_id": "wxid_a", "text": "x", "at": at, "confirm": True})
        self.assertEqual((code, body["item"]["status"]), (200, "pending"))
        _, body, _ = self.req("GET", "/api/schedule")
        sid = body["pending"][0]["id"]
        code, body, _ = self.req("POST", f"/api/schedule/{sid}/cancel", {})
        self.assertEqual(body["item"]["status"], "cancelled")
        self.assertEqual(self.req("POST", "/api/templates/delete", {"name": "问候"})[1]["items"], [])

class EventsTest(unittest.TestCase):
    def test_counts_reset_daily_and_slow_subscriber(self):
        e = events.Events(keep=3)
        q = e.subscribe()
        for i in range(250):                                                 # 订阅方不读：不阻塞、不报错
            e.publish("message", {"count": 1, "replies": ["x"] if i % 2 else []})
        self.assertEqual(q.qsize(), 200)
        self.assertEqual(len(e.recent(limit=10)), 3)
        c = e.counts()
        self.assertEqual((c["received"], c["replied"]), (250, 125))
        e.publish("status", {"paused": True})                               # 状态事件不进历史
        self.assertEqual(len(e.recent(limit=10)), 3)
        with mock.patch("fuxi_bridge.events._day", return_value="2099-01-01"):
            self.assertEqual(e.counts()["received"], 0)


class HandlersTest(unittest.TestCase):
    def test_overview_states(self):
        b = fake_bridge()
        b.paused = True
        self.assertEqual(handlers.overview(b)["state"], "warn")
        b.paused = False
        b.in_quiet_hours.return_value = True
        self.assertEqual(handlers.overview(b)["text"], "静默时段中，暂不回复")
        b.kernel_ok_at = time.time() - 600
        self.assertEqual(handlers.overview(b)["services"]["kernel"]["state"], "bad")


if __name__ == "__main__":
    unittest.main()


class StatsTest(unittest.TestCase):
    def test_reply_stats(self):
        from fuxi_bridge.messages import Message
        b = fake_bridge()
        b.self_wxid = "wxid_bot"
        b.self_names.return_value = ["浮汐"]
        b.name_of.side_effect = lambda x: x
        b.allowlist.items = ["*", "filehelper", "g@chatroom"]
        t = time.time() - 3600

        def m(i, sender, text, dt, src=""):
            return Message(chat="g@chatroom", local_id=i, sort_seq=i, type="text", sender=sender,
                           is_self=sender == "wxid_bot", create_time=int(t + dt), text=text, source=src, shard="s")
        b.reader.history.return_value = [
            m(1, "wxid_a", "@浮汐 怎么贴图", 0), m(2, "wxid_bot", "按 F3", 8), m(3, "wxid_bot", "就贴上去了", 10),
            m(4, "wxid_b", "今天好冷", 100), m(5, "wxid_bot", "是啊", 130),
        ]
        handlers._STATS.clear()
        r = handlers.reply_stats(b, 7)
        self.assertEqual(len(r["items"]), 1)                                 # 跳过 * 和文件传输助手
        s = r["items"][0]
        self.assertEqual((s["incoming"], s["senders"], s["turns"], s["bubbles"]), (2, 2, 2, 3))
        self.assertEqual((s["unprompted"], s["multi"], s["max_run"]), (1, 1, 2))  # 「是啊」没被 @
        self.assertEqual((s["latency_p50"], s["len_max"]), (30, 5))
        b.reader.history.return_value = []
        self.assertIs(handlers.reply_stats(b, 7), r)                          # 10 分钟内用缓存

    def test_scaled_kernel_setting(self):
        from fuxi_bridge.app import Bridge
        b = object.__new__(Bridge)
        b.kernel = mock.Mock()
        b.kernel.config.return_value = {"group": {"freeChatChance": 0.06}}
        self.assertEqual(b.get_setting("free_chat_pct"), 6)
        self.assertEqual(b.set_setting("free_chat_pct", "3"), 3)
        b.kernel.patch_config.assert_called_with(("group", "freeChatChance"), 0.03)
        with self.assertRaises(ValueError):
            b.set_setting("free_chat_pct", "50")
