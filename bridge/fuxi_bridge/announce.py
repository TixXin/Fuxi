"""要人确认的群发（浮屿发版公告等）：先发到文件传输助手预览，主人回 /发布 才真发。

  POST /announce {key, text?, image_base64?, image_name?, to?, chat_id?}
       key 是幂等键（如 fuyu-v1.2.3）：同一个 key 再调只返回原状态，不会重复预览/重复发群。
       to / chat_id 不给就用设置里的「发版群」。
  GET  /announce?key=...   查状态：pending（等确认）| sent | cancelled | expired | failed

遥控：/待发 列出等确认的；/发布 [编号] 发出（只有一条时可省编号）；/取消 [编号] 放弃。
预览 24 小时内不确认就过期。状态和图片存在状态卷（/state/announce/），重启不丢。
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import Bridge

log = logging.getLogger("fuxi.announce")

TTL = 24 * 3600
KEEP = 30 * 24 * 3600   # 已结束的记录保留 30 天（幂等用）
STATUS_ZH = {"pending": "等确认", "sending": "发送中", "sent": "已发出", "cancelled": "已取消", "expired": "已过期", "failed": "发送失败"}


class AnnounceError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


class Announcer:
    def __init__(self, bridge: "Bridge", root: str):
        self.b = bridge
        self.root = root
        self.path = os.path.join(root, "announce.json")
        os.makedirs(root, mode=0o700, exist_ok=True)
        self._lock = threading.RLock()
        self.items: dict[str, dict] = {}
        try:
            with open(self.path) as f:
                self.items = json.load(f).get("items", {})
        except (OSError, ValueError):
            pass

    # ---- 存储 ----
    def _save(self) -> None:
        now = time.time()
        for k, it in list(self.items.items()):
            if it["status"] != "pending" and now - it.get("updated", it["created"]) > KEEP:
                self.items.pop(k)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"items": self.items}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def _expire(self) -> None:
        now = time.time()
        for it in self.items.values():
            if it["status"] == "pending" and now - it["created"] > TTL:
                self._finish(it, "expired")

    def _finish(self, it: dict, status: str, error: str = "") -> None:
        it["status"], it["updated"] = status, time.time()
        if error:
            it["error"] = error
        img = it.get("image")
        if img and status != "pending" and os.path.exists(img):
            os.remove(img)

    def public(self, it: dict) -> dict:
        return {k: it.get(k) for k in ("key", "code", "chat_id", "to", "status", "error", "created", "updated")} | {
            "status_zh": STATUS_ZH.get(it["status"], it["status"])}

    def get(self, key: str) -> dict | None:
        with self._lock:
            self._expire()
            return self.items.get(key)

    def pending(self) -> list[dict]:
        with self._lock:
            self._expire()
            return sorted((i for i in self.items.values() if i["status"] == "pending"), key=lambda i: i["created"])

    # ---- 新建：存起来、发预览 ----
    def create(self, key: str, chat: str, text: str | None, image: bytes | None, image_ext: str = ".png") -> tuple[dict, bool]:
        """返回 (记录, 是否新建)。同 key 已存在时原样返回，不再预览。"""
        key = (key or "").strip()
        if not key or len(key) > 128:
            raise AnnounceError("需要 key（幂等键，128 字以内）")
        if not text and not image:
            raise AnnounceError("需要 text 或 image_base64")
        with self._lock:
            self._expire()
            old = self.items.get(key)
            if old:
                return old, False
            used = {i["code"] for i in self.items.values() if i["status"] == "pending"}
            code = next(c for c in (f"{random.randint(100, 999)}" for _ in range(1000)) if c not in used)
            img_path = None
            if image:
                img_path = os.path.join(self.root, f"{code}-{int(time.time())}{image_ext}")
                with open(img_path, "wb") as f:
                    f.write(image)
            it = {"key": key, "code": code, "chat_id": chat, "to": self.b.name_of(chat), "text": text or "",
                  "image": img_path, "status": "pending", "created": time.time(), "updated": time.time()}
            self.items[key] = it
            self._save()
        try:
            self._preview(it)
        except Exception as e:   # 预览没发出去：记录还在，主人仍可 /待发 → /发布
            log.warning("公告 %s 的预览没发出去：%s", key, e)
            it["error"] = f"预览没发出去：{e}"
        return it, True

    def _preview(self, it: dict) -> None:
        from .app import SendJob
        head = (f"【待发 #{it['code']}】下面这条将发到「{it['to']}」\n"
                f"回复 /发布 {it['code']} 发出，/取消 {it['code']} 放弃（24 小时内有效）")
        jobs = [SendJob(chat="filehelper", text=head, source="control")]
        if it.get("image"):
            jobs.append(SendJob(chat="filehelper", image=it["image"], source="control"))
        if it.get("text"):
            jobs.append(SendJob(chat="filehelper", text=it["text"], source="control"))
        for j in jobs:
            r = self.b.send(j)
            if not r.ok:
                raise RuntimeError(r.error or "发送失败")

    # ---- 确认 / 取消 ----
    def _pick(self, code: str) -> dict:
        items = self.pending()
        if code:
            for it in items:
                if it["code"] == code:
                    return it
            raise AnnounceError(f"没有等确认的 #{code}。" + (self._brief(items) if items else ""))
        if not items:
            raise AnnounceError("没有等确认的公告。")
        if len(items) > 1:
            raise AnnounceError("有多条等确认，请带编号：\n" + self._brief(items))
        return items[0]

    def _brief(self, items: list[dict]) -> str:
        return "\n".join(f"#{i['code']} → {i['to']}：{(i['text'] or '[图片]')[:30]}" for i in items)

    def publish(self, code: str = "") -> dict:
        from .app import SendJob
        with self._lock:
            it = self._pick(code.strip().lstrip("#"))
            it["status"] = "sending"   # 防止并发的第二次 /发布
        chat = it["chat_id"]
        if not self.b.allowlist.allows(chat):
            log.info("公告目标 %s 不在白名单，按主人确认照发", it["to"])
        results = []
        if it.get("image"):
            results.append(self.b.send(SendJob(chat=chat, image=it["image"], source="api")))
        if it.get("text") and all(r.ok for r in results):
            results.append(self.b.send(SendJob(chat=chat, text=it["text"], source="api")))
        err = "; ".join(r.error for r in results if r.error)
        with self._lock:
            if all(r.ok for r in results):
                self._finish(it, "sent")
                log.warning("公告 %s 已发到 %s", it["key"], it["to"])
            else:
                # 发了一半（图出去了、字没出去）不再自动重试，避免重复刷屏
                self._finish(it, "failed", err or "发送失败")
                log.error("公告 %s 发送失败：%s", it["key"], err)
            self._save()
        return it

    def cancel(self, code: str = "") -> dict:
        with self._lock:
            it = self._pick(code.strip().lstrip("#"))
            self._finish(it, "cancelled")
            self._save()
        return it

    def listing(self) -> str:
        items = self.pending()
        return ("等确认的公告：\n" + self._brief(items)) if items else "没有等确认的公告。"
