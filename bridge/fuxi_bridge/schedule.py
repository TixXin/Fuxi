"""发送的三样增强：异步发送（先拿任务号，回头查结果）、定时发送、消息模板。

- 异步任务只在内存里（最近 200 个），重启就没了；调用方拿不到结果就当没发成。
- 定时发送落盘在 /state/schedule.json（配图放 /state/schedule/），重启不丢。
  到点时桥接停着（重启、宕机）错过超过 GRACE 秒的，记为「错过」不补发，并私聊主人：
  晚了很久的消息（「早安」「会议开始了」）补发出去反而奇怪。
- 模板在 /state/templates.json：{名字: 文本}，文本里 {变量} 由调用方给；
  {date} {time} {weekday} 不给也会自动填成发送时刻的值（定时消息按到点那一刻填）。

暂停自动回复不影响这里：定时、手动、API 发送都是主人明确要发的。熔断时照样不发（发送队列统一拦）。
"""

from __future__ import annotations

import collections
import itertools
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .app import Bridge, SendJob

log = logging.getLogger(__name__)

GRACE = 600                 # 到点后多少秒内还补发
MAX_AHEAD = 90 * 86400      # 最远能定多久以后
MAX_PENDING = 200
MAX_TEXT = 2000
STATUS_ZH = {"pending": "等到点", "sending": "发送中", "sent": "已发出", "failed": "没发出去",
             "missed": "错过了", "cancelled": "已取消"}
WEEKDAYS = "一二三四五六日"


class ScheduleError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


# ---- 模板 ----
def builtin_vars(ts: float | None = None) -> dict:
    t = time.localtime(ts or time.time())
    return {"date": time.strftime("%m月%d日", t).lstrip("0").replace("月0", "月"), "time": time.strftime("%H:%M", t),
            "weekday": "周" + WEEKDAYS[t.tm_wday]}


def render(text: str, vars: dict | None = None, ts: float | None = None) -> str:
    """把 {变量} 换成值；缺了变量抛 ScheduleError，不发半截消息。{{ 和 }} 是字面的花括号。"""
    values = {**builtin_vars(ts), **{str(k): str(v) for k, v in (vars or {}).items()}}
    missing = []

    def sub(m: re.Match) -> str:
        if m.group(0) in ("{{", "}}"):
            return m.group(0)[0]
        key = m.group(1)
        if key not in values:
            missing.append(key)
            return m.group(0)
        return values[key]
    out = re.sub(r"\{\{|\}\}|\{([^{}\s]{1,30})\}", sub, text)
    if missing:
        raise ScheduleError("模板缺变量：" + "、".join(dict.fromkeys(missing)))
    return out


def placeholders(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"(?<!\{)\{([^{}\s]{1,30})\}(?!\})", text)))


class Templates:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        try:
            with open(path, encoding="utf-8") as f:
                self.items: dict[str, str] = json.load(f)
        except (FileNotFoundError, ValueError):
            self.items = {}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.items, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def list(self) -> list[dict]:
        auto = set(builtin_vars())
        return [{"name": k, "text": v, "vars": [p for p in placeholders(v) if p not in auto]}
                for k, v in sorted(self.items.items())]

    def get(self, name: str) -> str:
        if name not in self.items:
            raise ScheduleError(f"没有叫「{name}」的模板", 404)
        return self.items[name]

    def put(self, name: str, text: str) -> None:
        name, text = (name or "").strip(), text or ""
        if not name or len(name) > 30 or "/" in name:
            raise ScheduleError("模板名要 1~30 个字，不能有 /")
        if not text.strip() or len(text) > MAX_TEXT:
            raise ScheduleError(f"模板内容不能为空，最多 {MAX_TEXT} 字")
        with self._lock:
            if name not in self.items and len(self.items) >= 100:
                raise ScheduleError("模板最多 100 个")
            self.items[name] = text
            self._save()

    def delete(self, name: str) -> None:
        with self._lock:
            if self.items.pop(name, None) is None:
                raise ScheduleError(f"没有叫「{name}」的模板", 404)
            self._save()


# ---- 异步任务 ----
class AsyncJobs:
    def __init__(self, bridge: "Bridge", keep: int = 200):
        self.b = bridge
        self._jobs: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self._keep = keep
        self._lock = threading.Lock()

    def submit(self, chat: str, make: Callable[[], list["SendJob"]], source: str,
               cleanup: Callable[[], None] | None = None) -> dict:
        jid = uuid.uuid4().hex[:12]
        rec = {"id": jid, "chat_id": chat, "status": "queued", "created": time.time(), "done": None,
               "ok": None, "verified": None, "error": "", "source": source}
        with self._lock:
            self._jobs[jid] = rec
            while len(self._jobs) > self._keep:
                self._jobs.popitem(last=False)
        threading.Thread(target=self._run, args=(rec, make, cleanup), name=f"job-{jid}", daemon=True).start()
        return dict(rec)

    def _run(self, rec: dict, make: Callable[[], list["SendJob"]], cleanup: Callable[[], None] | None) -> None:
        rec["status"] = "running"
        try:
            results = [self.b.send(j) for j in make()]
            rec["ok"] = all(j.ok for j in results)
            rec["verified"] = all(j.verified for j in results)
            rec["error"] = "; ".join(j.error for j in results if j.error)
        except Exception as e:   # 后台线程不能让异常丢了：记到任务上
            rec["ok"], rec["error"] = False, str(e)
        finally:
            if cleanup:
                cleanup()
        rec["status"], rec["done"] = ("done" if rec["ok"] else "failed"), time.time()

    def get(self, jid: str) -> dict:
        with self._lock:
            rec = self._jobs.get(jid)
        if rec is None:
            raise ScheduleError("没有这个任务（只保留最近 200 个，重启后清空）", 404)
        return dict(rec)


# ---- 定时发送 ----
class Scheduler:
    def __init__(self, bridge: "Bridge", state_dir: str):
        self.b = bridge
        self.dir = os.path.join(state_dir, "schedule")
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(state_dir, "schedule.json")
        self._lock = threading.RLock()
        try:
            with open(self.path, encoding="utf-8") as f:
                self.items: list[dict] = json.load(f)
        except (FileNotFoundError, ValueError):
            self.items = []
        for it in self.items:   # 上次发到一半就停了：结果未知，按「没发出去」记，不重发
            if it["status"] == "sending":
                it.update(status="failed", error="发送途中桥接重启了，结果未知（去微信看一眼）")
        self._ids = itertools.count(max((it["id"] for it in self.items), default=0) + 1)

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.items, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def public(self, it: dict) -> dict:
        out = {k: it.get(k) for k in ("id", "chat_id", "name", "text", "template", "at", "created", "status",
                                       "error", "sent_at", "verified", "source")}
        return out | {"status_zh": STATUS_ZH.get(it["status"], it["status"]), "has_image": bool(it.get("image"))}

    def add(self, chat: str, at: float, text: str | None = None, template: str | None = None,
            vars: dict | None = None, image: bytes | None = None, ext: str = ".png", source: str = "api") -> dict:
        now = time.time()
        if at < now - 60:
            raise ScheduleError("时间已经过了")
        if at > now + MAX_AHEAD:
            raise ScheduleError("最远只能定 90 天以后")
        if template:
            text = self.b.templates.get(template)
            render(text, vars, at)   # 现在就检查变量齐不齐，别到点才发现
        if text is not None and len(text) > MAX_TEXT:
            raise ScheduleError(f"一次最多 {MAX_TEXT} 字")
        if not (text and text.strip()) and not image:
            raise ScheduleError("需要文字或图片")
        with self._lock:
            if sum(it["status"] == "pending" for it in self.items) >= MAX_PENDING:
                raise ScheduleError(f"等发的定时消息最多 {MAX_PENDING} 条")
            it = {"id": next(self._ids), "chat_id": chat, "name": self.b.name_of(chat), "text": text,
                  "template": template, "vars": vars or {}, "at": float(at), "created": now, "status": "pending",
                  "error": "", "sent_at": None, "verified": None, "source": source, "image": None}
            if image:
                it["image"] = os.path.join(self.dir, f"{it['id']}{ext if ext in ('.png', '.jpg') else '.png'}")
                with open(it["image"], "wb") as f:
                    f.write(image)
            self.items.append(it)
            self._prune()
            self._save()
        log.warning("定时发送 #%d：%s 发往 %s（来自 %s）", it["id"], time.strftime("%m-%d %H:%M", time.localtime(at)),
                    it["name"], source)
        self.b.events.publish("schedule", {"id": it["id"], "status": "pending"})
        return self.public(it)

    def cancel(self, sid: int) -> dict:
        with self._lock:
            it = next((i for i in self.items if i["id"] == sid), None)
            if it is None:
                raise ScheduleError("没有这条定时消息", 404)
            if it["status"] != "pending":
                raise ScheduleError(f"这条已经{STATUS_ZH.get(it['status'], it['status'])}，不能取消")
            it["status"] = "cancelled"
            self._drop_image(it)
            self._save()
        self.b.events.publish("schedule", {"id": sid, "status": "cancelled"})
        return self.public(it)

    def list(self, done: int = 30) -> dict:
        with self._lock:
            pending = sorted((i for i in self.items if i["status"] in ("pending", "sending")), key=lambda i: i["at"])
            finished = sorted((i for i in self.items if i["status"] not in ("pending", "sending")),
                              key=lambda i: i.get("sent_at") or i["at"], reverse=True)[:done]
        return {"pending": [self.public(i) for i in pending], "recent": [self.public(i) for i in finished]}

    def _drop_image(self, it: dict) -> None:
        if it.get("image") and os.path.exists(it["image"]):
            os.remove(it["image"])
        it["image"] = None

    def _prune(self) -> None:
        """结束的只留最近 100 条。"""
        done = [i for i in self.items if i["status"] not in ("pending", "sending")]
        if len(done) > 100:
            old = {id(i) for i in sorted(done, key=lambda i: i["at"])[:len(done) - 100]}
            self.items = [i for i in self.items if id(i) not in old]

    # ---- 到点发送 ----
    def due(self, now: float | None = None) -> list[dict]:
        now = now or time.time()
        with self._lock:
            return sorted((i for i in self.items if i["status"] == "pending" and i["at"] <= now), key=lambda i: i["at"])

    def tick(self, now: float | None = None) -> None:
        from .app import SendJob
        for it in self.due(now):
            now_ = now or time.time()
            if now_ - it["at"] > GRACE:
                self._finish(it, "missed", f"到点时桥接没在运行，晚了 {int((now_ - it['at']) // 60)} 分钟，没补发")
                continue
            with self._lock:
                it["status"] = "sending"
                self._save()
            try:
                text = render(it["text"], it.get("vars"), it["at"]) if it.get("text") else None
            except ScheduleError as e:
                self._finish(it, "failed", str(e))
                continue
            jobs = []
            if it.get("image"):
                jobs.append(self.b.send(SendJob(chat=it["chat_id"], image=it["image"], source="schedule")))
            if text and all(j.ok for j in jobs):
                jobs.append(self.b.send(SendJob(chat=it["chat_id"], text=text, source="schedule")))
            ok = bool(jobs) and all(j.ok for j in jobs)
            it["verified"] = all(j.verified for j in jobs) if ok else None
            self._finish(it, "sent" if ok else "failed", "; ".join(j.error for j in jobs if j.error))

    def _finish(self, it: dict, status: str, error: str = "") -> None:
        with self._lock:
            it.update(status=status, error=error, sent_at=time.time())
            self._drop_image(it)
            self._save()
        level = logging.INFO if status == "sent" else logging.WARNING
        log.log(level, "定时发送 #%d → %s：%s %s", it["id"], it["name"], STATUS_ZH[status], error)
        self.b.events.publish("schedule", {"id": it["id"], "status": status})
        if status in ("failed", "missed"):
            self.b.tell_owner(f"定时消息 #{it['id']}（发给「{it['name']}」，"
                              f"{time.strftime('%m-%d %H:%M', time.localtime(it['at']))}）{STATUS_ZH[status]}：{error}")

    def loop(self, every: float = 5) -> None:
        while True:
            try:
                self.tick()
            except Exception:   # 一条出错不能让定时线程死掉
                log.exception("定时发送出错")
            time.sleep(every)
