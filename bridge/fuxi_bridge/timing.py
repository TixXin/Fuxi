"""自动回复的全链路耗时：从对方消息入库到 bot 的气泡发出，每段各花了多久。

阶段（秒）：
  discover  消息入库 → 桥接读到（create_time 只精确到秒，有 ±1 秒误差）
  settle    读到 → 交给内核（同一会话连发合并窗口）
  kernel    内核 webhook + 发送许可（含大模型）
  typing    模拟打字延迟（防封，刻意的）
  queue     气泡在发送队列里等待（前一条还在发/核对）
  open      搜索并进入会话
  paste     粘贴 + 回车
  verify    发出后到数据库核对通过（会阻塞下一条气泡）
汇总：
  first     对方消息入库 → 第一个气泡发出（对方实际等待）
  total     对方消息入库 → 最后一个气泡发出
"""

from __future__ import annotations

import collections
import threading

STAGES = ("discover", "settle", "kernel", "typing", "queue", "open", "paste", "verify")
LABELS = {
    "discover": "发现", "settle": "合并", "kernel": "内核", "typing": "打字", "queue": "排队",
    "open": "进会话", "paste": "粘贴", "verify": "核对", "first": "首条", "total": "全部",
}


class Trace:
    """一次自动回复的耗时。气泡阶段（typing 及之后）按气泡累加。"""

    def __init__(self, msg_time: float, seen: float):
        self.msg_time = msg_time
        self.seen = seen
        self.s: dict[str, float] = collections.defaultdict(float)
        self.s["discover"] = max(0.0, seen - msg_time)
        self.bubbles = 0
        self.first_sent: float | None = None
        self.last_sent: float | None = None

    def add(self, stage: str, seconds: float) -> None:
        self.s[stage] += max(0.0, seconds)

    def bubble_sent(self, at: float) -> None:
        self.bubbles += 1
        if self.first_sent is None:
            self.first_sent = at
        self.last_sent = at

    def result(self) -> dict[str, float]:
        out = {k: round(self.s.get(k, 0.0), 2) for k in STAGES}
        if self.first_sent is not None:
            out["first"] = round(self.first_sent - self.msg_time, 2)
            out["total"] = round((self.last_sent or self.first_sent) - self.msg_time, 2)
        out["bubbles"] = self.bubbles
        return out

    def summary(self) -> str:
        r = self.result()
        head = f"首条 {r['first']}s / 全部 {r['total']}s（{self.bubbles} 个气泡）" if "first" in r else "未发出"
        parts = " ".join(f"{LABELS[k]}{r[k]}" for k in STAGES if r[k] >= 0.05)
        return f"{head}：{parts}"


class LatencyStats:
    def __init__(self, maxlen: int = 50):
        self._items: collections.deque[dict] = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, result: dict) -> None:
        if "first" in result:
            with self._lock:
                self._items.append(result)

    def summary(self) -> dict:
        """各阶段中位数（秒）；气泡阶段按「每个气泡」平均后再取中位数。"""
        with self._lock:
            items = list(self._items)
        if not items:
            return {"n": 0}

        def med(xs):
            xs = sorted(xs)
            return round(xs[len(xs) // 2], 2)

        out: dict = {"n": len(items)}
        for k in ("first", "total"):
            out[k] = med(r[k] for r in items)
        out["first_max"] = round(max(r["first"] for r in items), 2)
        for k in STAGES:
            per = [r[k] / max(1, r["bubbles"]) if k in STAGES[3:] else r[k] for r in items]
            out[k] = med(per)
        return out

    def brief(self) -> str:
        s = self.summary()
        if not s.get("n"):
            return ""
        parts = " ".join(f"{LABELS[k]}{s[k]}" for k in STAGES if s[k] >= 0.05)
        return f"首条中位 {s['first']} 秒（最慢 {s['first_max']}，近 {s['n']} 次）；分段中位：{parts}"
