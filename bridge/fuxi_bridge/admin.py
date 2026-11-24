"""网页管理台的服务端：发前端静态文件、管理接口、服务端推送（SSE）。

  宿主 127.0.0.1:8791 → OpenResty（Basic 认证 + 补 X-Fuxi-Admin 口令头）→ 浏览器。
  架构、接口和安全约定见 docs/admin.md。

  GET  /api/overview                 概览
  GET  /api/settings                 全部行为设置；POST {key: 值, ...} 修改
  GET  /api/messages?before=&limit=  对话流历史（内存里最近 500 条）
  GET  /api/events                   SSE：message、handoff、settings、status、knowledge、allow、announce、feedback
  POST /api/pause {} | /api/resume {}  暂停 / 恢复（恢复顺带解除熔断）
  GET  /api/knowledge                资料库状态；POST /api/knowledge/sync 后台同步一次
  GET  /api/knowledge/search?q=&judge=1  试搜：候选分数、会附给内核的段落（judge=1 再让模型筛）
  GET  /api/allow | /api/chats/recent | /api/chats?q=   白名单、最近的会话、搜会话
  POST /api/allow {add:[chat_id], remove:[chat_id], confirm}   移出要 confirm
  GET  /api/announce                 待发和最近的公告；GET /api/announce/<编号>/image 预览图
  POST /api/announce/<编号>/publish {confirm: true} | /cancel
  GET  /api/feedback?days=&ignored=1  群反馈条目；GET /api/feedback/preview/digest|group 预览摘要/小结
  POST /api/feedback/<id>/ignore | /unignore；POST /api/feedback/classify 后台立即整理

所有请求要带 X-Fuxi-Admin 口令；非 GET 还要 Content-Type: application/json + X-Requested-With: fuxi-admin，
Origin（有的话）要和 Host 一致。
"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import os
import queue
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from . import handlers

if TYPE_CHECKING:
    from .app import Bridge

log = logging.getLogger("fuxi.admin")
MAX_BODY = 1 << 20
PING = 15


def make_handler(bridge: "Bridge", token: str, static_dir: str, ping: float = PING):
    static_root = os.path.realpath(static_dir)

    class H(BaseHTTPRequestHandler):
        server_version = "fuxi-admin"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):   # 只记方法、路径、状态码，不记请求体
            log.debug("%s", fmt % args)

        # ---- 回复 ----
        def _send(self, code: int, body: bytes, ctype: str, cache: str = "private, no-store") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj: dict) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _err(self, code: int, msg: str, **extra) -> None:
            self._json(code, {"ok": False, "error": msg, **extra})

        # ---- 认证与防跨站 ----
        def _auth(self) -> bool:
            got = self.headers.get("X-Fuxi-Admin", "")
            if token and hmac.compare_digest(got.encode(), token.encode()):
                return True
            self._err(401, "未授权")
            return False

        def _write_ok(self) -> bool:
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json" or self.headers.get("X-Requested-With") != "fuxi-admin":
                self._err(403, "缺少防跨站请求头")
                return False
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != (self.headers.get("Host") or ""):
                self._err(403, "来源不符")
                return False
            return True

        def _body(self) -> dict | None:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self._err(413, "请求太大")
                return None
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = None
            if not isinstance(body, dict):
                self._err(400, "JSON 无效")
                return None
            return body

        # ---- 路由 ----
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._auth():
                return
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if not u.path.startswith("/api/"):
                return self._static(u.path)
            if u.path == "/api/overview":
                return self._json(200, {"ok": True, **handlers.overview(bridge)})
            if u.path == "/api/settings":
                return self._json(200, {"ok": True, "items": handlers.settings_items(bridge)})
            if u.path == "/api/messages":
                before = (qs.get("before") or [""])[0]
                limit = (qs.get("limit") or ["50"])[0]
                return self._json(200, {"ok": True, "items": bridge.events.recent(
                    before=int(before) if before.isdigit() else None,
                    limit=max(1, min(200, int(limit) if limit.isdigit() else 50)))})
            if u.path == "/api/events":
                return self._sse()
            try:
                got = self._get_more(u.path, qs)
            except handlers.HandlerError as e:
                return self._err(e.code, str(e))
            if got is not None:
                return
            if u.path == "/api/knowledge":
                return self._json(200, {"ok": True, **handlers.knowledge_info(bridge)})
            if u.path == "/api/knowledge/faq":
                return self._json(200, {"ok": True, "items": handlers.knowledge_faq_list(bridge)})
            if u.path == "/api/knowledge/search":
                try:
                    return self._json(200, {"ok": True, **handlers.knowledge_search(
                        bridge, (qs.get("q") or [""])[0], (qs.get("judge") or [""])[0] == "1")})
                except handlers.HandlerError as e:
                    return self._err(e.code, str(e))
            self._err(404, "没有这个接口")

        def _get_more(self, path: str, qs: dict):
            """C 期的读接口；处理了返回 True，不认识返回 None。"""
            if path == "/api/allow":
                return self._json(200, {"ok": True, "items": handlers.allow_list(bridge)}) or True
            if path == "/api/chats/recent":
                return self._json(200, {"ok": True, "items": handlers.recent_chats(bridge)}) or True
            if path == "/api/chats":
                return self._json(200, {"ok": True, "items": handlers.search_chats(bridge, (qs.get("q") or [""])[0])}) or True
            if path == "/api/announce":
                return self._json(200, {"ok": True, **handlers.announce_list(bridge)}) or True
            m = re.fullmatch(r"/api/announce/(\d{3})/image", path)
            if m:
                data, ctype = handlers.announce_image(bridge, m.group(1))
                return self._send(200, data, ctype) or True
            if path == "/api/feedback":
                days = (qs.get("days") or ["30"])[0]
                return self._json(200, {"ok": True, **handlers.feedback_list(
                    bridge, int(days) if days.isdigit() else 30, (qs.get("ignored") or [""])[0] == "1")}) or True
            m = re.fullmatch(r"/api/feedback/preview/(digest|group)", path)
            if m:
                return self._json(200, {"ok": True, "text": handlers.feedback_preview(bridge, m.group(1))}) or True
            if path == "/api/persona":
                return self._json(200, {"ok": True, **handlers.persona_get(bridge)}) or True
            if path == "/api/schedule":
                return self._json(200, {"ok": True, **handlers.schedule_list(bridge)}) or True
            if path == "/api/templates":
                return self._json(200, {"ok": True, "items": handlers.templates_list(bridge)}) or True
            if path == "/api/stats":
                days = (qs.get("days") or ["14"])[0]
                return self._json(200, {"ok": True, **handlers.reply_stats(bridge, int(days) if days.isdigit() else 14)}) or True
            return None

        def _post_more(self, path: str, body: dict):
            if path == "/api/allow":
                return self._json(200, {"ok": True, "items": handlers.allow_change(
                    bridge, body.get("add"), body.get("remove"), body.get("confirm") is True)}) or True
            m = re.fullmatch(r"/api/announce/(\d{3})/(publish|cancel)", path)
            if m:
                return self._json(200, {"ok": True, "item": handlers.announce_act(
                    bridge, m.group(1), m.group(2), body.get("confirm") is True)}) or True
            m = re.fullmatch(r"/api/feedback/(\d+)/(ignore|unignore)", path)
            if m:
                return self._json(200, {"ok": True, **handlers.feedback_ignore(
                    bridge, int(m.group(1)), m.group(2) == "ignore")}) or True
            if path == "/api/feedback/classify":
                handlers.feedback_classify(bridge)
                return self._json(202, {"ok": True, "started": True}) or True
            if path == "/api/persona":
                return self._json(200, {"ok": True, **handlers.persona_save(bridge, body)}) or True
            if path == "/api/persona/preset":
                return self._json(200, {"ok": True, **handlers.persona_preset(
                    bridge, str(body.get("id") or ""), body.get("confirm") is True)}) or True
            if path == "/api/send":
                return self._json(200, {"ok": True, **handlers.send(bridge, body)}) or True
            if path == "/api/schedule":
                if body.get("confirm") is not True:
                    raise handlers.HandlerError("定时发送需要确认")
                return self._json(200, {"ok": True, "item": handlers.schedule_add(bridge, body, "管理台")}) or True
            m = re.fullmatch(r"/api/schedule/(\d+)/cancel", path)
            if m:
                return self._json(200, {"ok": True, "item": handlers.schedule_cancel(bridge, int(m.group(1)))}) or True
            if path == "/api/templates":
                return self._json(200, {"ok": True, "items": handlers.template_put(
                    bridge, str(body.get("name") or ""), str(body.get("text") or ""))}) or True
            if path == "/api/templates/delete":
                return self._json(200, {"ok": True, "items": handlers.template_delete(bridge, str(body.get("name") or ""))}) or True
            if path == "/api/templates/render":
                return self._json(200, {"ok": True, "text": handlers.message_text(
                    bridge, {"template": str(body.get("name") or ""), "vars": body.get("vars") or {}})}) or True
            return None

        def do_POST(self):
            if not self._auth() or not self._write_ok():
                return
            path = urlparse(self.path).path
            body = self._body()
            if body is None:
                return
            try:
                if path == "/api/settings":
                    return self._json(200, {"ok": True, "items": handlers.set_settings(bridge, body, "管理台")})
                if path in ("/api/pause", "/api/resume"):
                    return self._json(200, {"ok": True, **handlers.pause(bridge, path == "/api/pause")})
                if path == "/api/knowledge/sync":
                    handlers.knowledge_sync(bridge)
                    return self._json(202, {"ok": True, "started": True})
                if path == "/api/knowledge/faq":
                    return self._json(200, {"ok": True, "items": handlers.knowledge_faq_save(bridge, body)})
                if path == "/api/knowledge/faq/delete":
                    return self._json(200, {"ok": True, "items": handlers.knowledge_faq_delete(bridge, body)})
                if self._post_more(path, body):
                    return
            except handlers.HandlerError as e:
                return self._err(e.code, str(e), **e.extra)
            self._err(404, "没有这个接口")

        def do_PUT(self):
            if self._auth():
                self._err(405, "不支持")

        do_DELETE = do_PUT

        # ---- 服务端推送 ----
        def _sse(self) -> None:
            q = bridge.events.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                self.wfile.write(b"retry: 3000\n\n")
                self.wfile.flush()
                while True:
                    try:
                        ev = q.get(timeout=ping)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    else:
                        data = json.dumps(ev, ensure_ascii=False)
                        self.wfile.write(f"id: {ev['id']}\nevent: {ev['kind']}\ndata: {data}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                bridge.events.unsubscribe(q)

        # ---- 静态文件 ----
        def _static(self, path: str) -> None:
            rel = path.lstrip("/") or "index.html"
            full = os.path.realpath(os.path.join(static_root, rel))
            if not full.startswith(static_root + os.sep) or not os.path.isfile(full):
                full = os.path.join(static_root, "index.html")    # 前端自己处理页面切换
            if not os.path.isfile(full):
                return self._err(404, "管理台前端没有打包进镜像")
            with open(full, "rb") as f:
                data = f.read()
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
                ctype += "; charset=utf-8"
            hashed = os.sep + "assets" + os.sep in full
            self._send(200, data, ctype, "public, max-age=31536000, immutable" if hashed else "private, no-store")

    return H


def serve(bridge: "Bridge") -> None:
    token = os.environ.get("FUXI_ADMIN_TOKEN", "")
    if not token:
        log.warning("未设置 FUXI_ADMIN_TOKEN，管理台不启动")
        return
    host = os.environ.get("FUXI_ADMIN_HOST", "0.0.0.0")
    port = int(os.environ.get("FUXI_ADMIN_PORT", "8791"))
    static_dir = os.environ.get("FUXI_ADMIN_DIR", "/app/admin")
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer((host, port), make_handler(bridge, token, static_dir))
    log.info("管理台监听 %s:%d", host, port)
    srv.serve_forever()
