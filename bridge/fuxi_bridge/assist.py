"""产品助手：产品助手群里被 @ / 被回复时附上浮屿资料和仓库信息，答不上转人工；
未 @ 时按浮屿相关性分层主动接话（能答上的提问高概率、普通聊浮屿低频带冷却）；有人问源码时直接回仓库地址。"""

from __future__ import annotations

import concurrent.futures as cf
import logging
import random
import re
import threading
import time

from . import knowledge
from .sending import SendJob

log = logging.getLogger("fuxi")

VERSION_Q = re.compile(r"版本|新版|更新|升级|发版|最新|release|v\d+\.\d+", re.I)
BUG_Q = re.compile(r"修(了|好|复)|bug|问题|没反应|不行|不了|不能|看不到|打不开|闪退|崩|卡|报错|失败|异常|不显示|没效果", re.I)
SOURCE_Q = re.compile(r"源码|源代码|开源|source ?code|代码在哪", re.I)
# 未 @ 时判定「明显在聊浮屿」的词；纯通用词（如「更新」「截图」）不放这里，避免把日常聊天误当成浮屿话题
FUYU_TOPIC_RE = re.compile(
    r"浮屿|fuyu|灵动岛|上岛|岛上|岛屿实验室|贴图|文件架|暂存架|cc-?connect|浮汐|fuxi|\bv0\.\d+",
    re.I,
)
ISSUE_NUM_RE = re.compile(r"#(\d{1,6})")


class AssistMixin:
    def _source_asked(self, payload: dict) -> bool:
        """有人直接问 bot 的源码（私聊，或群里 @ / 引用 bot）。问浮屿开不开源的不算。"""
        if not self.cfg.get("source_url"):
            return False
        if payload["chat_type"] == "group" and not (payload.get("at_me") or payload.get("reply_to_bot")):
            return False
        text = payload["text"]
        for n in payload.get("self_names") or []:
            text = text.replace(f"@{n}", " ")
        return bool(SOURCE_Q.search(text)) and not re.search(r"浮屿|fuyu", text, re.I) and len(text.strip()) <= 40

    # ---------- 产品助手 ----------
    def is_assistant_group(self, chat: str) -> bool:
        groups = self.cfg.get("assistant_groups")
        return bool(groups) and chat.endswith("@chatroom") and (chat in groups or self.name_of(chat) in groups)

    @staticmethod
    def _talking_to_other(payload: dict) -> bool:
        """未 @ bot 时，对方是否在 @ 别人或引用回复别人的消息。"""
        if payload.get("at_other"):
            return True
        if payload.get("reply_to_user_id") and not payload.get("reply_to_bot"):
            return True
        self_names = {n.lower() for n in (payload.get("self_names") or []) if n}
        for name in payload.get("mentioned_names") or []:
            if name and name.lower() not in self_names:
                return True
        return False

    def _fuyu_cooldown_ok(self, chat: str, cooldown: float) -> bool:
        if cooldown <= 0:
            return True
        table = getattr(self, "_fuyu_proactive_at", None) or {}
        last = table.get(chat, 0.0)
        return not last or (time.monotonic() - last) >= cooldown

    def _mark_fuyu_proactive(self, chat: str) -> None:
        table = getattr(self, "_fuyu_proactive_at", None)
        if table is None:
            table = {}
            self._fuyu_proactive_at = table
        table[chat] = time.monotonic()

    def _assist(self, chat: str, payload: dict) -> None:
        """产品助手群处理：
        1) 被 @ / 被回复：检索浮屿资料和仓库信息附给内核，答不上且像提问时转主人；
        2) 未被 @：不打断他人对话的前提下，
           - 浮屿提问且资料/仓库真能答上 → 按 fuyu_qa_pct（默认 90%）主动接话并附资料（答不上则安静，不转人工）；
           - 普通聊浮屿（非提问）→ 按 fuyu_chat_pct（默认 20%）+ fuyu_chat_cooldown（默认 240s）偶尔插一句；
           - 与浮屿无关的闲聊 → 不介入，交回内核按普通插话概率（如 3% + 600s 冷却）处理。
        """
        if not self.is_assistant_group(chat):
            return
        q = payload["text"]
        for n in payload.get("self_names") or []:
            q = q.replace(f"@{n}", " ")
        q = q.strip()
        if payload.get("at_me") or payload.get("reply_to_bot"):
            self._assist_addressed(chat, payload, q)
        else:
            self._assist_proactive(chat, payload, q)

    def _assist_addressed(self, chat: str, payload: dict, q: str) -> None:
        try:
            hits = self.knowledge.search(q)
        except Exception as e:   # 检索出错不能挡住回复
            log.warning("资料检索出错：%s", e)
            hits = []
        if hits and knowledge.is_question(q):
            hits = self._judge(q, hits)
        notes, sure = self._fuyu_notes(q)
        extra = ""
        if hits:
            extra += ("\n\n（以下是浮屿的说明资料，回答浮屿用法问题时以它为准；资料里没写的不要编，"
                      "就说不确定、已经转给主人了）\n" + knowledge.render(hits))
            log.info("产品助手 %s：附上 %d 段资料（%s）", self.name_of(chat), len(hits), "、".join(h.title[:20] for h in hits))
        if notes:
            extra += "\n\n（浮屿仓库的最新情况，回答版本、修没修时参考；开发记录未必就是对方说的问题，对不上就别提）\n" + "\n".join(notes)
            log.info("产品助手 %s：附上仓库信息 %d 条", self.name_of(chat), len(notes))
        if not hits and not sure and knowledge.is_question(q):
            extra += "\n\n（浮屿资料里没找到这个问题的答案。不确定就直说，告诉对方已经转给主人了，不要编）"
            self._handoff(chat, payload, q)
        payload["text"] += extra

    def _assist_proactive(self, chat: str, payload: dict, q: str) -> None:
        if len(q) < 2 or self._talking_to_other(payload):
            return
        fuyu_mentioned = bool(FUYU_TOPIC_RE.search(q))
        has_issue = bool(ISSUE_NUM_RE.search(q))
        asked = knowledge.is_question(q) or has_issue
        try:
            hits = self.knowledge.search(q)
        except Exception as e:
            log.warning("资料检索出错：%s", e)
            hits = []
        notes, sure = self._fuyu_notes(q, strict=not (fuyu_mentioned or bool(hits)))

        # ① 浮屿提问且资料或仓库真能答上：高概率主动帮答，并附上资料
        if asked:
            qa_pct = self.cfg.get("fuyu_qa_pct")
            if qa_pct <= 0 or (not hits and not sure):
                return
            if hits:
                hits = self._judge(q, hits)
            if not hits and not sure:
                return
            if random.random() * 100 >= qa_pct:
                return
            extra = ""
            if hits:
                extra += ("\n\n（以下是浮屿的说明资料，回答浮屿用法问题时以它为准；资料里没写的不要编）\n"
                          + knowledge.render(hits))
            if notes:
                extra += "\n\n（浮屿仓库的最新情况，回答版本、修没修时参考；对不上就别提）\n" + "\n".join(notes)
            extra += "\n\n（群里没直接 @ 你，但这个问题资料里有答案；自然顺手帮答一句即可，别用客服腔）"
            payload["text"] += extra
            payload["at_me"] = True
            self._mark_fuyu_proactive(chat)
            log.info("产品助手 %s：主动答疑（未 @，概率 %d%%，命中 %d 段资料/%d 条仓库）",
                     self.name_of(chat), qa_pct, len(hits), len(notes))
            return

        # ② 普通聊浮屿（非提问）：温和概率 + 冷却
        if not (fuyu_mentioned or hits):
            return
        chat_pct = self.cfg.get("fuyu_chat_pct")
        cooldown = self.cfg.get("fuyu_chat_cooldown")
        if chat_pct <= 0 or not self._fuyu_cooldown_ok(chat, cooldown):
            return
        if random.random() * 100 >= chat_pct:
            return
        extra = ""
        if hits:
            extra += "\n\n（相关浮屿资料，仅作事实参考，不要照本宣科）\n" + knowledge.render(hits[:2])
        extra += "\n\n（群里在聊浮屿，没直接 @ 你；像熟人一样自然搭一两句短话即可，不要用客服腔，没看到的内容不要编）"
        payload["text"] += extra
        payload["at_me"] = True
        self._mark_fuyu_proactive(chat)
        log.info("产品助手 %s：浮屿话题主动插话（未 @，概率 %d%%）", self.name_of(chat), chat_pct)

    def _judge(self, q: str, hits: list) -> list:
        """关键词检索只能找到「相关」的段落，让模型挑出真能回答这个问题的；都不能就返回空（转人工）。
        模型出错或超时就原样返回，宁可多给资料也不挡回复。"""
        listing = "\n\n".join(f"[{i}] {c.title}\n{c.text[:600]}" for i, c in enumerate(hits, 1))
        system = ("你在帮浮屿（一款桌面应用）的用户群答疑做资料筛选。给你用户的问题和几段编号的说明资料，"
                  "找出能直接回答这个问题的资料编号。只输出编号，用英文逗号分隔；没有任何一段能回答就只输出 0。")
        pool = cf.ThreadPoolExecutor(max_workers=1)
        try:
            out = pool.submit(self.kernel.complete, system, f"问题：{q}\n\n资料：\n{listing}", 20).result(timeout=20)
        except Exception as e:
            log.warning("资料筛选出错，原样附上：%s", e)
            return hits
        finally:
            pool.shutdown(wait=False)
        picked = [int(n) for n in re.findall(r"\d+", out) if 1 <= int(n) <= len(hits)]
        log.info("产品助手资料筛选：%s → %s", out[:20], picked or "都不能回答")
        return [hits[i - 1] for i in dict.fromkeys(picked)]

    def _fuyu_notes(self, q: str, strict: bool = False) -> tuple[list[str], bool]:
        """版本 / #编号 / 像问题反馈时，从 F2 的仓库缓存里找参考。返回 (几行说明, 是否把握较大可不转人工)。
        strict=True 用于未 @ 且未明提浮屿/未命中文档时：不把泛泛的「更新/最新」当浮屿版本提问。"""
        notes, sure = [], False
        try:
            bug = bool(BUG_Q.search(q))   # 「更新后打不开了」是故障反馈，不算问版本
            if not strict and VERSION_Q.search(q):
                notes.append(self.fuyu.version_brief())
                sure = bool(self.fuyu.latest()) and not bug
            numbers = list(dict.fromkeys(int(x) for x in ISSUE_NUM_RE.findall(q)))
            for n in numbers:
                notes.append(self.fuyu.status(n))
                sure = True
            if bug and not numbers:
                for score, it in self.fuyu.find(q, k=2):
                    if score >= 0.45:
                        notes.append(self.fuyu.status(it["number"]))
                        sure = sure or score >= 0.55
        except Exception as e:
            log.warning("浮屿仓库信息出错：%s", e)
        return notes, sure

    def tell_owner(self, text: str) -> None:
        """私聊主人（发到文件传输助手），不等结果。"""
        def go():
            job = self.send(SendJob(chat="filehelper", text=text, source="control"))
            if not job.ok:
                log.warning("给主人的通知没发出去：%s", job.error)
        threading.Thread(target=go, name="tell-owner", daemon=True).start()

    def _handoff(self, chat: str, payload: dict, q: str) -> None:
        now = time.time()
        recent = [t for t, c in self.handoffs if now - t < 3600]
        if len(recent) >= 10 or any(c == chat and now - t < 180 for t, c in self.handoffs):
            log.info("产品助手 %s：转人工太频繁，这次不私聊主人", self.name_of(chat))
            return
        self.handoffs.append((now, chat))
        self.events.publish("handoff", {"chat": chat, "chat_name": self.name_of(chat),
                                        "sender": payload.get("senderName") or "", "question": q[:300]})
        text = (f"【浮屿群求助】{payload.get('senderName') or '有人'} 在「{self.name_of(chat)}」问：\n{q[:300]}\n"
                "资料里没找到相关内容，可能需要你来回。")

        def go():
            job = self.send(SendJob(chat="filehelper", text=text, source="control"))
            if not job.ok:
                log.warning("转人工通知没发出去：%s", job.error)
        threading.Thread(target=go, name="handoff", daemon=True).start()
        log.info("产品助手 %s：资料里没找到，已转主人", self.name_of(chat))
