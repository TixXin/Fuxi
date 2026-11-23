"""与 whale_girl Node 内核通信（同一网络命名空间的回环）。

契约来自上游 src/adapters/gateway.js 与 src/http/routes/：
- POST /webhook/wechat           入站消息，回复在 HTTP 响应里（x-whale-bridge: 1）
- GET  /api/reminders/due        到期提醒/日记/插件文本（取出即标记 sending）
- POST /api/reminders/ack        {id, ok, error}
- POST /api/reminders/permission {id}（插件类提醒发送前确认）
- POST /api/wechat/send-permission {chatId, name} → {allowed, reason}
- POST /api/wechat/contacts/sync {account, at, items}
- POST /api/bridge/heartbeat     每 20 秒
- POST /api/config {patch}       改配置并立即生效（用于同步白名单）
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


class KernelError(Exception):
    pass


class Kernel:
    def __init__(self, base_url: str, token: str, timeout: float = 120):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _req(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json", "x-bot-token": self.token, "x-whale-bridge": "1"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise KernelError(f"{method} {path} → {e.code} {e.read()[:200]!r}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise KernelError(f"{method} {path} 失败: {e}") from e

    def wait_ready(self, max_wait: float = 120) -> None:
        deadline = time.monotonic() + max_wait
        while True:
            try:
                if self._req("GET", "/healthz", timeout=3).get("ok"):
                    return
            except KernelError:
                pass
            if time.monotonic() > deadline:
                raise KernelError("内核一直没就绪")
            time.sleep(2)

    def webhook(self, payload: dict) -> dict:
        return self._req("POST", "/webhook/wechat", payload)

    def send_permission(self, chat_id: str, name: str) -> tuple[bool, str]:
        try:
            r = self._req("POST", "/api/wechat/send-permission", {"chatId": chat_id, "name": name}, timeout=10)
            return bool(r.get("allowed", True)), r.get("reason") or ""
        except KernelError as e:
            log.warning("发送许可查询失败，按不允许处理：%s", e)
            return False, f"permission check failed: {e}"

    def reminders_due(self) -> list[dict]:
        return self._req("GET", "/api/reminders/due", timeout=10).get("items") or []

    def reminder_permission(self, rid: str) -> bool:
        try:
            return bool(self._req("POST", "/api/reminders/permission", {"id": rid}, timeout=10).get("ok"))
        except KernelError:
            return False

    def reminder_ack(self, rid: str, ok: bool, error: str = "") -> None:
        self._req("POST", "/api/reminders/ack", {"id": rid, "ok": ok, "error": error}, timeout=10)

    def contacts_sync(self, account: str, items: list[dict]) -> None:
        self._req("POST", "/api/wechat/contacts/sync",
                  {"account": account, "at": int(time.time() * 1000), "items": items}, timeout=30)

    def set_whitelist(self, items: list[str]) -> None:
        """内核第二道防线：whitelistOnly + care.whitelist（发送前拦截）。'*' = 关闭白名单。"""
        if "*" in items:
            patch = {"wechat": {"whitelistOnly": False}, "care": {"whitelist": []}}
        else:
            patch = {"wechat": {"whitelistOnly": True}, "care": {"whitelist": list(items)}}
        r = self._req("POST", "/api/config", {"patch": patch}, timeout=15)
        if not r.get("ok"):
            raise KernelError(f"内核拒绝白名单配置：{r.get('error')}")

    def heartbeat(self, info: dict) -> None:
        self._req("POST", "/api/bridge/heartbeat", {"platform": "wechat", **info}, timeout=10)
