"""产品助手：产品助手群里被 @ / 被回复时附上浮屿资料和仓库信息，答不上转人工；有人问源码时直接回仓库地址。"""

from __future__ import annotations

import concurrent.futures as cf
import logging
import re
import threading
import time

from . import knowledge
from .sending import SendJob

log = logging.getLogger("fuxi")

VERSION_Q = re.compile(r"版本|新版|更新|升级|发版|最新|release|v\d+\.\d+", re.I)
BUG_Q = re.compile(r"修(了|好|复)|bug|问题|没反应|不行|不了|不能|看不到|打不开|闪退|崩|卡|报错|失败|异常|不显示|没效果", re.I)
SOURCE_Q = re.compile(r"源码|源代码|开源|source ?code|代码在哪", re.I)


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

    def _assist(self, chat: str, payload: dict) -> None:
        """产品助手群里被 @ / 被回复时：从浮屿资料里找相关段落附在消息后面给内核参考；
        找不到又像是提问时，提示内核如实说不确定，并私聊主人。"""
        if not self.is_assistant_group(chat) or not (payload.get("at_me") or payload.get("reply_to_bot")):
            return
        q = payload["text"]
        for n in payload.get("self_names") or []:
            q = q.replace(f"@{n}", " ")
        q = q.strip()
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

    def _fuyu_notes(self, q: str) -> tuple[list[str], bool]:
        """版本 / #编号 / 像问题反馈时，从 F2 的仓库缓存里找参考。返回 (几行说明, 是否把握较大可不转人工)。"""
        notes, sure = [], False
        try:
            bug = bool(BUG_Q.search(q))   # 「更新后打不开了」是故障反馈，不算问版本
            if VERSION_Q.search(q):
                notes.append(self.fuyu.version_brief())
                sure = bool(self.fuyu.latest()) and not bug
            numbers = list(dict.fromkeys(int(x) for x in re.findall(r"#(\d{1,6})", q)))
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
