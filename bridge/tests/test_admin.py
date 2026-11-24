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
            return r.status, raw.decode(), r

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
