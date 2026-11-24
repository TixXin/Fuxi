"""反馈群里「营业」：新人进群欢迎、冷场时每天说一句、修好后 @ 反馈人。只在反馈群（设置「反馈群」）里做。

  进群欢迎   读到「…加入群聊」系统消息（扫码进群的 adder、被邀请的 names）后等 1 分钟，把这期间进群的人
             合在一条欢迎里（一个人就真 @ 他）。营业时段外进群的，等到营业时段再欢迎；超过 12 小时就算了。
  冷场说一句 营业时段内，群里超过「冷场时长」没人说话，就发一条（每天最多一次，所以时间跟着冷场走）。
             内容轮换：最近修了什么 / 一个功能小技巧（产品助手资料里的 README、docs/features）/ 邀请反馈，
             由内核模型按素材写，失败就用固定的一句。群小结前后 1 小时不发，免得连着刷屏。
  修复通知   浮屿发正式版时，找出群反馈里对应 issue 已包含在这个版本的条目，@ 当初反馈的人。
             「修复通知」= 预览（默认，走发版公告：先发到文件传输助手，/发布 才发群）| 直接发 | 关闭。

状态存 /state/outreach/state.json：各群最后有人说话的时间、今天冷场发过没、待欢迎的人、用过的小技巧。
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

from . import feedback as fb
from .fuyu import _ts as fuyu_ts

if TYPE_CHECKING:
    from .app import Bridge
    from .messages import Message

log = logging.getLogger("fuxi.outreach")

WELCOME_WAIT = 60
WELCOME_MAX_AGE = 12 * 3600
SUMMARY_GAP = 3600

WELCOME = ("欢迎{who}！我是浮汐，浮屿的群助手，24 小时在线～用浮屿遇到问题、有想法或建议，直接在群里说就行，"
           "@我也可以，我都会整理给作者。")
IDLE_FALLBACK = "有点安静～用浮屿遇到什么问题、或者有想要的功能，随时在群里说，@我也行，我会整理给作者。"

IDLE_SYSTEM = """你是「浮汐」，浮屿（一款桌面灵动岛软件，支持 Windows 和 macOS）用户群里的助手，24 小时在线。
群里冷场了一阵，你要发一条自然的群消息。
要求：1~3 句，不超过 90 字；口语、亲切，像群里的熟人随口一说；不要用「大家好」开头，不要像广告，emoji 最多 1 个。
只能用下面给的素材，不要编造功能、版本号、数字或日期。
结尾自然地带一句：用浮屿遇到问题或有想法，可以直接在群里说或者 @你。
只输出这条消息本身，不要加引号。"""

_JOIN = re.compile(r"加入(了)?群聊")


def parse_join(text: str) -> list[dict]:
    """「…加入群聊」系统消息 → 新进群的人 [{wxid, name}]；不是进群消息返回 []。"""
    i = (text or "").find("<sysmsg")
    if i < 0:
        return []
    try:
        root = ET.fromstring(text[i:])
    except ET.ParseError:
        return []
    if not _JOIN.search(root.findtext(".//template") or ""):
        return []
    out = []
    for link in root.iter("link"):
        if link.get("name") not in ("adder", "names"):   # adder：扫码进群的人；names：被邀请的人
            continue
        for mem in link.iter("member"):
            w, n = (mem.findtext("username") or "").strip(), (mem.findtext("nickname") or "").strip()
            if w or n:
                out.append({"wxid": w, "name": n})
    return out


def parse_window(s: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)-([01]?\d|2[0-3]):([0-5]\d)", (s or "").strip())
    if not m:
        return None
    a, b, c, d = map(int, m.groups())
    return a * 60 + b, c * 60 + d


def in_window(window: str, now: float) -> bool:
    w = parse_window(window)
    if not w:
        return True
    lt = time.localtime(now)
    cur = lt.tm_hour * 60 + lt.tm_min
    start, end = w
    return start <= cur < end if start <= end else (cur >= start or cur < end)


def release_points(body: str, n: int = 6) -> list[str]:
    """版本说明里「更新内容」下的要点（去掉 Markdown）。"""
    body = body or ""
    i = body.find("更新内容")
    part = body[i:] if i >= 0 else body
    out = []
    for line in part.splitlines():
        line = line.strip()
        if line.startswith(("- ", "* ")):
            t = re.sub(r"\*\*([^*]+)\*\*", r"\1", line[2:])
            t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t).replace("`", "")
            out.append(t[:70])
        if len(out) >= n:
            break
    return out


class Outreach:
    def __init__(self, bridge: "Bridge", root: str):
        self.b = bridge
        self.root = root
        os.makedirs(root, mode=0o700, exist_ok=True)
        self._lock = threading.Lock()
        try:
            with open(os.path.join(root, "state.json"), encoding="utf-8") as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            self.state = {}
        for k in ("active", "idle_day", "joins", "posted"):
            self.state.setdefault(k, {})
        self.state.setdefault("tips", [])

    def _save(self) -> None:
        p = os.path.join(self.root, "state.json")
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(p + ".tmp", p)

    @property
    def on(self) -> bool:
        return bool(self.b.cfg.get("outreach")) and bool(self.b.cfg.get("feedback_groups"))

    # ---- 观察 ----
    def observe(self, m: "Message") -> None:
        """反馈群的每条消息都过一遍：记最后有人说话的时间；进群消息排队欢迎。"""
        with self._lock:
            if m.type == "system":
                joined = [j for j in parse_join(m.text) if j["wxid"] != self.b.self_wxid]
                if joined:
                    q = self.state["joins"].setdefault(m.chat, [])
                    q += [{**j, "ts": float(m.create_time)} for j in joined]
                    self._save()
                    log.info("%s 有 %d 人进群，稍后欢迎", self.b.name_of(m.chat), len(joined))
                return
            if m.type == "revoke" or not m.text.strip():
                return
            self.state["active"][m.chat] = max(float(m.create_time), float(self.state["active"].get(m.chat) or 0))
            if not m.is_self:   # 自己说的不用存盘那么勤
                self._save()

    def bot_posted(self, chat: str, now: float | None = None) -> None:
        with self._lock:
            self.state["posted"][chat] = now or time.time()
            self.state["active"][chat] = now or time.time()
            self._save()

    # ---- 定时 ----
    def tick(self, now: float | None = None) -> None:
        if not self.on:
            return
        now = now or time.time()
        window = self.b.cfg.get("open_hours")
        if not in_window(window, now):
            return
        self._welcome_due(now)
        self._idle_due(now)

    def _welcome_due(self, now: float) -> None:
        for chat, q in list(self.state["joins"].items()):
            q = [j for j in q if now - j["ts"] < WELCOME_MAX_AGE]
            if not q:
                self.state["joins"].pop(chat, None)
                continue
            if now - max(j["ts"] for j in q) < WELCOME_WAIT:   # 等一会，把一起进群的合成一条
                continue
            with self._lock:
                self.state["joins"].pop(chat, None)
                self._save()
            if not self.b.is_feedback_group(chat):
                continue
            self.send_welcome(chat, q, now)

    def welcome_text(self, chat: str, joined: list[dict]) -> tuple[str, str]:
        """返回（正文, 真 @ 的名字）。一个人：真 @ 他；多人：文字 @ 列出来（最多 5 个）。"""
        names = []
        for j in joined:
            n = self._member_name(chat, j["wxid"]) or j["name"]
            if n and n not in names:
                names.append(n)
        if len(names) == 1:
            return WELCOME.format(who="新朋友"), names[0]
        who = " ".join("@" + n for n in names[:5]) + (" 等新朋友" if len(names) > 5 else "") if names else "新朋友"
        return WELCOME.format(who=" " + who if names else who), ""

    def send_welcome(self, chat: str, joined: list[dict], now: float | None = None) -> None:
        from .app import SendJob
        text, at = self.welcome_text(chat, joined)
        job = self.b.send(SendJob(chat=chat, text=text, at=at, source="api"))
        if job.ok:
            self.bot_posted(chat, now)
            log.info("已欢迎 %s 的 %d 位新人", self.b.name_of(chat), len(joined))
        else:
            log.warning("进群欢迎没发出去：%s", job.error)

    def _near_summary(self, now: float) -> bool:
        hm = fb._parse_time(self.b.cfg.get("group_summary_time"))
        if not hm:
            return False
        lt = time.localtime(now)
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hm[0], hm[1], 0, 0, 0, -1))
        return abs(now - t) < SUMMARY_GAP

    def _idle_due(self, now: float) -> None:
        idle = max(1, int(self.b.cfg.get("idle_hours") or 2)) * 3600
        today = fb._day(now)
        for chat, last in list(self.state["active"].items()):
            if self.state["idle_day"].get(chat) == today or now - float(last) < idle:
                continue
            if now - float(self.state["posted"].get(chat) or 0) < SUMMARY_GAP or self._near_summary(now):
                continue
            if not self.b.is_feedback_group(chat) or not self.b._allowed_chat(chat):
                continue
            with self._lock:
                self.state["idle_day"][chat] = today   # 先记上：失败也不在今天反复重试
                self._save()
            text = self.idle_text(now)
            from .app import SendJob
            job = self.b.send(SendJob(chat=chat, text=text, source="api"))
            if job.ok:
                self.bot_posted(chat, now)
                log.info("%s 冷场 %.1f 小时，说了一句", self.b.name_of(chat), (now - float(last)) / 3600)
            else:
                log.warning("冷场发言没发出去：%s", job.error)

    # ---- 冷场发言的内容 ----
    def _materials(self, now: float) -> dict[str, str]:
        out: dict[str, str] = {}
        fy = getattr(self.b, "fuyu", None)
        if fy:
            rels = [r for r in fy.releases if not r.get("prerelease")
                    and now - fuyu_ts(r.get("published_at")) < 10 * 86400][:2]
            if rels:
                lines = []
                for r in rels:
                    pts = release_points(r.get("body") or "")
                    lines.append(f"{r['tag_name']}（{fb._day(fuyu_ts(r['published_at']))} 发布）"
                                 + ("：\n" + "\n".join("- " + p for p in pts) if pts else ""))
                out["fixes"] = "最近的版本：\n" + "\n".join(lines)
        tips = self._tip_chunks()
        if tips:
            c = random.choice(tips)
            out["tip"] = f"一个功能小技巧（来自浮屿说明文档「{c.title}」）：\n{c.text[:500]}"
            out["_tip_title"] = c.title
        week = [i for i in self.b.feedback.items if now - i["last_seen"] < 7 * 86400 and i["status"] != "ignored"]
        fixed = [i for i in week if i.get("fixed_in") or "包含在" in (self.b.feedback.progress(i) if i.get("issue") else "")]
        # 没有反馈时不提数字（「没人提反馈」读起来像在抱怨），只邀请大家聊聊用得怎么样
        out["invite"] = (f"最近 7 天群里整理了 {len(week)} 条反馈，其中 {len(fixed)} 条已经修好发版。谢谢大家，欢迎继续提。"
                         if fixed else "邀请大家聊聊最近用浮屿的感受：哪里好用、哪里别扭、还想要什么功能。")
        return out

    def _tip_chunks(self) -> list:
        kn = getattr(self.b, "knowledge", None)
        chunks = getattr(getattr(kn, "index", None), "chunks", None) or []
        used = set(self.state.get("tips") or [])
        ok = [c for c in chunks if (c.source == "README.md" or c.source.startswith("docs/features/"))
              and len(c.text) > 60]
        fresh = [c for c in ok if c.title not in used]
        return fresh or ok

    def idle_text(self, now: float | None = None) -> str:
        now = now or time.time()
        mats = self._materials(now)
        kinds = [k for k in ("fixes", "tip", "invite") if k in mats]
        kind = kinds[int(now // 86400) % len(kinds)]
        try:
            text = self.b.kernel.complete(IDLE_SYSTEM, f"素材：\n{mats[kind]}", 400)
        except Exception as e:
            log.warning("冷场发言生成失败，用固定的一句：%s", e)
            return IDLE_FALLBACK
        text = text.strip().strip("\"“”「」'")
        if not text or len(text) > 200:
            return IDLE_FALLBACK
        if kind == "tip" and mats.get("_tip_title"):
            with self._lock:
                self.state["tips"] = (self.state["tips"] + [mats["_tip_title"]])[-40:]
                self._save()
        return text

    # ---- 修复通知 ----
    def _member_name(self, chat: str, wxid: str) -> str:
        if not wxid:
            return ""
        try:
            return self.b.reader.member_name(chat, wxid) or ""
        except Exception:
            return ""

    def fix_notes(self, tag: str) -> list[tuple[str, str, str, list[dict]]]:
        """这个版本修好的群反馈 → [(chat, 正文, 真 @ 的名字, 条目)]，按群分。"""
        items = self.b.feedback.fixed_in(tag)
        by_chat: dict[str, list[dict]] = {}
        for it in items:
            for chat in it.get("chats") or []:
                if self.b.is_feedback_group(chat):
                    by_chat.setdefault(chat, []).append(it)
        out = []
        for chat, its in by_chat.items():
            names: list[str] = []
            for it in its:
                for r in it["reporters"]:
                    n = self._member_name(chat, r["wxid"]) or r["name"]
                    if n and n not in names:
                        names.append(n)
            at = names[0] if names else ""
            others = " ".join("@" + n for n in names[1:8])
            lines = [f"{k}. {it['title']}" for k, it in enumerate(its, 1)]
            text = (f"{others + ' ' if others else ''}浮屿 {tag} 发布了，修好了之前在群里反馈的：\n" + "\n".join(lines)
                    + "\n更新到最新版试试，还有问题随时在群里说～")
            out.append((chat, text, at, its))
        return out

    def on_release(self, tag: str) -> None:
        mode = self.b.cfg.get("fix_notify")
        if mode == "off" or not self.b.cfg.get("feedback_groups"):
            return
        from .app import SendJob
        for chat, text, at, its in self.fix_notes(tag):
            self.b.feedback.mark_fixed(its, tag)
            if mode == "preview":
                try:
                    self.b.announcer.create(f"fix-{tag}-{chat}", chat, text, None, at=at)
                    log.info("修复通知 %s → %s 已发预览，等 /发布", tag, self.b.name_of(chat))
                except Exception as e:
                    log.warning("修复通知预览失败：%s", e)
                continue
            job = self.b.send(SendJob(chat=chat, text=text, at=at, source="api"))
            if job.ok:
                self.bot_posted(chat)
                log.info("修复通知 %s 已发到 %s", tag, self.b.name_of(chat))
            else:
                log.warning("修复通知没发出去：%s", job.error)

    # ---- 遥控 ----
    def brief(self, now: float | None = None) -> str:
        now = now or time.time()
        parts = []
        for chat, last in self.state["active"].items():
            if not self.b.is_feedback_group(chat):
                continue
            done = "今天已说过" if self.state["idle_day"].get(chat) == fb._day(now) else "今天还没说"
            parts.append(f"{self.b.name_of(chat)}：{(now - float(last)) / 3600:.1f} 小时没人说话，冷场发言{done}")
        waiting = sum(len(q) for q in self.state["joins"].values())
        if waiting:
            parts.append(f"待欢迎 {waiting} 人")
        return "；".join(parts) or "还没观察到反馈群的消息"
