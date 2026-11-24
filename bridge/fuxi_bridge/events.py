"""管理台用的事件中心：桥接里发生的事（处理完一批消息、转人工……）发布到这里，
管理台的服务端推送（SSE）订阅它；最近 500 条留在内存里给对话流翻历史，今天的数字也在这里数。

只在内存里，重启清空，不落盘：管理台不新增保存聊天内容的地方（见 docs/admin.md「目标与边界」）。
"""

from __future__ import annotations

import collections
import itertools
import queue
import threading
import time

KEEP = 500


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class Events:
    def __init__(self, keep: int = KEEP):
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._recent: collections.deque[dict] = collections.deque(maxlen=keep)
        self._subs: set[queue.Queue] = set()
        self.started = time.time()
        self.day = _day(self.started)
        self.today: collections.Counter = collections.Counter()
        self.hourly: list[list[int]] = [[0, 0] for _ in range(24)]   # 今天每小时 [收到, 回复]

    # ---- 发布 ----
    def publish(self, kind: str, data: dict) -> dict:
        ev = {"id": next(self._ids), "kind": kind, "ts": time.time(), **data}
        with self._lock:
            self._count(ev)
            if kind in ("message", "handoff"):
                self._recent.append(ev)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:   # 订阅方太慢（网页卡住）：丢掉它的事件，不拖慢桥接
                pass
        return ev

    def _count(self, ev: dict) -> None:
        day = _day(ev["ts"])
        if day != self.day:
            self.day, self.today = day, collections.Counter()
            self.hourly = [[0, 0] for _ in range(24)]
        hour = time.localtime(ev["ts"]).tm_hour
        if ev["kind"] == "message":
            n = int(ev.get("count") or 1)
            self.today["received"] += n
            self.hourly[hour][0] += n
            if ev.get("replies"):
                self.today["replied"] += 1
                self.hourly[hour][1] += 1
        elif ev["kind"] == "handoff":
            self.today["handoffs"] += 1

    # ---- 读 ----
    def recent(self, before: int | None = None, limit: int = 50, kind: str | None = None) -> list[dict]:
        with self._lock:
            items = list(self._recent)
        if kind:
            items = [e for e in items if e["kind"] == kind]
        if before:
            items = [e for e in items if e["id"] < before]
        return items[-limit:][::-1]

    def counts(self) -> dict:
        with self._lock:
            if _day(time.time()) != self.day:
                return {"received": 0, "replied": 0, "handoffs": 0, "hourly": [[0, 0]] * 24, "since": self.started}
            return {"received": self.today["received"], "replied": self.today["replied"],
                    "handoffs": self.today["handoffs"], "hourly": [list(h) for h in self.hourly],
                    "since": self.started}

    # ---- 订阅 ----
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)
