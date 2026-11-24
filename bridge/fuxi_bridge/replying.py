"""回复的后半段：图片/语音先看懂听懂、把内核的回复逐个气泡发出去（真 @、打字节奏、撤回检查）、表情包。"""

from __future__ import annotations

import base64
import logging
import os
import random
import time

from . import timing
from .kernel import KernelError
from .messages import Message
from .sending import SendJob

log = logging.getLogger("fuxi")


class ReplyMixin:
    def _understand(self, chat: str, batch: list[Message], tr: "timing.Trace") -> None:
        """图片、语音交给内核前先看懂/听懂：「[图片]（图片内容：…）」「[语音 5秒]（语音内容：…）」。
        只处理最后发言人的（_payload 只带这些），每种最多最后三条。任何失败都保留原占位，不影响回复。"""
        group = chat.endswith("@chatroom")
        last = batch[-1].sender
        t0 = time.monotonic()
        did = False
        for kind, mode, label in (("image", self.vision_mode, "图片内容"), ("voice", self.voice_mode, "语音内容")):
            if mode == "off" or (mode == "private" and group):
                continue
            todo = [m for m in batch if m.type == kind and m.media and m.sender == last]
            for m in todo[-3:]:
                key = f"{kind}:{chat}:{m.media}"
                got = self.media_cache.get(key)
                if got is None:
                    did = True
                    try:
                        got = self._recognize(chat, kind, m.media)
                    except (ValueError, KernelError) as e:
                        log.info("%s失败 %s：%s", "看图" if kind == "image" else "语音识别", self.name_of(chat), e)
                        continue
                    if got:
                        self.media_cache[key] = got
                        while len(self.media_cache) > 300:
                            self.media_cache.popitem(last=False)
                    else:
                        log.warning("%s返回空结果 %s（media=%s）", "看图" if kind == "image" else "语音识别",
                                    self.name_of(chat), str(m.media)[:8])
                if got:
                    m.text = f"{m.text}（{label}：{got}）"
                    if self.is_feedback_group(chat):
                        self.feedback.annotate(m.msg_id, self._render(m))
        if did:
            tr.add("vision", time.monotonic() - t0)

    def _recognize(self, chat: str, kind: str, media: str) -> str:
        if kind == "image":
            jpeg, src = self.images.load(chat, media)
            text = self.kernel.vision(base64.b64encode(jpeg).decode())
            log.info("看图 %s：%s（%d 字）", self.name_of(chat), src, len(text))
            return text
        wav = self.voices.load(chat, int(media))
        text = self.kernel.asr(base64.b64encode(wav).decode())
        log.info("语音识别 %s：%.1f 秒 → %d 字", self.name_of(chat), (len(wav) - 44) / 32000, len(text))
        return text

    def _deliver(self, chat: str, batch: list[Message], resp: dict, tr: "timing.Trace", k0: float) -> bool:
        """发出内核的文字回复；全部气泡都发成功才返回 True（随后才发表情包）。"""
        if resp.get("action") != "reply":
            log.info("内核不回复：%s", resp.get("reason") or resp.get("skipped") or resp.get("action"))
            tr.reason = f"内核决定不回：{resp.get('reason') or resp.get('skipped') or resp.get('action')}"
            return False
        bubbles = [b for b in (resp.get("bubbles") or [resp.get("reply") or ""]) if b and b.strip()]
        allowed, reason = self.kernel.send_permission(chat, self.name_of(chat))
        if not allowed:
            log.info("内核不允许在 %s 发言：%s", chat, reason)
            tr.reason = f"不允许发言：{reason}"
            return False
        tr.add("kernel", time.monotonic() - k0)
        mention = resp.get("mention") or ""
        for i, b in enumerate(bubbles):
            if self.paused:
                tr.reason = "发送途中被暂停"
                return False
            if not self.rate_ok(chat):
                tr.reason = "超过发送频率上限"
                log.warning("超过发送频率上限，丢弃 %s 的剩余 %d 条回复", self.name_of(chat), len(bubbles) - i)
                return False
            at, text = "", b
            if i == 0 and mention and chat.endswith("@chatroom"):
                at = self._mention_name(chat, batch, mention)
                head = text.lstrip()
                if head.startswith("@" + mention):   # 模型自己写了「@名字」开头：去掉，换成真 @
                    text = head[len(mention) + 1:].lstrip(" \u2005：:，,") or text
            delay = self.first_delay(text, tr.s["kernel"]) if i == 0 else self.typing_delay(text)
            time.sleep(delay)
            tr.add("typing", delay)
            if i == 0 and self._revoked(batch):
                log.info("%s 在生成回复期间撤回了消息，放弃这次回复", self.name_of(chat))
                tr.reason = "对方撤回了"
                return False
            self.sent_times.append((time.time(), chat))
            job = self.send(SendJob(chat=chat, text=text, at=at))
            if not job.ok:
                tr.reason = f"发送失败：{job.error}"
                return False
            tr.replies.append(text)
            if job.t_sent:
                tr.add("queue", job.t_start - job.t_queued)
                tr.add("open", job.open_s)
                tr.add("paste", job.t_sent - job.t_start - job.open_s)
                tr.add("verify", job.t_done - job.t_sent)
                tr.bubble_sent(time.time() - (time.monotonic() - job.t_sent))
        return True

    def _mention_name(self, chat: str, batch: list[Message], mention: str) -> str:
        """内核给的 @ 对象（名字）→ 该成员在群里的显示名（@ 弹窗按它过滤）。
        认不出是这批消息里的谁，就原样用内核给的名字（多半会退回文字 @）。"""
        for m in reversed(batch):
            if not m.sender or m.sender == self.self_wxid:
                continue
            try:
                shown = self.reader.member_name(chat, m.sender)
            except Exception:
                log.debug("读群昵称失败", exc_info=True)
                shown = ""
            names = [n for n in (shown, self.name_of(m.sender)) if n]
            # 内核取 @ 名字时遇空格截断（「tixxin - 建议…」→「tixxin」），所以前缀也算
            if mention == m.sender or any(n == mention or n.startswith(mention) for n in names):
                return shown or mention
        return mention

    def _sticker_file(self, path: str) -> str:
        """内核容器里的路径（/app/data/...）→ 桥接这边只读挂载的同一个文件。"""
        for prefix in ("/app/data/", "data/"):
            if path.startswith(prefix):
                return os.path.join(self.kernel_data, path[len(prefix):])
        return path

    def _send_sticker(self, chat: str, item: dict, delivered: bool) -> None:
        """文字发完后发内核预留的表情包：领取 → 发送 → 回执。文字没发成就取消预留。"""
        sid = item.get("id") or ""
        try:
            if not delivered or self.paused or self.halted or not self.rate_ok(chat):
                self.kernel.sticker_cancel(item)
                log.info("取消表情包任务（文字回复没发完或已暂停）")
                return
            got = self.kernel.sticker_claim(sid)
            path = self._sticker_file(got.get("path") or item.get("path") or "")
            if not os.path.isfile(path):
                self.kernel.sticker_ack(sid, "failed", "表情包文件桥接读不到（内核 data 卷没挂载？）")
                log.error("表情包文件读不到：%s", path)
                return
            time.sleep(random.uniform(0.8, 2.0))
            self.sent_times.append((time.time(), chat))
            job = self.send(SendJob(chat=chat, sticker=path))
            status = "failed" if not job.ok else ("confirmed" if job.verified else "unconfirmed")
            self.kernel.sticker_ack(sid, status, job.error)
        except KernelError as e:
            log.warning("表情包任务处理失败：%s", e)

    def _revoked(self, batch: list[Message]) -> set[str]:
        try:
            return self.reader.revoked(batch)
        except Exception:
            log.exception("检查撤回失败")
            return set()
