"""浮屿用户群的后台工作：群反馈收集与定时整理/摘要/群小结（F1）、云端代理进度和凭据到期提醒（F3）、
浮屿仓库动态通知（F2）。"""

from __future__ import annotations

import logging
import threading
import time

from . import feedback, fuyu
from .messages import Message
from .sending import SendJob

log = logging.getLogger("fuxi")


def _date_ts(d: str) -> float:
    try:
        return time.mktime(time.strptime(d[:10], "%Y-%m-%d"))
    except ValueError:
        return float("inf")


class GroupWorkMixin:
    # ---------- 群反馈（F1） ----------
    def is_feedback_group(self, chat: str) -> bool:
        groups = self.cfg.get("feedback_groups")
        return bool(groups) and chat.endswith("@chatroom") and (chat in groups or self.name_of(chat) in groups)

    def _collect(self, new: list[Message]) -> None:
        """反馈群的每条消息都记下来（不管白名单、静默、有没有 @ bot），整理交给 feedback.py。"""
        for m in new:
            try:
                self.outreach.observe(m)
            except Exception:
                log.exception("群营业观察消息出错")
            if m.type in ("system", "revoke") or not m.text.strip():
                continue
            if m.is_self:
                role, name = "bot", "浮汐"
            else:
                role = "owner" if m.sender in self.masters else ""
                name = self.name_of(m.sender) if m.sender else "群友"
            try:
                self.feedback.add(m.msg_id, float(m.create_time), m.chat, m.sender, name, self._render(m), role)
            except OSError as e:
                log.warning("反馈收集写入失败：%s", e)

    def _feedback_loop(self) -> None:
        """每分钟看一次：整点前后整理（每小时）、到点发摘要和群小结。"""
        time.sleep(60)
        while True:
            try:
                self._feedback_tick()
            except Exception:
                log.exception("群反馈定时任务出错")
            time.sleep(60)

    def _feedback_tick(self, now: float | None = None) -> None:
        if not self.cfg.get("feedback_groups"):
            return
        fb, now = self.feedback, now or time.time()
        try:
            self.outreach.tick(now)
        except Exception:
            log.exception("群营业定时任务出错")
        try:
            self._agent_watch(now)
        except Exception:
            log.exception("云端代理检查出错")
        digest_due = fb.due("digest", self.cfg.get("digest_time"), now)
        group_due = fb.due("group", self.cfg.get("group_summary_time"), now)
        if digest_due or group_due or now - float(fb.meta.get("classified_at") or 0) >= feedback.CLASSIFY_EVERY:
            try:
                msg = fb.classify(now)
                if msg != "没有新消息":
                    log.info("群反馈：%s", msg)
            except Exception as e:   # 整理失败也照样发（用已有的条目），错误会写在摘要末尾
                log.warning("群反馈整理失败：%s", e)
        if digest_due:
            since = float(fb.meta.get("digest_at") or 0) or now - 86400
            text = fb.digest(since=max(since, now - 3 * 86400))
            to = self.masters[0] if self.masters else "filehelper"
            fb.mark("digest", now)
            job = self.send(SendJob(chat=to, text=text, source="control"))
            if job.ok:
                log.info("反馈摘要已发给主人")
            else:
                log.warning("反馈摘要没发出去：%s", job.error)
        if group_due:
            fb.mark("group", now)
            text = fb.group_summary(feedback._day(now))
            if not text:
                log.info("群反馈：今天没有反馈，不发群小结")
                return
            day = feedback._day(now)
            chats = {c for i in fb.items if feedback._day(i["last_seen"]) == day for c in i["chats"]}
            for chat in sorted(chats):
                if not self.is_feedback_group(chat) or not self._allowed_chat(chat):
                    continue
                job = self.send(SendJob(chat=chat, text=text, source="control"))
                if job.ok:
                    self.outreach.bot_posted(chat, now)
                    log.info("群小结已发到 %s", self.name_of(chat))
                else:
                    log.warning("群小结没发到 %s：%s", self.name_of(chat), job.error)

    AGENT_LATE = 2 * 3600

    def _agent_watch(self, now: float) -> None:
        """派给代理超过 2 小时还没结果就提醒主人一次（错过了 PR 事件的，用 PR 缓存补上）；
        开 issue 的 token、代理凭据到期前 14 天每天提醒一次。"""
        texts = []
        for it in self.feedback.agent_waiting():
            prs = [p for p in self.fuyu.fixes_for(it["issue"]) if fuyu._ts(p.get("created_at")) >= it["agent_at"] - 60]
            if prs:
                self.feedback.set_fields(it, agent_pr=prs[-1]["number"])
            elif now - it["agent_at"] > self.AGENT_LATE and not it.get("agent_late"):
                self.feedback.set_fields(it, agent_late=True)
                repo = self.cfg.get("knowledge_repo") or self.fuyu.repo
                look = ("Mac 开着吗？日志在 ~/.fuxi-fix/logs/" if it.get("agent_where") == "local"
                        else f"看看运行记录：https://github.com/{repo}/actions/workflows/claude-fix.yml")
                texts.append(f"【代理还没结果】反馈 #{it['id']}「{it['title']}」→ issue #{it['issue']}，派出已超过 2 小时。\n{look}")
        gh = self.fixflow.gh
        if gh.ready and now - gh.checked_at > 86400:
            try:
                gh.check()
            except Exception as e:
                log.warning("检查开 issue 的 token 失败：%s", e)
        day = feedback._day(now)
        if self.feedback.meta.get("expiry_day") != day:
            soon = [f"{name}将在 {d} 到期" for name, d in self.token_expiry() if 0 <= (_date_ts(d) - now) / 86400 <= 14]
            soon += [f"{name}已在 {d} 过期" for name, d in self.token_expiry() if _date_ts(d) < now]
            if soon:
                self.feedback.meta["expiry_day"] = day
                self.feedback._save()
                texts.append("【到期提醒】" + "；".join(soon) + "。到期后 /修 用不了，记得换新的（步骤见 docs/feedback-loop.md）。")
        for text in texts:
            job = self.send(SendJob(chat="filehelper", text=text, source="control"))
            if not job.ok:
                log.warning("云端代理提醒没发出去：%s", job.error)

    def token_expiry(self) -> list[tuple[str, str]]:
        """[(名称, YYYY-MM-DD)]：开 issue 的 token（GitHub 响应头）、代理凭据（设置「代理凭据到期」）。"""
        out = []
        if self.fixflow.gh.expires:
            out.append(("开 issue 的 token", self.fixflow.gh.expires[:10]))
        if self.cfg.get("agent_token_expires"):
            out.append(("代理凭据 CLAUDE_CODE_OAUTH_TOKEN", self.cfg.get("agent_token_expires")))
        return out

    def _fuyu_events(self, events: list[dict]) -> None:
        """浮屿仓库同步发现变化：发正式版时通知群里的反馈人（见 outreach.py）；按「浮屿动态」设置私聊主人（文件传输助手）。"""
        for e in events:
            if e.get("type") == "new_release" and not e.get("prerelease") and self.cfg.get("outreach"):
                threading.Thread(target=self.outreach.on_release, args=(e["tag"],), name="fix-notify", daemon=True).start()
        texts = []
        for e in events:   # /修 派给代理的 issue 有结果了：不管「浮屿动态」怎么设都告诉主人
            if e.get("type") == "pr_opened":
                for it in self.feedback.agent_items(e.get("refs") or []):
                    if it.get("agent_pr"):
                        continue
                    self.feedback.set_fields(it, agent_pr=e["number"])
                    texts.append(f"【代理出 PR 了】反馈 #{it['id']}「{it['title']}」→ issue #{it['issue']}\n"
                                 f"PR #{e['number']}：{e['title']}\n{e['url']}\n看过没问题就在 GitHub 上转正式并合并。")
            elif e.get("type") == "agent_stuck":
                for it in self.feedback.agent_items([e["number"]]):
                    if it.get("agent_stuck") or it.get("agent_pr"):
                        continue
                    self.feedback.set_fields(it, agent_stuck=True)
                    texts.append(f"【代理没修成】反馈 #{it['id']}「{it['title']}」→ issue #{it['issue']}\n"
                                 f"它查到的情况写在 issue 评论里：{e['url']}\n可以补充线索后再 /修 {it['id']}，或者自己修。")
        mode = self.cfg.get("fuyu_notify")
        if mode == "release":
            events = [e for e in events if e.get("type") == "new_release"]
        if events and mode != "off":
            lines = [fuyu.render_event(e) for e in events[:12]]
            if len(events) > 12:
                lines.append(f"……还有 {len(events) - 12} 条（/浮屿 看最近动态）")
            texts.append("【浮屿动态】\n" + "\n".join("- " + x for x in lines))
        if not texts:
            return
        text = "\n\n".join(texts)

        def go():   # 同步线程还拿着锁，发送可能要等几十秒，放到别的线程里
            job = self.send(SendJob(chat="filehelper", text=text, source="control"))
            if not job.ok:
                log.warning("浮屿动态通知没发出去：%s", job.error)
        threading.Thread(target=go, name="fuyu-notify", daemon=True).start()
