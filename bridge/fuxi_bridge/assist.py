"""产品助手：产品助手群里被 @ / 被回复时附上浮屿资料和仓库信息，答不上转人工；
未 @ 时按浮屿相关性分层主动接话（能答上的提问高概率、普通聊浮屿低频带冷却）；有人问源码时直接回仓库地址。"""

from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import os
import random
import re
import threading
import time

from . import knowledge
from .sending import SendJob

log = logging.getLogger("fuxi")

VERSION_Q = re.compile(r"版本|新版|更新|升级|发版|最新|release|v\d+\.\d+", re.I)
BUG_Q = re.compile(r"修(了|好|复)|bug|问题|没反应|不行|不了|不能|看不到|打不开|闪退|崩|卡|报错|失败|异常|不显示|没效果", re.I)
EXPLAIN_Q = re.compile(r"解释|说明|干啥|什么意思|原因|弹窗|权限|钥匙串|keychain|报错|异常|凭据|credential|源码", re.I)
SOURCE_Q = re.compile(r"源码|源代码|开源|source ?code|代码在哪", re.I)
# 未 @ 时判定「明显在聊浮屿」的词；纯通用词（如「更新」「截图」）不放这里，避免把日常聊天误当成浮屿话题
FUYU_TOPIC_RE = re.compile(
    r"浮屿|fuyu|灵动岛|上岛|岛上|岛屿实验室|贴图|文件架|暂存架|cc-?connect|浮汐|fuxi|\bv0\.\d+",
    re.I,
)
# 群友在用户群里报 bug / 反馈视觉与交互问题 / 提建议时的典型表达（即便没写出「浮屿」二字也能认出）
FEEDBACK_RE = re.compile(
    r"小问题|新问题|有问题|bug|报错|闪退|崩溃|卡死|卡顿|打不开|用不了|没反应|不显示|看不到|没效果|不生效|异常|失灵|"
    r"过期|失效|无效|不见了|消失|出不来|莫名其妙|乱码|"
    r"错位|重叠|阴影|压暗|闪烁|遮挡|穿透|不和谐|任务栏.{0,12}(问题|阴影|压暗|挡|偏|歪|显示)|"
    r"建议|希望(能|可以)?|能不能加|可以加个|想要个|提个建议|反馈|提个?需求",
    re.I,
)
FEEDBACK_ACK_COOLDOWN = 120
FOLLOWUP_WINDOW = 180          # 浮汐刚回复过某群友后，3 分钟内同人补充细节/截图自动接住（绕过内核 30 字截断）
FOLLOWUP_MAX_TURNS = 2         # 单段对话最多自动续接 2 轮，防无限拉扯
CLOSING_ACK_RE = re.compile(
    r"^(?:好[的滴呀啊啦]?|行[吧的呀]?|嗯+[嗯呀啊]?|哦+[哦呀]?|ok|okay|收到|明白|了解|知道了|"
    r"谢+谢[啦呀啊]?|多谢|感谢[啦呀]?|辛苦[了啦]?|太好了|赞|棒|👍|🙏|👌)+[。！!~～]*$",
    re.I,
)
ISSUE_NUM_RE = re.compile(r"#(\d{1,6})")
# 内核 pipeline/chat.js normalize() 会对 msg.text 做 2000 字硬截断，桥接拼资料时必须留出余量
PAYLOAD_TEXT_LIMIT = 1920
# 群里赶她闭嘴/退场的常见表达（与内核 canned.js 保持一致，用来在桥接侧同步静默期，防止未 @ 主动触发误破静默）
DISMISS_DIRECT_RE = re.compile(
    r"闭嘴|滚蛋|滚开|滚出去|你滚|^滚[吧啊呀了嘛哦]*$|走开|退下|一边去|别说了|别吵|别接话|别插嘴|没你事|不用你了|安静点?$"
)
DISMISS_THIRD_RE = re.compile(
    r"(让|叫|把|喊|请)(?:她|它|这货|机器人|浮汐|fuxi)(?:赶紧|快点|马上)?(?:滚|闭嘴|安静|别说话|少说话|别接话|别插嘴|别吵|走开|退下)",
    re.I,
)
SILENCE_SEC = 600


def _fit_append(base: str, hits_header: str, hits_body: str, tail: str, limit: int = PAYLOAD_TEXT_LIMIT) -> str:
    """把资料段和尾注拼到 base 后面，总长严格不超过 limit（保住原话与尾注，只裁超长的资料正文）。"""
    if not hits_body:
        out = base + tail
        return out[:limit]
    budget = limit - len(base) - len(hits_header) - len(tail)
    if budget <= 40:
        return (base + tail)[:limit]
    body = hits_body if len(hits_body) <= budget else (hits_body[:budget - 1].rstrip() + "…")
    return base + hits_header + body + tail


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

    def _is_group_silenced(self, chat: str) -> bool:
        table = getattr(self, "_group_silenced_until", None) or {}
        return time.monotonic() < table.get(chat, 0.0)

    def _update_group_silence(self, chat: str, payload: dict, q: str) -> None:
        """同步群里「闭嘴/滚」触发的静默期，防止未 @ 的主动插话把静默意外打破。"""
        table = getattr(self, "_group_silenced_until", None)
        if table is None:
            table = {}
            self._group_silenced_until = table
        addressed = bool(payload.get("at_me") or payload.get("reply_to_bot"))
        clean = q.strip(" ，,。.!！~～、…")
        dismissed = (addressed and bool(DISMISS_DIRECT_RE.search(clean))) or bool(DISMISS_THIRD_RE.search(clean))
        if dismissed:
            table[chat] = time.monotonic() + SILENCE_SEC
            log.info("产品助手 %s：检测到退场指令，%d 秒内暂停主动插话", self.name_of(chat), SILENCE_SEC)
        elif addressed:
            masters = getattr(self, "masters", None) or []
            if payload.get("sender_id") in masters or payload.get("user_id") in masters:
                table.pop(chat, None)

    def _wait_qa_yield(self, chat: str, seq_before: int | None, delay: float) -> bool:
        """主动答疑前先观察 delay 秒；期间若群里已有新消息（别人/主人接了话）或被暂停，返回 True 表示让贤。"""
        if delay <= 0 or seq_before is None:
            return False
        seq_map = getattr(self, "_chat_msg_seq", None)
        if seq_map is None:
            return False
        if seq_map.get(chat, 0) > seq_before:
            return True
        deadline = time.monotonic() + delay
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return seq_map.get(chat, 0) > seq_before or bool(getattr(self, "paused", False))
            time.sleep(min(0.5, left))
            if seq_map.get(chat, 0) > seq_before or getattr(self, "paused", False):
                return True

    def _assist(self, chat: str, payload: dict) -> None:
        """产品助手处理：
        1) 私聊：聊到浮屿 / 仓库 / #issue 时顺手附资料和仓库动态（不转人工），让她在私聊也了解项目；
        2) 产品助手群被 @ / 被回复：检索浮屿资料和仓库信息附给内核，答不上且像提问时转主人；
        3) 产品助手群未被 @：不打断他人对话、不在静默期内，
           - ① 浮屿提问且资料/仓库真能答上 → 观察避让秒数后，按 fuyu_qa_pct 主动帮答并附资料；
           - ② 群友报 bug / 提建议（即使没写「浮屿」或只 @ 了主人）→ 等主人避让后按 fuyu_feedback_pct 兜底接住并确认记下；
           - ③ 普通聊浮屿（非提问）→ 按 fuyu_chat_pct + fuyu_chat_cooldown 偶尔插一句；
           - ④ 与浮屿无关的闲聊 → 不介入，交回内核按普通插话概率处理。
        """
        seq_before = payload.pop("_chat_seq", None)
        boot_catchup = bool(payload.pop("_boot_catchup", False))
        msg_ts = payload.pop("_msg_ts", None)
        followup_img = bool(payload.pop("_followup_img", False))
        q = payload["text"]
        for n in payload.get("self_names") or []:
            q = q.replace(f"@{n}", " ")
        q = q.strip()
        if payload.get("chat_type") == "private":
            self._assist_private(chat, payload, q)
            return
        if not self.is_assistant_group(chat):
            return
        self._update_group_silence(chat, payload, q)
        if payload.get("at_me") or payload.get("reply_to_bot"):
            self._assist_addressed(chat, payload, q)
        else:
            self._assist_proactive(chat, payload, q, seq_before, boot_catchup=boot_catchup,
                                   msg_ts=msg_ts, followup_img=followup_img)

    def _assist_private(self, chat: str, payload: dict, q: str) -> None:
        """私聊聊到浮屿 / 仓库 / #编号 时顺手附上仓库动态和相关资料（不触发转人工）。"""
        if len(q) < 2:
            return
        if not (FUYU_TOPIC_RE.search(q) or ISSUE_NUM_RE.search(q)):
            return
        hits = []
        if hasattr(self, "knowledge"):
            try:
                hits = self.knowledge.search(q, k=2, budget=800)
            except Exception as e:
                log.warning("私聊资料检索出错：%s", e)
        notes, _ = self._fuyu_notes(q)
        if not notes and re.search(r"浮屿|fuyu", q, re.I) and hasattr(self, "fuyu"):
            try:
                if self.fuyu.latest():
                    notes.append(self.fuyu.version_brief())
            except Exception:
                pass
        hits_header = "\n\n（以下是浮屿相关说明资料，仅作参考）\n" if hits else ""
        hits_body = knowledge.render(hits) if hits else ""
        tail = ("\n\n（浮屿仓库最新情况，仅作事实参考）\n" + "\n".join(notes)) if notes else ""
        if hits_body or tail:
            payload["text"] = _fit_append(payload["text"], hits_header, hits_body, tail)
            log.info("私聊 %s：附上浮屿参考（%d 段资料/%d 条仓库）", self.name_of(chat), len(hits), len(notes))

    def _assist_addressed(self, chat: str, payload: dict, q: str) -> None:
        try:
            hits = self.knowledge.search(q)
        except Exception as e:   # 检索出错不能挡住回复
            log.warning("资料检索出错：%s", e)
            hits = []
        if hits and (knowledge.is_question(q) or EXPLAIN_Q.search(q)):
            hits = self._judge(q, hits)
        notes, sure = self._fuyu_notes(q)
        hits_header, hits_body, tail = "", "", ""
        if hits:
            hits_header = ("\n\n（以下是浮屿的说明资料，回答问题时以它为准；如果资料主题和对方问的弹窗、报错或具体代码对不上，"
                           "严禁硬套或编造，直接说不确定、需要查源码或已经转给主人了）\n")
            hits_body = knowledge.render(hits)
            log.info("产品助手 %s：附上 %d 段资料（%s）", self.name_of(chat), len(hits), "、".join(h.title[:20] for h in hits))
        if notes:
            tail += "\n\n（浮屿仓库的最新情况，回答版本、修没修时参考；开发记录未必就是对方说的问题，对不上就别提）\n" + "\n".join(notes)
            log.info("产品助手 %s：附上仓库信息 %d 条", self.name_of(chat), len(notes))
        if not hits and not sure and (knowledge.is_question(q) or EXPLAIN_Q.search(q)):
            tail += "\n\n（浮屿资料里没找到这个问题的答案。不确定就直说不知道或需要核实源码，严禁猜测编造原因，告诉对方已经转给主人了）"
            self._handoff(chat, payload, q)
        if hits_body or tail:
            payload["text"] = _fit_append(payload["text"], hits_header, hits_body, tail)

    def _fb_cooldown_ok(self, chat: str) -> bool:
        table = getattr(self, "_fuyu_fb_at", None) or {}
        last = table.get(chat, 0.0)
        return not last or (time.monotonic() - last) >= FEEDBACK_ACK_COOLDOWN

    def _mark_fb_ack(self, chat: str) -> None:
        table = getattr(self, "_fuyu_fb_at", None)
        if table is None:
            table = {}
            self._fuyu_fb_at = table
        table[chat] = time.monotonic()

    def _note_group_reply(self, chat: str, payload: dict, resp: dict, was_followup: bool = False) -> None:
        """记录产品助手群里浮汐刚回复过的非主人群友，用于后续 3 分钟内同人补充说明/截图时自动续接。"""
        if not self.is_assistant_group(chat):
            return
        sender = payload.get("sender_id") or payload.get("user_id") or ""
        masters = set(getattr(self, "masters", None) or [])
        if not sender or sender in masters:
            return
        table = getattr(self, "_group_followup", None)
        if table is None:
            table = {}
            self._group_followup = table
        now = time.monotonic()
        # 顺手清理过期的旧记录
        for k in [k for k, v in table.items() if now - v.get("at", 0) > FOLLOWUP_WINDOW]:
            table.pop(k, None)
        prev = table.get((chat, sender)) or {}
        turns = int(prev.get("turns", 0)) + 1 if was_followup else 0
        reply_text = str((resp or {}).get("reply") or "")
        table[(chat, sender)] = {
            "at": now,
            "turns": turns,
            "had_question": bool(re.search(r"[?？]", reply_text)),
        }

    def _has_recent_followup(self, chat: str, payload: dict) -> bool:
        """当前消息是否来自刚和浮汐聊过（3 分钟内且未超轮次）的同一非主人群友。"""
        if not self.is_assistant_group(chat) or self._is_group_silenced(chat):
            return False
        if self._talking_to_other(payload):
            return False
        sender = payload.get("sender_id") or payload.get("user_id") or ""
        masters = set(getattr(self, "masters", None) or [])
        if not sender or sender in masters:
            return False
        table = getattr(self, "_group_followup", None) or {}
        entry = table.get((chat, sender))
        if not entry:
            return False
        if time.monotonic() - float(entry.get("at") or 0) > FOLLOWUP_WINDOW:
            table.pop((chat, sender), None)
            return False
        return int(entry.get("turns", 0)) < FOLLOWUP_MAX_TURNS

    def _try_followup(self, chat: str, payload: dict, q: str, hits: list, notes: list[str],
                      followup_img: bool = False) -> bool:
        """浮汐刚回复过该群友后，对方在 3 分钟内不 @ 补充细节/回答提问/发截图，直接接住避免被内核 30 字上限判为旁人闲聊。"""
        if not self._has_recent_followup(chat, payload):
            return False
        sender = payload.get("sender_id") or payload.get("user_id") or ""
        table = getattr(self, "_group_followup", None) or {}
        entry = table.get((chat, sender)) or {}
        clean_q = q.strip()
        if not followup_img and CLOSING_ACK_RE.match(clean_q) and not entry.get("had_question"):
            table.pop((chat, sender), None)
            return False
        hits_header = "\n\n（相关浮屿说明资料，仅作参考）\n" if hits else ""
        hits_body = knowledge.render(hits[:2]) if hits else ""
        tail = ""
        if notes:
            tail += "\n\n（浮屿仓库的相关记录，对不上就别提）\n" + "\n".join(notes)
        if followup_img:
            tail += "\n\n（对方正在补充刚才反馈的截图，没再 @ 你；简短自然地确认截图也收到了、一起记下转给开发者即可，不要继续追问）"
        else:
            tail += "\n\n（对方正在接着刚才的对话补充细节或回答你的问题，没再 @ 你；结合上文简短自然地确认已补充记下即可，不要继续追问系统版本或步骤，别用客服腔）"
        payload["text"] = _fit_append(payload["text"], hits_header, hits_body, tail)
        payload["at_me"] = True
        payload["_followup_turn"] = True
        log.info("产品助手 %s：同人对话续接（%s，第 %d/%d 轮%s）",
                 self.name_of(chat), payload.get("senderName") or sender,
                 int(entry.get("turns", 0)) + 1, FOLLOWUP_MAX_TURNS,
                 "，纯截图" if followup_img else "")
        return True

    def _try_feedback_fallback(self, chat: str, payload: dict, q: str, notes: list[str], seq_before: int | None,
                               owner_at: bool, boot_catchup: bool = False, msg_ts: float | None = None) -> bool:
        """群友（非主人）报 bug/提建议、或直接 @ 主人说正事且主人没立刻回复时，主动接住并确认记下。"""
        if owner_at:
            # 去掉微信 @名字\u2005（或裸 @名字）前缀，光秃秃只 @ 一下不接，有实质内容（≥4字）不卡死在关键词上
            body = re.sub(r"@[^\u2005\n]+\u2005", "", q)
            for mn in payload.get("mentioned_names") or []:
                if mn:
                    body = body.replace(f"@{mn}", "")
            body = re.sub(r"^@\S+(?:\s*-\s*\S+)?\s*", "", body.strip())
            body = re.sub(r"@[^\s@，,。！？!?]+\s*", "", body).strip()
            if len(body) < 4:
                return False
        elif len(q) < 6 or not FEEDBACK_RE.search(q):
            return False
        masters = set(getattr(self, "masters", None) or [])
        sender = payload.get("sender_id") or payload.get("user_id") or ""
        if sender and sender in masters:
            return False
        fb_pct = self.cfg.get("fuyu_feedback_pct")
        if fb_pct <= 0 or not self._fb_cooldown_ok(chat):
            return False
        if not boot_catchup and random.random() * 100 >= fb_pct:
            return False
        base_delay = self.cfg.get("fuyu_qa_delay") or 8
        wait_s = 10 if owner_at else max(10, base_delay)
        elapsed = max(0.0, time.time() - float(msg_ts)) if msg_ts else 0.0
        remain = max(0.0, wait_s - elapsed)
        if self._wait_qa_yield(chat, seq_before, remain):
            log.info("产品助手 %s：反馈兜底避让（%d 秒内群里已有新消息）", self.name_of(chat), wait_s)
            return True   # 已处理（让贤），不要再往下掉进闲聊分支
        tail = ""
        if notes:
            tail += "\n\n（浮屿仓库的相关记录，对不上就别提）\n" + "\n".join(notes)
        tail += ("\n\n（群友在反馈浮屿的问题或建议，没直接 @ 你，开发者暂时没回复；"
                 "请自然接住、简短确认已经记下转给开发者即可；不要动不动追问对方的系统版本、浮屿版本号或复现步骤，别用客服腔）")
        payload["text"] = _fit_append(payload["text"], "", "", tail)
        payload["at_me"] = True
        self._mark_fb_ack(chat)
        self._mark_fuyu_proactive(chat)
        self._notify_feedback(chat, payload, q)
        log.info("产品助手 %s：反馈兜底接话（未 @%s%s，概率 %d%%）",
                 self.name_of(chat), "、原 @ 主人" if owner_at else "",
                 "、重启补回" if boot_catchup else "", fb_pct)
        return True

    def _assist_proactive(self, chat: str, payload: dict, q: str, seq_before: int | None = None,
                          boot_catchup: bool = False, msg_ts: float | None = None,
                          followup_img: bool = False) -> None:
        if len(q) < 2 or self._is_group_silenced(chat):
            return
        reply_to_other = bool(payload.get("reply_to_user_id") and not payload.get("reply_to_bot"))
        if reply_to_other:
            return
        talking_other = self._talking_to_other(payload)
        owner_at = bool(payload.get("at_owner_only"))
        # @ 别的普通群友：绝不打断；只 @ 了主人（群友报 bug 常 @ 作者）：仅走更长避让后的反馈兜底
        if talking_other:
            if owner_at:
                notes, _ = self._fuyu_notes(q)
                self._try_feedback_fallback(chat, payload, q, notes, seq_before, owner_at=True,
                                            boot_catchup=boot_catchup, msg_ts=msg_ts)
            return

        if followup_img:
            self._try_followup(chat, payload, q, [], [], followup_img=True)
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

        # ① 浮屿提问且资料或仓库真能答上：高概率主动帮答，并附上资料（先等几秒看群里是否已有别人接话）
        if asked:
            qa_pct = self.cfg.get("fuyu_qa_pct")
            if qa_pct > 0 and (hits or sure):
                if hits:
                    hits = self._judge(q, hits)
                if hits or sure:
                    if boot_catchup or random.random() * 100 < qa_pct:
                        delay = self.cfg.get("fuyu_qa_delay")
                        elapsed = max(0.0, time.time() - float(msg_ts)) if msg_ts else 0.0
                        remain = max(0.0, delay - elapsed)
                        if self._wait_qa_yield(chat, seq_before, remain):
                            log.info("产品助手 %s：主动答疑避让（%d 秒内群里已有新消息）", self.name_of(chat), delay)
                            return
                        hits_header, hits_body, tail = "", "", ""
                        if hits:
                            hits_header = "\n\n（以下是浮屿的说明资料，回答浮屿用法问题时以它为准；资料里没写的不要编）\n"
                            hits_body = knowledge.render(hits)
                        if notes:
                            tail += "\n\n（浮屿仓库的最新情况，回答版本、修没修时参考；对不上就别提）\n" + "\n".join(notes)
                        tail += "\n\n（群里没直接 @ 你，但这个问题资料里有答案；自然顺手帮答一句即可，别用客服腔）"
                        payload["text"] = _fit_append(payload["text"], hits_header, hits_body, tail)
                        payload["at_me"] = True
                        self._mark_fuyu_proactive(chat)
                        log.info("产品助手 %s：主动答疑（未 @%s，概率 %d%%，命中 %d 段资料/%d 条仓库）",
                                 self.name_of(chat), "、重启补回" if boot_catchup else "", qa_pct, len(hits), len(notes))
                    return
            # 没命中资料 → 优先检查是否刚对话过的同人续接，再走 ② 反馈兜底
            if self._try_followup(chat, payload, q, hits, notes):
                return
            if self._try_feedback_fallback(chat, payload, q, notes, seq_before, owner_at=False,
                                           boot_catchup=boot_catchup, msg_ts=msg_ts):
                return
            return

        # ①.5 刚和此人聊过（3 分钟内）：对方接着补充细节/回答提问，不被内核 30 字限制判为旁人闲聊
        if self._try_followup(chat, payload, q, hits, notes):
            return

        # ② 群友陈述式报 bug / 提建议（如「目前还有一个小问题…」「移动到任务栏还是有阴影」）
        if self._try_feedback_fallback(chat, payload, q, notes, seq_before, owner_at=False,
                                       boot_catchup=boot_catchup, msg_ts=msg_ts):
            return

        # ③ 普通聊浮屿（非提问、非反馈）：必须真的提到浮屿相关词，且不主动对看不清的截图硬插话
        if not fuyu_mentioned or ("[图片]" in payload.get("text", "") and "（图片内容：" not in payload.get("text", "")):
            return
        chat_pct = self.cfg.get("fuyu_chat_pct")
        cooldown = self.cfg.get("fuyu_chat_cooldown")
        if chat_pct <= 0 or not self._fuyu_cooldown_ok(chat, cooldown):
            return
        if random.random() * 100 >= chat_pct:
            return
        hits_header = "\n\n（相关浮屿资料，仅作事实参考，不要照本宣科）\n" if hits else ""
        hits_body = knowledge.render(hits[:2]) if hits else ""
        tail = "\n\n（群里在聊浮屿，没直接 @ 你；像熟人一样自然搭一两句短话即可，不要用客服腔，没看到的内容不要编）"
        payload["text"] = _fit_append(payload["text"], hits_header, hits_body, tail)
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

    def owner_chat(self) -> str:
        """通知主人的目标会话：优先发给主人个人微信（owners[0]），未配主人时回退到 filehelper。"""
        masters = getattr(self, "masters", None) or []
        return masters[0] if masters else "filehelper"

    def tell_owner(self, text: str) -> None:
        """立刻私聊主人，不等结果（用于主人主动指令的回报或紧急提醒）。"""
        to = self.owner_chat()

        def go():
            job = self.send(SendJob(chat=to, text=text, source="control"))
            if not job.ok:
                log.warning("给主人的通知没发出去（%s）：%s", to, job.error)
        threading.Thread(target=go, name="tell-owner", daemon=True).start()

    def _owner_notify_lock(self) -> threading.Lock:
        lk = getattr(self, "_owner_notify_lk", None)
        if lk is None:
            lk = threading.Lock()
            self._owner_notify_lk = lk
        return lk

    def _load_owner_queue(self) -> list[dict]:
        path = getattr(self, "owner_notify_path", "")
        if path:
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
            except (OSError, ValueError):
                return []
        return list(getattr(self, "_owner_notify_mem", []))

    def _save_owner_queue(self, items: list[dict]) -> None:
        items = items[-30:]
        path = getattr(self, "owner_notify_path", "")
        if path:
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(items, f, ensure_ascii=False, indent=1)
                os.replace(tmp, path)
                return
            except OSError as e:
                log.warning("保存主人暂存提醒失败：%s", e)
        self._owner_notify_mem = list(items)

    def notify_owner(self, text: str, now: float | None = None) -> bool:
        """被动提醒（群反馈、转人工、外部 PR、夜间换码等）：在 owner_notify_hours 内立刻私聊；
        时段外（如凌晨 00:00-09:00）持久化暂存，到点由 flush_owner_notify 汇总发出。返回 True=已立即发。"""
        from . import outreach

        now = now or time.time()
        window = (self.cfg.get("owner_notify_hours") or "").strip() if hasattr(self, "cfg") else ""
        if not window or outreach.in_window(window, now):
            self.tell_owner(text)
            return True
        with self._owner_notify_lock():
            q = self._load_owner_queue()
            if not any(x.get("text") == text for x in q):
                q.append({"ts": now, "text": text})
                self._save_owner_queue(q)
            cnt = len(q)
        log.info("非主人通知时段（%s），暂存 1 条私聊提醒（当前待发 %d 条）", window, cnt)
        return False

    def flush_owner_notify(self, now: float | None = None) -> int:
        """进入主人通知时段后，把夜间暂存的私聊提醒汇总成一条发出。"""
        from . import outreach

        now = now or time.time()
        if getattr(self, "paused", False):
            return 0
        window = (self.cfg.get("owner_notify_hours") or "").strip() if hasattr(self, "cfg") else ""
        if window and not outreach.in_window(window, now):
            return 0
        with self._owner_notify_lock():
            q = self._load_owner_queue()
            if not q:
                return 0
            fresh = [x for x in q if now - float(x.get("ts") or 0) <= 3 * 86400 and x.get("text")]
            self._save_owner_queue([])
        if not fresh:
            return 0
        if len(fresh) == 1:
            t = time.strftime("%H:%M", time.localtime(float(fresh[0]["ts"])))
            msg = f"【夜间暂存提醒 {t}】\n{fresh[0]['text']}"
        else:
            parts = [f"【夜间暂存提醒汇总（共 {len(fresh)} 条）】"]
            for it in fresh:
                t = time.strftime("%H:%M", time.localtime(float(it["ts"])))
                parts.append(f"[{t}] {it['text']}")
            msg = "\n\n".join(parts)
        self.tell_owner(msg)
        log.info("已汇总补发 %d 条夜间暂存的主人私聊提醒", len(fresh))
        return len(fresh)

    def _notify_feedback(self, chat: str, payload: dict, q: str) -> None:
        """群反馈兜底接住后通知主人（遵循 owner_notify_hours），并在反馈群场景下补跑一次归类以带上编号与操作提示。"""
        now = time.time()
        recent = [t for t, c in self.handoffs if now - t < 3600]
        if len(recent) >= 10 or any(c == chat and now - t < 180 for t, c in self.handoffs):
            return
        self.handoffs.append((now, chat))
        sender = payload.get("senderName") or "群友"
        chat_name = self.name_of(chat)
        self.events.publish("handoff", {"chat": chat, "chat_name": chat_name,
                                        "sender": sender, "question": q[:300]})

        def go():
            hint = "回复 /反馈 整理 可立刻归类，/反馈 看列表"
            fb = getattr(self, "feedback", None)
            if fb is not None and getattr(self, "is_feedback_group", lambda _c: False)(chat):
                try:
                    fb.classify(time.time() + 31)   # 跳过 30 秒缓冲，立即把刚收到的反馈归类出编号
                except Exception as e:
                    log.debug("反馈即时归类失败：%s", e)
                items = [it for it in getattr(fb, "items", []) if chat in (it.get("chats") or [])]
                if items:
                    latest = max(items, key=lambda it: it.get("last_seen", 0))
                    if time.time() - latest.get("last_seen", 0) <= 7200:
                        iid = latest["id"]
                        hint = (f"已归类为反馈 FB-{iid}「{latest['title']}」\n"
                                f"回复 /fb {iid} 看详情，/issue {iid} 开 issue，/fix {iid} [mac|win] [fg] 派修")
            text = f"【浮屿群反馈】{sender} 在「{chat_name}」反馈：\n{q[:300]}\n\n{hint}"
            self.notify_owner(text)
        threading.Thread(target=go, name="fb-notify", daemon=True).start()

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
        sent_now = self.notify_owner(text)
        to = self.owner_chat()
        log.info("产品助手 %s：资料里没找到，已%s（%s）", self.name_of(chat),
                 "转主人" if sent_now else "暂存待主人通知时段转交",
                 self.name_of(to) if to != "filehelper" else "filehelper")
