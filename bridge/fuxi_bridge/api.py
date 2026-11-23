"""对外发送 API：供其他项目（如浮屿发版）往微信会话/群发文字或图片。

所有请求需 `Authorization: Bearer <FUXI_API_TOKEN>`。

  GET  /healthz                      {ok, wechat, halted}
  POST /resume                       解除熔断（发送后核对失败会停止一切发送）
  GET  /allow                        当前白名单 [{chat_id, name}]
  PUT  /allow  {"items": [...]}      整体替换；元素可以是 chat_id、唯一的显示名，或 "*"
  POST /allow  {"add": [...], "remove": [...]}  增删
  GET  /chats?q=关键词                按备注/昵称/群名模糊搜，返回 [{chat_id, name, remark, isGroup}]
  POST /send                         {"to": "群名或备注" | "chat_id": "xxx@chatroom",
                                       "text": "...", "image_base64": "...", "image_name": "a.png"}
       → {ok, chat_id, verified}     verified=发送后在数据库里看到了这条消息
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from .app import Bridge

log = logging.getLogger("fuxi.api")
MAX_BODY = 20 << 20


def serve(bridge: "Bridge") -> None:
    token = os.environ["FUXI_API_TOKEN"]
    host = os.environ.get("FUXI_API_HOST", "0.0.0.0")
    port = int(os.environ.get("FUXI_API_PORT", "8790"))
    outdir = os.path.join(bridge.work, "outbox")
    os.makedirs(outdir, mode=0o700, exist_ok=True)

    class H(BaseHTTPRequestHandler):
        server_version = "fuxi"

        def log_message(self, fmt, *args):  # 走 logging
            log.info("%s %s", self.address_string(), fmt % args)

        def _json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _auth(self) -> bool:
            got = self.headers.get("Authorization", "")
            if hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
                return True
            self._json(401, {"ok": False, "error": "unauthorized"})
            return False

        def do_GET(self):
            if not self._auth():
                return
            u = urlparse(self.path)
            if u.path == "/healthz":
                return self._json(200, {"ok": True, "wechat": bridge.last_send_ok, "halted": bridge.halted})
            if u.path == "/allow":
                return self._json(200, {"ok": True, "items": [
                    {"chat_id": i, "name": "全部" if i == "*" else bridge.name_of(i)} for i in bridge.allowlist.items]})
            if u.path == "/chats":
                q = (parse_qs(u.query).get("q") or [""])[0].strip()
                if not q:
                    return self._json(400, {"ok": False, "error": "缺少 q"})
                hits = [c for c in bridge.reader.contacts()
                        if q in c["name"] or q in c["remark"] or q == c["alias"]][:30]
                return self._json(200, {"ok": True, "items": [
                    {"chat_id": c["username"], "name": c["name"], "remark": c["remark"], "isGroup": c["isGroup"]}
                    for c in hits]})
            self._json(404, {"ok": False, "error": "not found"})

        def _body(self) -> dict | None:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                self._json(413, {"ok": False, "error": "body 为空或超过 20MB"})
                return None
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                body = None
            if not isinstance(body, dict):
                self._json(400, {"ok": False, "error": "JSON 无效"})
                return None
            return body

        def _resolve(self, items) -> tuple[list[str], dict[str, str]] | None:
            """把显示名解析成 chat_id；有歧义或找不到就整批拒绝。"""
            ids, names, bad = [], {}, []
            for raw in items or []:
                it = str(raw).strip()
                if not it:
                    continue
                if it == "*" or it == "filehelper" or it.endswith("@chatroom") or it.startswith("wxid_"):
                    ids.append(it)
                    names[it] = "全部" if it == "*" else bridge.name_of(it)
                    continue
                hits = bridge.reader.find_chats(it)
                if len(hits) != 1:
                    bad.append(f"「{it}」匹配到 {len(hits)} 个会话")
                    continue
                ids.append(hits[0]["username"])
                names[hits[0]["username"]] = it
            if bad:
                self._json(409, {"ok": False, "error": "；".join(bad) + "，请改用 chat_id（GET /chats?q= 查）"})
                return None
            return ids, names

        def _allow_reply(self):
            return self._json(200, {"ok": True, "items": [
                {"chat_id": i, "name": "全部" if i == "*" else bridge.name_of(i)} for i in bridge.allowlist.items]})

        def do_PUT(self):
            if not self._auth():
                return
            if urlparse(self.path).path != "/allow":
                return self._json(404, {"ok": False, "error": "not found"})
            body = self._body()
            if body is None:
                return
            got = self._resolve(body.get("items"))
            if got is None:
                return
            ids, names = got
            bridge.allowlist.set(ids, names)
            log.warning("白名单被 API 整体替换为 %d 项", len(ids))
            return self._allow_reply()

        def do_POST(self):
            if not self._auth():
                return
            if urlparse(self.path).path == "/allow":
                body = self._body()
                if body is None:
                    return
                add = self._resolve(body.get("add"))
                if add is None:
                    return
                rm = self._resolve(body.get("remove"))
                if rm is None:
                    return
                cur = list(bridge.allowlist.items)
                names = {i: bridge.name_of(i) for i in cur if i != "*"} | add[1]
                new = [i for i in cur if i not in rm[0]] + [i for i in add[0] if i not in cur]
                bridge.allowlist.set(new, names)
                log.warning("白名单经 API 修改：+%d -%d", len(add[0]), len(rm[0]))
                return self._allow_reply()
            if urlparse(self.path).path == "/resume":
                was, bridge.halted = bridge.halted, ""
                log.warning("人工解除熔断（之前：%s）", was or "未熔断")
                return self._json(200, {"ok": True, "was": was})
            if urlparse(self.path).path != "/send":
                return self._json(404, {"ok": False, "error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._json(413, {"ok": False, "error": "body 为空或超过 20MB"})
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                return self._json(400, {"ok": False, "error": "JSON 无效"})

            chat = (body.get("chat_id") or "").strip()
            if not chat:
                to = (body.get("to") or "").strip()
                if not to:
                    return self._json(400, {"ok": False, "error": "需要 to 或 chat_id"})
                hits = bridge.reader.find_chats(to)
                if len(hits) != 1:
                    return self._json(409, {"ok": False, "error": f"「{to}」匹配到 {len(hits)} 个会话，请改用 chat_id",
                                            "candidates": [h["username"] for h in hits[:10]]})
                chat = hits[0]["username"]

            text = body.get("text")
            img_path = None
            if body.get("image_base64"):
                ext = ".jpg" if str(body.get("image_name", "")).lower().endswith((".jpg", ".jpeg")) else ".png"
                img_path = os.path.join(outdir, uuid.uuid4().hex + ext)
                try:
                    with open(img_path, "wb") as f:
                        f.write(base64.b64decode(body["image_base64"], validate=True))
                except ValueError:
                    return self._json(400, {"ok": False, "error": "image_base64 无效"})
            if text is None and img_path is None:
                return self._json(400, {"ok": False, "error": "需要 text 或 image_base64"})

            from .app import SendJob

            try:
                results = []
                # 先图后文：分享图配一句说明的常见用法
                if img_path:
                    results.append(bridge.send(SendJob(chat=chat, image=img_path)))
                if text:
                    results.append(bridge.send(SendJob(chat=chat, text=str(text))))
            finally:
                if img_path and os.path.exists(img_path):
                    os.remove(img_path)
            err = "; ".join(j.error for j in results if j.error)
            ok = all(j.ok for j in results)
            verified = all(j.verified for j in results)
            log.info("API 发送到 %s ok=%s verified=%s", chat, ok, verified)
            self._json(200 if ok else 502, {"ok": ok, "chat_id": chat, "verified": verified, "error": err})

    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer((host, port), H)
    log.info("对外发送 API 监听 %s:%d", host, port)
    srv.serve_forever()
