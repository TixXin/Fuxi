"""对外发送 API：口令、同步/异步发送、模板、定时发送（起真的 HTTP 服务）。"""

import base64
import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from fuxi_bridge import api, events, schedule, settings

TOKEN = "api-t0ken"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        b = mock.Mock()
        d = tempfile.mkdtemp()
        b.work = d
        b.events = events.Events()
        b.cfg = settings.Settings(os.path.join(d, "s.json"), {})
        b.name_of.side_effect = lambda x: {"wxid_a": "阿青"}.get(x, x)
        b.reader.find_chats.side_effect = lambda q: [{"username": "wxid_a"}] if q == "阿青" else []
        b.sent = []

        def send(job):
            b.sent.append(job)
            if job.image:
                assert os.path.exists(job.image)
            job.ok, job.verified = True, True
            return job
        b.send.side_effect = send
        b.templates = schedule.Templates(os.path.join(d, "t.json"))
        b.scheduler = schedule.Scheduler(b, d)
        b.jobs = schedule.AsyncJobs(b)
        cls.b, cls.port = b, free_port()
        env = {"FUXI_API_TOKEN": TOKEN, "FUXI_API_HOST": "127.0.0.1", "FUXI_API_PORT": str(cls.port)}
        with mock.patch.dict(os.environ, env):
            threading.Thread(target=api.serve, args=(b,), daemon=True).start()
            for _ in range(100):
                try:
                    socket.create_connection(("127.0.0.1", cls.port), timeout=0.1).close()
                    break
                except OSError:
                    time.sleep(0.02)

    def req(self, method, path, body=None, token=TOKEN):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Authorization": f"Bearer {token}"} if token else {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = json.loads(r.read() or b"{}")
        c.close()
        return r.status, out

    def setUp(self):
        self.b.sent.clear()

    def test_auth(self):
        self.assertEqual(self.req("GET", "/schedule", token="bad")[0], 401)

    def test_send_sync_with_template(self):
        self.b.templates.put("问候", "{who}，在吗")
        code, body = self.req("POST", "/send", {"to": "阿青", "template": "问候", "vars": {"who": "阿青"}})
        self.assertEqual((code, body["verified"]), (200, True))
        self.assertEqual(self.b.sent[-1].text, "阿青，在吗")
        self.assertEqual(self.req("POST", "/send", {"to": "阿青", "template": "问候"})[0], 400)    # 缺变量
        code, body = self.req("POST", "/send", {"to": "没这人", "text": "x"})
        self.assertEqual(code, 409)
        _, body = self.req("GET", "/templates")
        self.assertEqual(body["items"][0]["vars"], ["who"])

    def test_send_async(self):
        img = base64.b64encode(b"\x89PNG").decode()
        code, body = self.req("POST", "/send", {"chat_id": "wxid_a", "text": "图来了", "image_base64": img, "async": True})
        self.assertEqual(code, 202)
        jid = body["job"]["id"]
        for _ in range(100):
            _, j = self.req("GET", f"/jobs/{jid}")
            if j["job"]["status"] == "done":
                break
            time.sleep(0.02)
        self.assertEqual((j["job"]["ok"], j["job"]["verified"]), (True, True))
        self.assertEqual([bool(x.image) for x in self.b.sent], [True, False])
        self.assertFalse(os.path.exists(self.b.sent[0].image))            # 临时图片发完就删
        self.assertEqual(self.req("GET", "/jobs/nope")[0], 404)

    def test_schedule(self):
        code, body = self.req("POST", "/schedule", {"to": "阿青", "text": "提醒", "in_minutes": 30})
        self.assertEqual((code, body["item"]["status"], body["item"]["name"]), (200, "pending", "阿青"))
        sid = body["item"]["id"]
        self.assertEqual(self.req("POST", "/schedule", {"to": "阿青", "text": "x", "at": "明天"})[0], 400)
        _, body = self.req("GET", "/schedule")
        self.assertIn(sid, [i["id"] for i in body["pending"]])
        code, body = self.req("POST", f"/schedule/{sid}/cancel", {})
        self.assertEqual((code, body["item"]["status"]), (200, "cancelled"))
        self.assertEqual(self.req("POST", f"/schedule/{sid}/cancel", {})[0], 400)
