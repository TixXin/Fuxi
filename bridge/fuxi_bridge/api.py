"""对外发送 API：供其他项目（如浮屿发版）往微信会话/群发文字或图片。

所有请求需 `Authorization: Bearer <FUXI_API_TOKEN>`。

  GET  /healthz                      {ok, healthy, problems[], wechat, halted, version}
  POST /resume                       解除熔断（发送后核对失败会停止一切发送）
  GET  /allow                        当前白名单 [{chat_id, name}]
  PUT  /allow  {"items": [...]}      整体替换；元素可以是 chat_id、唯一的显示名，或 "*"
  POST /allow  {"add": [...], "remove": [...]}  增删
  GET  /settings                     全部行为设置 {items: [{key, label, type, value, default, desc, choices, kernel}]}
  POST /settings {"key": 值, ...}    修改（立即生效）；→ {ok, items} 或 {ok:false, error}
  POST /announce {key, text?, image_base64?, image_name?, to?|chat_id?}
       先发到文件传输助手预览，主人回 /发布 才发群；key 幂等（见 announce.py）→ {ok, created, item}
  GET  /announce?key=...             查公告状态
  GET  /fuyu                         浮屿仓库同步状态 {brief, version, latest, events}；?issue=N 查进度；?q=文字 找相关 issue
  GET  /chats?q=关键词                按备注/昵称/群名模糊搜，返回 [{chat_id, name, remark, isGroup}]
  POST /send                         {"to": "群名或备注" | "chat_id": "xxx@chatroom",
                                       "text": "...", "image_base64": "...", "image_name": "a.png"}
       → {ok, chat_id, verified}     verified=发送后在数据库里看到了这条消息
       text 可以换成 {"template": "模板名", "vars": {...}}；加 "async": true 立即返回 202 {job}，再查 GET /jobs/<id>
  GET  /jobs/<id>                    异步发送的结果 {status: queued|running|done|failed, ok, verified, error}
  POST /schedule                     定时发送 {to|chat_id, at: Unix 秒或「2026-11-25 09:00」| in_minutes,
                                       text | template+vars, image_base64?} → {item}
  GET  /schedule                     {pending, recent}；POST /schedule/<id>/cancel 取消
  GET  /templates                    消息模板 [{name, text, vars}]（在管理台「发送」页维护）
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from . import handlers

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

        def _settings_reply(self) -> None:
            return self._json(200, {"ok": True, "items": handlers.settings_items(bridge)})

        def _auth(self) -> bool:
            got = self.headers.get("Authorization", "")
            if hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
                return True
            self._json(401, {"ok": False, "error": "unauthorized"})
            return False

        def do_GET(self):
            if not self._auth():
                return
            # 请求行按 latin-1 解码；没做百分号编码的中文（curl 直接写 ?q=浮屿）要还原成 UTF-8
            try:
                raw = self.path.encode("latin-1").decode("utf-8")
            except UnicodeError:
                raw = self.path
            u = urlparse(raw)
            if u.path == "/healthz":
                return self._json(200, {"ok": True, **handlers.health(bridge)})
            if u.path == "/allow":
                return self._json(200, {"ok": True, "items": [
                    {"chat_id": i, "name": "全部" if i == "*" else bridge.name_of(i)} for i in bridge.allowlist.items]})
            if u.path == "/settings":
                return self._settings_reply()
            if u.path == "/announce":
                key = (parse_qs(u.query).get("key") or [""])[0]
                it = bridge.announcer.get(key)
                if not it:
                    return self._json(404, {"ok": False, "error": "没有这个 key"})
                return self._json(200, {"ok": True, "item": bridge.announcer.public(it)})
            if u.path == "/fuyu":
                qs = parse_qs(u.query)
                f = bridge.fuyu
                if (qs.get("issue") or [""])[0].lstrip("#").isdigit():
                    n = int(qs["issue"][0].lstrip("#"))
                    return self._json(200, {"ok": True, "number": n, "status": f.status(n)})
                q = (qs.get("q") or [""])[0].strip()
                if q:
                    return self._json(200, {"ok": True, "items": [
                        {"score": s, "number": it["number"], "title": it["title"], "state": it["state"],
                         "url": it["html_url"], "status": f.status(it["number"])} for s, it in f.find(q, k=5)]})
                return self._json(200, {"ok": True, "brief": f.brief(), "version": f.version_brief(),
                                        "latest": f.latest(), "latest_beta": f.latest(prerelease=True),
                                        "events": f.recent_events(20)})
            if u.path.startswith("/jobs/"):
                try:
                    return self._json(200, {"ok": True, "job": handlers.job_get(bridge, u.path[6:])})
                except handlers.HandlerError as e:
                    return self._json(e.code, {"ok": False, "error": str(e)})
            if u.path == "/schedule":
                return self._json(200, {"ok": True, **handlers.schedule_list(bridge)})
            if u.path == "/templates":
                return self._json(200, {"ok": True, "items": handlers.templates_list(bridge)})
            if u.path == "/remote/poll":
                qs = parse_qs(u.query)
                target = (qs.get("target") or ["mac"])[0].strip().lower()
                wait = float((qs.get("wait") or ["25"])[0])
                host = (qs.get("host") or [""])[0].strip()
                cmds = bridge.remote.poll(target, wait=wait, host=host) if hasattr(bridge, "remote") else []
                return self._json(200, {"ok": True, "commands": cmds})
            if u.path == "/remote/nodes":
                snap = bridge.remote.nodes_snapshot() if hasattr(bridge, "remote") else {}
                active = dict(bridge.remote.active) if hasattr(bridge, "remote") and bridge.remote.active else None
                return self._json(200, {"ok": True, "nodes": snap, "active": active})
            if u.path == "/feedback":
                qs = parse_qs(u.query)
                days = max(1, min(int((qs.get("days") or ["14"])[0]), 90))
                ignored = (qs.get("ignored") or ["0"])[0] in ("1", "true", "yes")
                status = (qs.get("status") or [""])[0].strip()
                sync_res = ""
                if (qs.get("sync") or ["0"])[0] in ("1", "true", "yes"):
                    try:
                        sync_res = bridge.feedback.classify(__import__("time").time() + 31)
                    except Exception as e:
                        sync_res = f"整理失败：{e}"
                data = handlers.feedback_list(bridge, days=days, ignored=ignored, status=status)
                if sync_res:
                    data["sync_result"] = sync_res
                return self._json(200, {"ok": True, **data})
            m_fb = re.fullmatch(r"/feedback/(\d+)", u.path)
            if m_fb:
                try:
                    return self._json(200, {"ok": True, "item": handlers.feedback_get(bridge, int(m_fb.group(1)))})
                except handlers.HandlerError as e:
                    return self._json(e.code, {"ok": False, "error": str(e)})
            m_med = re.fullmatch(r"/feedback/(\d+)/media/([^/]+)", u.path)
            if m_med:
                it = bridge.feedback.get(int(m_med.group(1)))
                fname = m_med.group(2)
                if not it or fname not in (it.get("images") or []):
                    return self._json(404, {"ok": False, "error": "找不到该截图"})
                fp = bridge.feedback.media_path(fname)
                if not fp:
                    return self._json(404, {"ok": False, "error": "截图文件不存在"})
                with open(fp, "rb") as f:
                    raw_img = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(raw_img)))
                self.end_headers()
                self.wfile.write(raw_img)
                return
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
            if urlparse(self.path).path == "/settings":
                body = self._body()
                if body is None:
                    return
                try:
                    items = handlers.set_settings(bridge, body)
                except handlers.HandlerError as e:
                    return self._json(e.code, {"ok": False, "error": str(e), **e.extra})
                return self._json(200, {"ok": True, "items": items})
            if urlparse(self.path).path == "/announce":
                from .announce import AnnounceError
                body = self._body()
                if body is None:
                    return
                target = body.get("chat_id") or body.get("to") or bridge.cfg.get("announce_to")
                try:
                    chat = bridge.resolve_chat(target)
                except ValueError as e:
                    return self._json(409, {"ok": False, "error": f"{e}（可在 /设置 发版群 指定默认群）"})
                image, ext = None, ".png"
                if body.get("image_base64"):
                    try:
                        image = base64.b64decode(body["image_base64"], validate=True)
                    except ValueError:
                        return self._json(400, {"ok": False, "error": "image_base64 无效"})
                    if str(body.get("image_name", "")).lower().endswith((".jpg", ".jpeg")):
                        ext = ".jpg"
                text = body.get("text")
                try:
                    it, created = bridge.announcer.create(str(body.get("key") or ""), chat,
                                                          str(text) if text else None, image, ext)
                except AnnounceError as e:
                    return self._json(e.code, {"ok": False, "error": str(e)})
                log.warning("公告 %s：%s", it["key"], "已发预览，等确认" if created else f"已存在（{it['status']}）")
                return self._json(200, {"ok": True, "created": created, "item": bridge.announcer.public(it)})
            if urlparse(self.path).path == "/resume":
                return self._json(200, {"ok": True, "was": handlers.resume(bridge)})
            if urlparse(self.path).path == "/remote/heartbeat":
                body = self._body()
                if body is None:
                    return
                res = bridge.remote.heartbeat(str(body.get("target") or "mac"), str(body.get("host") or ""))
                return self._json(200, res)
            if urlparse(self.path).path == "/remote/reply":
                body = self._body()
                if body is None:
                    return
                res = bridge.remote.deliver_reply(
                    sid=str(body.get("sid") or ""),
                    seq=int(body.get("seq") or 0),
                    ok=bool(body.get("ok", True)),
                    text=str(body.get("text") or ""),
                    claude_session_id=str(body.get("claude_session_id") or ""),
                    duration_s=float(body.get("duration_s") or 0),
                    cost_usd=float(body.get("cost_usd") or 0),
                    error=str(body.get("error") or ""),
                    log_path=str(body.get("log_path") or ""),
                )
                return self._json(200, res)
            path = urlparse(self.path).path
            if path == "/feedback/classify":
                try:
                    res = bridge.feedback.classify(__import__("time").time() + 31)
                except Exception as e:
                    return self._json(502, {"ok": False, "error": str(e)})
                return self._json(200, {"ok": True, "result": res})
            m_fb_act = re.fullmatch(r"/feedback/(\d+)/(claim|done|ignore|unignore)", path)
            if m_fb_act:
                iid = int(m_fb_act.group(1))
                act = m_fb_act.group(2)
                body = self._body() if int(self.headers.get("Content-Length") or 0) > 0 else {}
                if body is None:
                    return
                try:
                    if act == "claim":
                        item = handlers.feedback_claim(bridge, iid, body)
                        return self._json(200, {"ok": True, "item": item})
                    if act == "done":
                        item = handlers.feedback_done(bridge, iid, body)
                        return self._json(200, {"ok": True, "item": item})
                    res = handlers.feedback_ignore(bridge, iid, on=(act == "ignore"))
                    return self._json(200, {"ok": True, **res})
                except handlers.HandlerError as e:
                    return self._json(e.code, {"ok": False, "error": str(e)})
            m = re.fullmatch(r"/schedule/(\d+)/cancel", path)
            if m:
                try:
                    return self._json(200, {"ok": True, "item": handlers.schedule_cancel(bridge, int(m.group(1)))})
                except handlers.HandlerError as e:
                    return self._json(e.code, {"ok": False, "error": str(e)})
            if path not in ("/send", "/schedule"):
                return self._json(404, {"ok": False, "error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._json(413, {"ok": False, "error": "body 为空或超过 20MB"})
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                return self._json(400, {"ok": False, "error": "JSON 无效"})
            if not isinstance(body, dict):
                return self._json(400, {"ok": False, "error": "JSON 要是对象"})

            image, ext = None, ".png"
            if body.get("image_base64"):
                ext = ".jpg" if str(body.get("image_name", "")).lower().endswith((".jpg", ".jpeg")) else ".png"
                try:
                    image = base64.b64decode(body["image_base64"], validate=True)
                except ValueError:
                    return self._json(400, {"ok": False, "error": "image_base64 无效"})
            try:
                if path == "/schedule":
                    return self._json(200, {"ok": True, "item": handlers.schedule_add(bridge, body, "api", image, ext)})
                chat = handlers.resolve_target(bridge, body)
                text = handlers.message_text(bridge, body)
            except handlers.HandlerError as e:
                return self._json(e.code, {"ok": False, "error": str(e), **e.extra})
            if text is None and image is None:
                return self._json(400, {"ok": False, "error": "需要 text、template 或 image_base64"})

            img_path = None
            if image is not None:
                img_path = os.path.join(outdir, uuid.uuid4().hex + ext)
                with open(img_path, "wb") as f:
                    f.write(image)

            from .app import SendJob

            def make():
                jobs = []
                # 先图后文：分享图配一句说明的常见用法
                if img_path:
                    jobs.append(SendJob(chat=chat, image=img_path, source="api"))
                if text:
                    jobs.append(SendJob(chat=chat, text=text, source="api"))
                return jobs

            def cleanup():
                if img_path and os.path.exists(img_path):
                    os.remove(img_path)

            if body.get("async"):
                job = bridge.jobs.submit(chat, make, "api", cleanup)
                log.info("API 异步发送到 %s：任务 %s", chat, job["id"])
                return self._json(202, {"ok": True, "chat_id": chat, "job": job})
            try:
                results = [bridge.send(j) for j in make()]
            finally:
                cleanup()
            err = "; ".join(j.error for j in results if j.error)
            ok = all(j.ok for j in results)
            verified = all(j.verified for j in results)
            log.info("API 发送到 %s ok=%s verified=%s", chat, ok, verified)
            self._json(200 if ok else 502, {"ok": ok, "chat_id": chat, "verified": verified, "error": err})

    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer((host, port), H)
    log.info("对外发送 API 监听 %s:%d", host, port)
    srv.serve_forever()
