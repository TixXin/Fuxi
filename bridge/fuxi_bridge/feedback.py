"""群反馈收集（F1，见 docs/feedback-loop.md）：把反馈群里的聊天整理成 bug / 建议 / 提问，
中午私聊主人发摘要，晚上在群里发当天小结。

流程：
  收集   桥接读到反馈群的每条消息（不管有没有 @ bot、在不在静默时段）都追加到 inbox.jsonl；
         看图 / 语音识别完成后再补上识别结果。
  整理   每小时（以及发摘要、小结前）把还没整理的消息成批交给内核的模型（/api/fuxi/complete），
         让它找出对浮屿的反馈：归到已有条目，或新建条目。谁提的由消息序号反查，不让模型写人名。
  对照   新条目用 F2 的浮屿仓库缓存查相关 issue（分数 ≥ 0.35），附上进度（已修于 vX / 跟进中）。
  摘要   每天 digest_time（默认 12:00）私聊主人：过去 24 小时有动静的条目，带编号。
  小结   每天 group_time（默认 20:00）在反馈群发当天小结：只有标题和处理状态，不带任何人名。
         没有反馈就不发。错过时间 2 小时以上（比如那会儿桥接在重启）当天就不补发了。

存在 /state/feedback/：
  inbox.jsonl  原始消息 {mid, ts, chat, sender, name, text, role}，以及识别补写 {mid, update}
  items.json   条目 [{id, category, title, detail, platform, status, first_seen, last_seen, count,
                       reporters[{wxid, name}], quotes[], issue, issue_score, chats[]}]
  meta.json    classified_until、next_id、digest_day、group_day、last_error
状态：new（新）| known（GitHub 上已有 issue）| answered（群里已解答的提问）| ignored（主人 /忽略）
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

log = logging.getLogger("fuxi.feedback")

CATEGORIES = {"bug": "问题", "suggestion": "建议", "question": "提问"}
MATCH_FLOOR = 0.35
BATCH = 150            # 每次交给模型的消息条数
CLASSIFY_EVERY = 3600
LATE_LIMIT = 2 * 3600  # 定时任务最多晚多久还补发

SYSTEM = """你在帮「浮屿」（一款桌面灵动岛软件，支持 Windows 和 macOS）的作者整理用户群聊天。
从聊天里找出用户对浮屿的反馈，分三类：
- bug：浮屿出错、崩溃、显示不对、功能不按说明工作
- suggestion：想要的新功能、改进建议
- question：怎么用、在哪设置、支不支持某功能这类提问
闲聊、表情、与浮屿无关的内容、主人（标了「主人」）或 bot（标了「bot」）自己说的话都不算反馈，
但可以用来判断问题是否已经在群里解答。

已有条目（编号、类别、标题）：
{existing}

只输出 JSON，不要解释、不要代码块围栏：
{{"items":[{{"ref":已有条目编号或null,"category":"bug|suggestion|question","title":"不超过 30 字，客观描述问题本身",
"detail":"不超过 120 字，补充现象、复现条件、系统和版本（聊天里有就写）","platform":"Windows|macOS|都有|未知",
"messages":[支撑这条反馈的消息序号],"answered":true或false}}]}}
规则：
- 同一个问题合并成一条；和已有条目是同一个问题时填 ref，不要重复新建。
- title、detail 里不要出现任何人名、昵称、微信号、联系方式；不要编造聊天里没有的信息。
- answered 只对 question 有意义：群里已经有人（主人、bot 或其他用户）给出了答案就填 true。
- 没有反馈就输出 {{"items":[]}}。"""


def _day(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))


def _hm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _parse_time(s: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", (s or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def _json_block(text: str) -> dict:
    m = re.search(r"\{[\s\S]*\}", text or "")
    if not m:
        raise ValueError("模型没有输出 JSON")
    return json.loads(m.group(0))


class Feedback:
    def __init__(self, root: str, complete=None, fuyu=None):
        """complete(system, text) -> str：大模型调用（桥接传内核的 /api/fuxi/complete）；fuyu：F2 仓库缓存。"""
        self.root = root
        self.complete = complete
        self.fuyu = fuyu
        os.makedirs(root, mode=0o700, exist_ok=True)
        self._lock = threading.RLock()
        self.meta: dict = self._read("meta.json", {})
        self.items: list[dict] = self._read("items.json", [])

    # ---- 文件 ----
    def _path(self, name: str) -> str:
        return os.path.join(self.root, name)

    def _read(self, name: str, default):
        try:
            with open(self._path(name), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def _write(self, name: str, obj) -> None:
        p = self._path(name)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(p + ".tmp", p)

    def _save(self) -> None:
        self._write("items.json", self.items)
        self._write("meta.json", self.meta)

    # ---- 收集 ----
    def add(self, mid: str, ts: float, chat: str, sender: str, name: str, text: str, role: str = "") -> None:
        """role：owner（主人）/ bot / 空（群友）。"""
        text = (text or "").strip()
        if not text:
            return
        rec = {"mid": mid, "ts": ts, "chat": chat, "sender": sender, "name": name, "text": text[:1500], "role": role}
        with self._lock, open(self._path("inbox.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def annotate(self, mid: str, text: str) -> None:
        """看图 / 语音识别完成后，用带识别结果的文字替换原来的占位。"""
        with self._lock, open(self._path("inbox.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"mid": mid, "update": text[:1500]}, ensure_ascii=False) + "\n")

    def _inbox(self, since: float) -> list[dict]:
        recs: dict[str, dict] = {}
        updates: dict[str, str] = {}
        try:
            with open(self._path("inbox.jsonl"), encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if "update" in r:
                        updates[r["mid"]] = r["update"]
                    elif r.get("ts", 0) > since:
                        recs[r["mid"]] = r
        except OSError:
            return []
        for mid, t in updates.items():
            if mid in recs:
                recs[mid]["text"] = t
        return sorted(recs.values(), key=lambda r: r["ts"])

    def _trim_inbox(self, keep_after: float) -> None:
        """整理过的消息只留 7 天（给 /反馈 编号 看原话），防止文件一直长。"""
        p = self._path("inbox.jsonl")
        try:
            if os.path.getsize(p) < 4 << 20:
                return
            with open(p, encoding="utf-8") as f:
                lines = [ln for ln in f if '"update"' in ln or json.loads(ln).get("ts", 0) > keep_after]
            with open(p + ".tmp", "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.replace(p + ".tmp", p)
        except (OSError, ValueError) as e:
            log.warning("收件箱清理失败：%s", e)

    # ---- 整理 ----
    def classify(self, now: float | None = None) -> str:
        """把还没整理的消息交给模型；返回说明。没有新消息时什么都不做。"""
        if not self.complete:
            return "没有可用的模型"
        now = now or time.time()
        with self._lock:
            since = float(self.meta.get("classified_until") or 0)
            msgs = [m for m in self._inbox(since) if m["ts"] <= now - 30]   # 最近 30 秒的可能还在补识别
            if not msgs:
                return "没有新消息"
            made = touched = 0
            for i in range(0, len(msgs), BATCH):
                part = msgs[i:i + BATCH]
                try:
                    out = self._classify_batch(part)
                except Exception as e:
                    self.meta["last_error"] = f"{type(e).__name__}: {e}"[:200]
                    self._save()
                    raise
                for it in out:
                    if self._merge(it, part):
                        made += 1
                    else:
                        touched += 1
                self.meta["classified_until"] = part[-1]["ts"]
                self.meta["last_error"] = ""
                self._save()
            self.meta["classified_at"] = now
            self._save()
            self._trim_inbox(now - 7 * 86400)
            return f"整理了 {len(msgs)} 条消息：新反馈 {made} 条，更新已有 {touched} 条"

    def _classify_batch(self, part: list[dict]) -> list[dict]:
        recent = [i for i in self.items if i["status"] != "ignored" and i["last_seen"] > time.time() - 14 * 86400]
        existing = "\n".join(f"#{i['id']} [{i['category']}] {i['title']}" for i in recent[-60:]) or "（无）"
        lines = []
        for n, m in enumerate(part, 1):
            role = {"owner": "（主人）", "bot": "（bot）"}.get(m.get("role") or "", "")
            lines.append(f"[{n}] {_hm(m['ts'])} {m.get('name') or '群友'}{role}：{m['text']}")
        raw = self.complete(SYSTEM.format(existing=existing), "\n".join(lines))
        data = _json_block(raw)
        out = []
        for it in data.get("items") or []:
            if not isinstance(it, dict) or it.get("category") not in CATEGORIES or not str(it.get("title") or "").strip():
                continue
            idx = [int(x) for x in it.get("messages") or [] if str(x).isdigit() and 1 <= int(x) <= len(part)]
            it["_msgs"] = [part[k - 1] for k in idx]
            out.append(it)
        return out

    def _merge(self, it: dict, part: list[dict]) -> bool:
        """并入已有条目或新建；返回是否新建。"""
        msgs = it.get("_msgs") or []
        users = [m for m in msgs if not m.get("role")]
        ts = max((m["ts"] for m in msgs), default=part[-1]["ts"])
        ref = it.get("ref")
        cur = next((x for x in self.items if str(x["id"]) == str(ref)), None) if ref not in (None, "", "null") else None
        new = cur is None
        if new:
            nid = int(self.meta.get("next_id") or 1)
            self.meta["next_id"] = nid + 1
            cur = {"id": nid, "category": it["category"], "title": str(it["title"]).strip()[:40],
                   "detail": str(it.get("detail") or "").strip()[:200], "platform": it.get("platform") or "未知",
                   "status": "new", "first_seen": min((m["ts"] for m in msgs), default=ts), "last_seen": ts,
                   "count": 0, "reporters": [], "quotes": [], "issue": None, "issue_score": 0, "chats": []}
            self.items.append(cur)
        else:
            if it.get("detail") and len(str(it["detail"])) > len(cur.get("detail") or ""):
                cur["detail"] = str(it["detail"]).strip()[:200]
            cur["last_seen"] = max(cur["last_seen"], ts)
        cur["count"] += max(1, len(users)) if users or new else 0
        known = {r["wxid"] for r in cur["reporters"]}
        for m in users:
            if m["sender"] not in known:
                cur["reporters"].append({"wxid": m["sender"], "name": m.get("name") or ""})
                known.add(m["sender"])
        for m in msgs:
            q = f"{_hm(m['ts'])} {m.get('name') or ''}：{m['text'][:200]}"
            if q not in cur["quotes"]:
                cur["quotes"].append(q)
        cur["quotes"] = cur["quotes"][-8:]
        for m in msgs:
            if m["chat"] not in cur["chats"]:
                cur["chats"].append(m["chat"])
        if cur["category"] == "question" and it.get("answered") and cur["status"] == "new":
            cur["status"] = "answered"
        self._match(cur)
        return new

    def _match(self, cur: dict) -> None:
        if not self.fuyu or cur.get("issue") or cur["status"] == "ignored":
            return
        try:
            hits = self.fuyu.find(f"{cur['title']} {cur.get('detail') or ''}", k=1)
        except Exception as e:
            log.warning("反馈查重失败：%s", e)
            return
        if hits and hits[0][0] >= MATCH_FLOOR:
            cur["issue"], cur["issue_score"] = hits[0][1]["number"], hits[0][0]
            if cur["status"] == "new":
                cur["status"] = "known"

    # ---- 查询与操作 ----
    def get(self, iid: int) -> dict | None:
        return next((x for x in self.items if x["id"] == iid), None)

    def ignore(self, iid: int) -> str:
        with self._lock:
            it = self.get(iid)
            if not it:
                return f"没有 #{iid}"
            it["status"] = "ignored"
            self._save()
            return f"#{iid}「{it['title']}」已忽略，不再出现在摘要和群小结里"

    def progress(self, it: dict) -> str:
        """给主人看的进度（带 issue 编号）。"""
        if it.get("issue") and self.fuyu:
            try:
                return self.fuyu.status(it["issue"])
            except Exception:
                pass
        return {"new": "新问题，GitHub 上还没有对应 issue", "answered": "群里已解答",
                "ignored": "已忽略"}.get(it["status"], it["status"])

    def public_state(self, it: dict) -> str:
        """给群里看的处理状态：不带 issue 编号和人名。"""
        if it["status"] == "answered":
            return "已在群里解答"
        if it.get("issue") and self.fuyu:
            try:
                s = self.fuyu.status(it["issue"])
            except Exception:
                s = ""
            m = re.search(r"包含在 (v[\w.-]+)", s)
            if m:
                return f"已在 {m.group(1)} 修复，更新到最新版即可"
            if "测试版" in s and "已包含" in s:
                b = re.search(r"测试版 (v[\w.-]+)", s)
                return f"已修复，测试版 {b.group(1)} 可用，正式版即将发布" if b else "已修复，等待正式版发布"
            if "还没发版" in s:
                return "已修好，会在下个版本发布"
            if "不计划处理" in s:
                return "已记录（暂不计划处理）"
            return "已在跟进"
        return "已记录"

    def detail(self, iid: int) -> str:
        it = self.get(iid)
        if not it:
            return f"没有 #{iid}"
        who = "、".join(r["name"] or r["wxid"] for r in it["reporters"]) or "（只有主人或 bot 提到）"
        lines = [f"#{it['id']} [{CATEGORIES[it['category']]}] {it['title']}",
                 it.get("detail") or "",
                 f"平台：{it.get('platform') or '未知'}；提到 {it['count']} 次；反馈人：{who}",
                 f"首次 {time.strftime('%m-%d %H:%M', time.localtime(it['first_seen']))}，"
                 f"最近 {time.strftime('%m-%d %H:%M', time.localtime(it['last_seen']))}",
                 "进度：" + self.progress(it)]
        if it["quotes"]:
            lines.append("原话：\n" + "\n".join("  " + q for q in it["quotes"][-5:]))
        return "\n".join(x for x in lines if x)

    def listing(self, days: int = 7) -> str:
        cut = time.time() - days * 86400
        items = [i for i in self.items if i["last_seen"] >= cut and i["status"] != "ignored"]
        if not items:
            return f"最近 {days} 天没有整理出反馈。"
        items.sort(key=lambda i: -i["last_seen"])
        return f"最近 {days} 天的反馈（/反馈 编号 看详情，/忽略 编号）：\n" + "\n".join(self._line(i) for i in items[:30])

    def _line(self, it: dict) -> str:
        n = len(it["reporters"])
        heat = f"（{n} 人）" if n > 1 else ""
        tag = f" → issue #{it['issue']}" if it.get("issue") else (" → 群里已解答" if it["status"] == "answered" else " → 新")
        return f"#{it['id']} [{CATEGORIES[it['category']]}] {it['title']}{heat}{tag}"

    # ---- 摘要与小结 ----
    def digest(self, since: float) -> str:
        """给主人的摘要：since 之后有动静的条目。"""
        items = [i for i in self.items if i["last_seen"] >= since and i["status"] != "ignored"]
        head = f"【浮屿群反馈摘要】{time.strftime('%m-%d %H:%M', time.localtime(since))} 以来"
        if not items:
            return head + "没有新的反馈。"
        out = [head + f"共 {len(items)} 条（/反馈 编号 看原话，/忽略 编号 不再提醒）："]
        for cat, zh in CATEGORIES.items():
            group = sorted((i for i in items if i["category"] == cat), key=lambda i: -len(i["reporters"]))
            if not group:
                continue
            out.append(f"\n{zh}：")
            for i in group:
                out.append(self._line(i))
                if i.get("issue"):
                    out.append("    " + self.progress(i))
        if self.meta.get("last_error"):
            out.append(f"\n（上次整理出错：{self.meta['last_error']}）")
        return "\n".join(out)

    def group_summary(self, day: str | None = None) -> str:
        """给群里的当天小结；没有反馈返回空串（不发）。"""
        day = day or _day()
        items = [i for i in self.items if i["status"] != "ignored"
                 and (_day(i["first_seen"]) == day or _day(i["last_seen"]) == day)]
        if not items:
            return ""
        m, d = day[5:7].lstrip("0"), day[8:10].lstrip("0")
        out = [f"【今日反馈小结】{m}月{d}日", f"今天大家提了 {len(items)} 条反馈，都已经记下来了："]
        n = 0
        for cat, zh in CATEGORIES.items():
            group = [i for i in items if i["category"] == cat]
            if not group:
                continue
            out.append(f"\n{zh}")
            for i in group:
                n += 1
                out.append(f"{n}. {i['title']} —— {self.public_state(i)}")
        out.append("\n谢谢大家！还有遗漏的，直接在群里说就行。")
        return "\n".join(out)

    def due(self, kind: str, at: str, now: float | None = None) -> bool:
        """kind = digest / group：今天到点了、还没发过、也没晚太久。"""
        hm = _parse_time(at)
        if not hm:
            return False
        now = now or time.time()
        lt = time.localtime(now)
        target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hm[0], hm[1], 0, 0, 0, -1))
        return target <= now < target + LATE_LIMIT and self.meta.get(f"{kind}_day") != _day(now)

    def mark(self, kind: str, now: float | None = None) -> None:
        with self._lock:
            self.meta[f"{kind}_day"] = _day(now)
            self.meta[f"{kind}_at"] = now or time.time()
            self._save()

    def brief(self) -> str:
        open_ = sum(1 for i in self.items if i["status"] in ("new", "known"))
        at = self.meta.get("classified_at")
        when = time.strftime("%m-%d %H:%M", time.localtime(at)) if at else "从未"
        err = f"；上次出错：{self.meta['last_error']}" if self.meta.get("last_error") else ""
        return f"{len(self.items)} 条（待处理 {open_}），整理于 {when}{err}"
