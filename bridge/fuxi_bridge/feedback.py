"""群反馈收集（F1，见 docs/feedback-loop.md）：把反馈群里的聊天整理成 bug / 建议 / 提问，
中午私聊主人发摘要，晚上在群里发当天小结。

流程：
  收集   桥接读到反馈群的每条消息（不管有没有 @ bot、在不在静默时段）都追加到 inbox.jsonl；
         看图 / 语音识别完成后再补上识别结果。
  整理   每小时（以及发摘要、小结前）把还没整理的消息成批交给内核的模型（/api/fuxi/complete），
         让它找出对浮屿的反馈：归到已有条目，或新建条目。谁提的由消息序号反查，不让模型写人名。
  对照   bug / 建议用 F2 的浮屿仓库缓存按字面相似挑 8 个候选 issue，再让模型判断是不是同一件事，
         对上了就附进度（已修于 vX / 跟进中）。提问不对应 issue。
  摘要   每天 digest_time（默认 12:00）私聊主人：过去 24 小时有动静的条目，带编号。
  小结   每天 group_time（默认 20:00）在反馈群发当天小结：只有标题和处理状态，不带任何人名。
         没有反馈就不发。错过时间 2 小时以上（比如那会儿桥接在重启）当天就不补发了。

存在 /state/feedback/：
  inbox.jsonl  原始消息 {mid, ts, chat, sender, name, text, role}，以及识别补写 {mid, update}
  items.json   条目 [{id, category, title, detail, platform, status, first_seen, last_seen, count,
                       reporters[{wxid, name}], quotes[], issue, issue_score, chats[]}]
  meta.json    classified_until、next_id、digest_day、group_day、last_error
状态：new（新）| known（GitHub 上已有 issue）| answered（群里已解答的提问）| fixed（已随某个正式版发布，fixed_in）
     | ignored（主人 /忽略）
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
MATCH_FLOOR = 0.5      # 没有模型时只靠字面相似度，阈值放高宁可漏配
CANDIDATES = 8         # 每条反馈交给模型判断的候选 issue 数
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

MATCH_SYSTEM = """判断浮屿用户在群里的反馈，是不是 GitHub 上已有的某个 issue 说的同一件事（同一个故障，或同一个需求）。
每条反馈后面列了几个候选 issue（字面相似挑出来的，大多数可能无关）。
只在确定是同一件事时才对应；只是同一个模块、相关但不是同一个问题的，不要对应。
只输出 JSON，不要解释、不要代码块围栏：{"matches":[{"id":反馈编号,"issue":issue编号或null}]}"""


def _day(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))


def _hm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def fb_code(iid: int) -> str:
    """反馈展示编号：用 FB-1 避免在 GitHub Issue 里被自动链接到 #1 号老 Issue。"""
    return f"FB-{iid}"


def parse_fb_id(raw: str) -> int | None:
    """解析 1 / #1 / FB-1 / fb1 → 整数编号。"""
    s = (raw or "").strip()
    m = re.fullmatch(r"(?:fb-?|#)?(\d+)", s, re.I)
    return int(m.group(1)) if m else None


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
    def _media_dir(self) -> str:
        d = os.path.join(self.root, "media")
        os.makedirs(d, mode=0o700, exist_ok=True)
        return d

    def save_media(self, key: str, data: bytes) -> str:
        """把解密后的反馈图片存到 /state/feedback/media/<safe>.jpg，返回文件名。"""
        safe = re.sub(r"[^a-zA-Z0-9_-]", "", str(key or ""))[:48] or f"img_{int(time.time() * 1000)}"
        fname = f"{safe}.jpg"
        p = os.path.join(self._media_dir(), fname)
        if not os.path.exists(p):
            tmp = p + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, p)
        return fname

    def media_path(self, fname: str) -> str | None:
        if not fname or "/" in fname or "\\" in fname or fname in (".", ".."):
            return None
        p = os.path.join(self.root, "media", fname)
        return p if os.path.isfile(p) else None

    def image_paths(self, it: dict) -> list[tuple[str, str]]:
        """返回条目已保存的本地图片 [(filename, abs_path), ...]。"""
        out: list[tuple[str, str]] = []
        for fname in it.get("images") or []:
            p = self.media_path(fname)
            if p:
                out.append((fname, p))
        return out

    def add(self, mid: str, ts: float, chat: str, sender: str, name: str, text: str, role: str = "",
            media: str = "") -> None:
        """role：owner（主人）/ bot / 空（群友）。"""
        text = (text or "").strip()
        if not text:
            return
        rec = {"mid": mid, "ts": ts, "chat": chat, "sender": sender, "name": name, "text": text[:1500], "role": role}
        if media:
            rec["media"] = media
        with self._lock, open(self._path("inbox.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def annotate(self, mid: str, text: str) -> None:
        """看图 / 语音识别完成后，用带识别结果的文字替换原来的占位。"""
        with self._lock, open(self._path("inbox.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"mid": mid, "update": text[:1500]}, ensure_ascii=False) + "\n")

    def annotate_media(self, mid: str, media: str) -> None:
        """图片解密落盘后，把文件名补挂到原消息记录上。"""
        if not media:
            return
        with self._lock, open(self._path("inbox.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"mid": mid, "media_update": media}, ensure_ascii=False) + "\n")

    def _inbox(self, since: float) -> list[dict]:
        recs: dict[str, dict] = {}
        updates: dict[str, str] = {}
        media_updates: dict[str, str] = {}
        try:
            with open(self._path("inbox.jsonl"), encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if "update" in r:
                        updates[r["mid"]] = r["update"]
                    elif "media_update" in r:
                        media_updates[r["mid"]] = r["media_update"]
                    elif r.get("ts", 0) > since:
                        recs[r["mid"]] = r
        except OSError:
            return []
        for mid, t in updates.items():
            if mid in recs:
                recs[mid]["text"] = t
        for mid, m in media_updates.items():
            if mid in recs:
                recs[mid]["media"] = m
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
            fresh: list[dict] = []
            for i in range(0, len(msgs), BATCH):
                part = msgs[i:i + BATCH]
                try:
                    out = self._classify_batch(part)
                except Exception as e:
                    self.meta["last_error"] = f"{type(e).__name__}: {e}"[:200]
                    self._save()
                    raise
                for it in out:
                    new, cur = self._merge(it, part)
                    made += new
                    touched += not new
                    if not cur.get("issue") and cur not in fresh:
                        fresh.append(cur)
                self.meta["classified_until"] = part[-1]["ts"]
                self.meta["last_error"] = ""
                self._save()
            self._match(fresh)
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

    def _merge(self, it: dict, part: list[dict]) -> tuple[bool, dict]:
        """并入已有条目或新建；返回（是否新建，条目）。"""
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
            med = m.get("media")
            if med:
                imgs = cur.setdefault("images", [])
                if med not in imgs:
                    imgs.append(med)
                cur["images"] = imgs[-6:]
        if cur["category"] == "question" and it.get("answered") and cur["status"] == "new":
            cur["status"] = "answered"
        return new, cur

    def _candidates(self, cur: dict) -> list[tuple[float, dict]]:
        quotes = " ".join(q.split("：", 1)[-1] for q in cur.get("quotes") or [])
        seen: dict[int, tuple[float, dict]] = {}
        for q in (cur["title"], f"{cur['title']} {cur.get('detail') or ''} {quotes}"):
            for score, issue in self.fuyu.find(q, k=CANDIDATES):
                if issue["number"] not in seen or seen[issue["number"]][0] < score:
                    seen[issue["number"]] = (score, issue)
        return sorted(seen.values(), key=lambda x: -x[0])[:CANDIDATES]

    def _match(self, items: list[dict]) -> None:
        """给还没对应 issue 的 bug / 建议找 GitHub 上的同一个 issue：字面相似挑候选，模型判断是不是同一件事。"""
        items = [i for i in items if i["category"] != "question" and i["status"] != "ignored" and not i.get("issue")]
        if not self.fuyu or not items:
            return
        try:
            cands = {i["id"]: self._candidates(i) for i in items}
        except Exception as e:
            log.warning("反馈查重失败：%s", e)
            return
        picks: dict[int, int] = {}
        if self.complete:
            blocks = []
            for i in items:
                lines = [f"反馈 #{i['id']} [{i['category']}] {i['title']}。{i.get('detail') or ''}"]
                lines += [f"  候选 #{c['number']}（{'未关闭' if c['state'] == 'open' else '已关闭'}）{c['title']}"
                          for _, c in cands[i["id"]]]
                blocks.append("\n".join(lines))
            try:
                data = _json_block(self.complete(MATCH_SYSTEM, "\n\n".join(blocks)))
                valid = {iid: {c["number"] for _, c in cs} for iid, cs in cands.items()}
                for m in data.get("matches") or []:
                    iid, num = m.get("id"), m.get("issue")
                    if isinstance(iid, int) and isinstance(num, int) and num in valid.get(iid, ()):
                        picks[iid] = num
            except Exception as e:   # 模型判断失败：退回只认字面相似度很高的
                log.warning("反馈查重（模型）失败：%s", e)
                picks = {iid: cs[0][1]["number"] for iid, cs in cands.items() if cs and cs[0][0] >= MATCH_FLOOR}
        else:
            picks = {iid: cs[0][1]["number"] for iid, cs in cands.items() if cs and cs[0][0] >= MATCH_FLOOR}
        for i in items:
            if i["id"] in picks:
                i["issue"] = picks[i["id"]]
                i["issue_score"] = next((s for s, c in cands[i["id"]] if c["number"] == i["issue"]), 0)
                if i["status"] == "new":
                    i["status"] = "known"

    # ---- 查询与操作 ----
    def get(self, iid: int) -> dict | None:
        return next((x for x in self.items if x["id"] == iid), None)

    def ignore(self, iid: int) -> str:
        with self._lock:
            it = self.get(iid)
            if not it:
                return f"没有 {fb_code(iid)}"
            it["status"] = "ignored"
            self._save()
            return f"{fb_code(iid)}「{it['title']}」已忽略，不再出现在摘要和群小结里"

    def fixed_in(self, tag: str) -> list[dict]:
        """对应 issue 已包含在正式版 tag 里、还没通知过的条目（修复通知用）。"""
        out = []
        for it in self.items:
            if it["status"] == "ignored" or not it.get("issue") or it.get("fixed_in") or not self.fuyu:
                continue
            try:
                s = self.fuyu.status(it["issue"])
            except Exception:
                continue
            if f"包含在 {tag}（" in s:
                out.append(it)
        return out

    def mark_fixed(self, items: list[dict], tag: str) -> None:
        with self._lock:
            for it in items:
                it["fixed_in"], it["status"] = tag, "fixed"
            self._save()

    def mark_issue(self, it: dict, number: int, agent: bool = False, local: bool = False,
                   target: str = "", mode: str = "bg") -> None:
        """/fix、/issue 开出（或对上）issue 后记下；agent=True 表示已派给修复代理。"""
        with self._lock:
            it["issue"], it["issue_score"] = number, 1
            if it["status"] in ("new", "answered"):
                it["status"] = "known"
            if agent:   # 重新派（比如补充线索后再 /fix）：清掉上一次的结果
                where = target or ("mac" if local else "cloud")
                it["agent_at"] = time.time()
                it["agent_where"] = where
                it["agent_mode"] = mode if where in ("mac", "win", "local") else "bg"
                for k in ("agent_pr", "agent_stuck", "agent_late", "agent_done"):
                    it.pop(k, None)
            self._save()

    def agent_items(self, refs: list[int]) -> list[dict]:
        """派给代理、对应 issue 在 refs 里的条目（代理出 draft PR / 没修成时通知主人用）。"""
        return [i for i in self.items if i.get("agent_at") and i.get("issue") in set(refs)]

    def agent_waiting(self) -> list[dict]:
        """派给代理、还没有结果（没出 PR、没说修不了、对应 issue 也未关闭）的条目。"""
        return [i for i in self.items if i.get("agent_at") and not i.get("agent_pr")
                and not i.get("agent_stuck") and not i.get("agent_done")
                and i["status"] != "ignored"]

    def set_fields(self, it: dict, **kw) -> None:
        with self._lock:
            it.update(kw)
            self._save()

    def progress(self, it: dict) -> str:
        """给主人看的进度（带 issue 编号）。"""
        if it.get("issue") and self.fuyu:
            try:
                return self.fuyu.status(it["issue"])
            except Exception:
                pass
        return {"new": "新问题，GitHub 上还没有对应 issue", "known": "已记下", "answered": "群里已解答", "fixed": f"已在 {it.get('fixed_in')} 修复",
                "ignored": "已忽略"}.get(it["status"], it["status"])

    def public_state(self, it: dict) -> str:
        """给群里看的处理状态：不带 issue 编号和人名。"""
        if it["status"] == "answered":
            return "已在群里解答"
        if it["status"] == "fixed" and it.get("fixed_in"):
            return f"已在 {it['fixed_in']} 修复，更新到最新版即可"
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
            return f"没有 {fb_code(iid)}"
        who = "、".join(r["name"] or r["wxid"] for r in it["reporters"]) or "（只有主人或 bot 提到）"
        img_cnt = len(self.image_paths(it))
        lines = [f"{fb_code(it['id'])} [{CATEGORIES[it['category']]}] {it['title']}",
                 it.get("detail") or "",
                 f"平台：{it.get('platform') or '未知'}；提到 {it['count']} 次；反馈人：{who}"
                 + (f"；附图 {img_cnt} 张" if img_cnt else ""),
                 f"首次 {time.strftime('%m-%d %H:%M', time.localtime(it['first_seen']))}，"
                 f"最近 {time.strftime('%m-%d %H:%M', time.localtime(it['last_seen']))}",
                 "进度：" + self.progress(it),
                 self._agent_line(it)]
        if it["quotes"]:
            lines.append("原话：\n" + "\n".join("  " + q for q in it["quotes"][-5:]))
        return "\n".join(x for x in lines if x)

    def _agent_line(self, it: dict) -> str:
        if not it.get("agent_at"):
            return ""
        w = it.get("agent_where") or "cloud"
        fg = it.get("agent_mode") == "fg"
        where = {
            "mac": "Mac 前台修复" if fg else "Mac 本机修复",
            "local": "Mac 前台修复" if fg else "Mac 本机修复",
            "win": "Win 前台修复" if fg else "Win 本机修复",
            "cloud": "云端代理",
        }.get(w, "云端代理")
        head = f"{where}：{time.strftime('%m-%d %H:%M', time.localtime(it['agent_at']))} 派出，"
        if it.get("agent_pr"):
            pr = it["agent_pr"]
            try:
                return head + (self.fuyu.status(pr) if self.fuyu else f"PR #{pr}")
            except Exception:
                return head + f"PR #{pr}"
        if it.get("agent_stuck"):
            return head + f"没修成，查到的情况在 issue #{it['issue']} 的评论里"
        if it.get("agent_done"):
            return head + f"已完成（issue #{it['issue']} 已关闭）"
        issue_num = it.get("issue")
        if self.fuyu and issue_num:
            try:
                prs = [p for p in self.fuyu.fixes_for(issue_num) if p.get("created_at") and p.get("number")]
                if prs:
                    return head + self.fuyu.status(prs[-1]["number"])
            except Exception:
                pass
            issues_map = getattr(self.fuyu, "issues", None) or {}
            cached = issues_map.get(issue_num) or issues_map.get(str(issue_num))
            if cached and cached.get("state") == "closed":
                return head + f"已完成（issue #{issue_num} 已关闭）"
        return head + "还在修" + ("（超过 2 小时没结果，已提醒过）" if it.get("agent_late") else "")

    def listing(self, days: int = 7) -> str:
        cut = time.time() - days * 86400
        items = [i for i in self.items if i["last_seen"] >= cut and i["status"] != "ignored"]
        if not items:
            return f"最近 {days} 天没有整理出反馈。"
        items.sort(key=lambda i: -i["last_seen"])
        return f"最近 {days} 天的反馈（/fb 编号 看详情，/fix 编号 [mac|win] [fg] 修复，/ignore 编号）：\n" + "\n".join(self._line(i) for i in items[:30])

    def _line(self, it: dict) -> str:
        n = len(it["reporters"])
        heat = f"（{n} 人）" if n > 1 else ""
        tag = f" → issue #{it['issue']}" if it.get("issue") else (" → 群里已解答" if it["status"] == "answered" else " → 新")
        return f"{fb_code(it['id'])} [{CATEGORIES[it['category']]}] {it['title']}{heat}{tag}"

    # ---- 摘要与小结 ----
    def digest(self, since: float) -> str:
        """给主人的摘要：since 之后有动静的条目。"""
        items = [i for i in self.items if i["last_seen"] >= since and i["status"] != "ignored"]
        head = f"【浮屿群反馈摘要】{time.strftime('%m-%d %H:%M', time.localtime(since))} 以来"
        if not items:
            return head + "没有新的反馈。"
        out = [head + f"共 {len(items)} 条（/fb 编号 看原话，/ignore 编号 不再提醒）："]
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
        out.append("\n谢谢大家！有问题或想法随时在群里说，@我也行，我 24 小时在线，都会整理给作者。")
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
