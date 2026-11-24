"""文件传输助手遥控器：主人在文件传输助手里发 `/指令`，桥接执行并回报。

只认「自己发到 filehelper」的消息（别人无法往你的文件传输助手里写），且必须以 / 开头、
是下面登记过的指令；其余消息照常处理。指令不交给内核，也不受白名单/静默时段/暂停限制。

  /帮助              指令列表
  /状态              熔断、暂停、微信窗口、问题清单、白名单、进入会话耗时
  /暂停              停止自动回复与提醒（API 主动发送不受影响），重启后仍保持
  /恢复              取消暂停并解除熔断
  /名单              查看白名单
  /允许 名字或ID      加入白名单（名字须唯一匹配：备注/昵称/群名）
  /禁止 名字或ID      移出白名单
  /测试 名字          只打开该会话并核对标题、不发送，回报耗时
  /存表情 [分类]      把 10 分钟内发到这里的最新一个表情存进表情包库（分类默认「开心」，还有傲娇、炸毛）
  /搭话              立即检查一次主动搭话（仍受内核时间窗/间隔/上限约束），回报结果
  /待发              列出等确认的发版公告；/发布 [编号] 发出，/取消 [编号] 放弃
  /资料              产品助手资料库状态；/资料 同步 立即同步；/资料 搜 问题 看会检索到哪几段；
                     /资料 问答 查看自定义问答；/资料 记 问题 | 回答 新增一条；/资料 删 编号 删除
  /反馈              最近 7 天整理出的群反馈；/反馈 3 看详情和原话；/反馈 整理 立即整理；
                     /反馈 摘要 预览给主人的摘要；/反馈 小结 预览今晚的群小结（只预览，不发）
  /忽略 编号          这条反馈不再出现在摘要和群小结里
  /修 编号 [补充]      预览要在浮屿仓库开的 issue（已脱敏）；/修 3 确认 开出并交给云端代理修，出 draft PR 后通知你；
                     /修 3 本地 交给 Mac 上的本机修复（要在 macOS 上构建/测试的问题）
  /开issue 编号 [补充] 同上，但只开 issue 不派代理；反馈已对上现有 issue 时改为补评论（/修 还会打 claude-fix）
  /营业              反馈群营业状态（冷场多久、今天说过没、待欢迎）；/营业 试写 生成一条冷场发言看看（不发）；
                     /营业 欢迎 看欢迎语；/营业 修复 v0.3.14 预览这个版本的修复通知（不发）
  /浮屿              浮屿版本、issue 统计和最近动态；/浮屿 123 查进度；/浮屿 搜 问题 找相关 issue；/浮屿 同步
  /定时              等发的定时消息；/定时 取消 编号 取消一条（定时在管理台「发送」页或 API POST /schedule 设）
  /设置              查看全部设置；/设置 看图 看说明；/设置 看图 只看私聊 修改（立即生效）
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

from . import allow

if TYPE_CHECKING:
    from .app import Bridge
    from .messages import Message

log = logging.getLogger("fuxi.control")

HELP = __doc__.split("\n\n", 2)[2].strip().replace("  /", "/")


def is_command(m: "Message", self_wxid: str) -> bool:
    if m.chat != "filehelper" or not m.is_self or m.type != "text":
        return False
    head = m.text.strip().split(maxsplit=1)[0] if m.text.strip() else ""
    return head in COMMANDS


def parse(text: str) -> tuple[str, str]:
    parts = text.strip().split(maxsplit=1)
    return parts[0], (parts[1].strip() if len(parts) > 1 else "")


STICKER_KINDS = {"开心": "happy", "傲娇": "cute", "撒娇": "cute", "炸毛": "angry"}


def sticker_ext(data: bytes) -> str:
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ""


class Control:
    def __init__(self, bridge: "Bridge"):
        self.b = bridge

    def handle(self, text: str) -> str:
        cmd, arg = parse(text)
        fn = getattr(self, COMMANDS[cmd])
        log.warning("遥控指令 %s", cmd)
        try:
            return fn(arg)
        except Exception as e:  # 指令失败也要回报，不能让主人干等
            log.exception("遥控指令 %s 失败", cmd)
            return f"{cmd} 执行出错：{e}"

    # ---- 指令 ----
    def help(self, _arg: str) -> str:
        return "浮汐遥控指令：\n" + HELP

    def status(self, _arg: str) -> str:
        b = self.b
        probs = b.problems()
        lines = [
            "运行中" if not (b.halted or b.paused) else ("已熔断" if b.halted else "已暂停"),
            f"熔断：{b.halted or '无'}",
            f"暂停：{'是（/恢复 取消）' if b.paused else '否'}",
            f"白名单：{self._allow_brief()}",
            f"静默时段：{b.quiet or '无'}{'（当前静默中）' if b.in_quiet_hours() else ''}",
        ]
        st = b.sender.open_stats()
        if st.get("n"):
            lines.append(f"进入会话：中位 {st['p50']} 秒，最慢 {st['max']} 秒（近 {st['n']} 次）")
        lat = b.latency.brief()
        if lat:
            lines.append(f"回复耗时：{lat}")
        if b.care_on:
            lines.append(f"主动搭话：{b.care.brief()}")
        if b.vision_mode != "off":
            lines.append("看图：" + ("已就绪" if b.images.uin is not None else "还没有图片密钥（收到图片后自动推算）")
                         + ("，只看私聊" if b.vision_mode == "private" else ""))
        if b.voice_mode != "off":
            lines.append("语音转文字：开" + ("（只听私聊）" if b.voice_mode == "private" else ""))
        if b.cfg.get("assistant_groups"):
            lines.append(f"产品助手：{len(b.cfg.get('assistant_groups'))} 个群；资料 {b.knowledge.brief()}")
        if b.cfg.get("feedback_groups"):
            lines.append(f"群反馈：{b.feedback.brief()}")
        if b.cfg.get("fuyu_sync"):
            lines.append(f"浮屿仓库：{b.fuyu.brief()}")
        if b.fixflow.gh.ready or b.cfg.get("agent_token_expires"):
            wait = len(b.feedback.agent_waiting())
            exp = "；".join(f"{n}到 {d}" for n, d in b.token_expiry()) or "到期日未知"
            lines.append(f"云端代理：{'在修 ' + str(wait) + ' 条；' if wait else ''}{exp}")
        up = int(time.time() - b.started)
        lines.append(f"版本：{os.environ.get('FUXI_BUILD', 'dev')}")
        lines.append(f"已运行：{up // 3600} 小时 {up % 3600 // 60} 分")
        lines.append("问题：" + ("；".join(p for p in probs if not p.startswith("已熔断")) or "无"))
        return "\n".join(lines)

    def pause(self, _arg: str) -> str:
        self.b.set_paused(True)
        return "已暂停自动回复与提醒。/恢复 取消。"

    def resume(self, _arg: str) -> str:
        b = self.b
        was = b.halted
        b.halted = ""
        b.set_paused(False)
        b.sender.forget_chat()
        return "已恢复。" + (f"（解除了熔断：{was}）" if was else "")

    def list_allow(self, _arg: str) -> str:
        al = self.b.allowlist
        items = al.items
        head = f"回复范围：{allow.SCOPES.get(al.scope, al.scope)}（/设置 回复范围 改）\n"
        if al.blocked:
            head += "不回的会话：" + "、".join(sorted(al.blocked)) + "\n"
        if not items:
            return head + ("白名单为空。" if al.open else "白名单为空（谁都不回）。")
        return head + "白名单：\n" + "\n".join(f"- {'全部放行' if i == '*' else self.b.name_of(i)}" for i in items)

    def allow(self, arg: str) -> str:
        chat, err = self._resolve(arg)
        if err:
            return err
        b = self.b
        cur = list(b.allowlist.items)
        if chat in cur:
            return f"「{b.name_of(chat)}」已在白名单里。"
        names = {i: b.name_of(i) for i in cur if i != "*"} | {chat: b.name_of(chat)}
        b.allowlist.set(cur + [chat], names)
        return f"已允许「{b.name_of(chat)}」。当前 {self._allow_brief()}。"

    def deny(self, arg: str) -> str:
        chat, err = self._resolve(arg)
        if err:
            return err
        b = self.b
        cur = list(b.allowlist.items)
        if chat not in cur:
            return f"「{b.name_of(chat)}」不在白名单里。" + ("（注意：当前是 * 全部放行）" if "*" in cur else "")
        names = {i: b.name_of(i) for i in cur if i != "*"}
        b.allowlist.set([i for i in cur if i != chat], names)
        return f"已禁止「{b.name_of(chat)}」。当前 {self._allow_brief()}。"

    def test_open(self, arg: str) -> str:
        chat, err = self._resolve(arg)
        if err:
            return err
        name = self.b.name_of(chat)
        try:
            dt = self.b.sender.probe_open(name)
        except Exception as e:
            return f"打开「{name}」失败：{e}"
        return f"已打开并核对「{name}」，耗时 {dt:.2f} 秒（未发送任何内容）。"

    def care(self, _arg: str) -> str:
        b = self.b
        if not b.care_on:
            return "主动搭话已关闭（/设置 主动搭话 开）。"
        r = b.care.tick()
        return "已发出一次主动搭话。" if r == "sent" else f"这次不发：{r}"

    def save_sticker(self, arg: str) -> str:
        import urllib.request

        from . import messages
        b = self.b
        label = arg.strip() or "开心"
        emotion = STICKER_KINDS.get(label)
        if not emotion:
            return f"不认识的分类「{label}」，可选：开心、傲娇、炸毛。"
        xml = b.reader.last_emoji("filehelper", int(time.time()) - 600)
        if not xml:
            return "10 分钟内没在文件传输助手里看到表情。先从手机往文件传输助手发一个表情，再发 /存表情。"
        url = messages.emoji_url(xml)
        if not url.startswith(("http://", "https://")):
            return "这个表情没有下载地址（电脑端发的表情没有），请从手机发一次。"
        with urllib.request.urlopen(url, timeout=20) as r:
            data = r.read(4 * 1024 * 1024 + 1)
        ext = sticker_ext(data)
        if not ext:
            return "下载到的不是 GIF/PNG/JPG/WebP 图片，存不了。"
        if len(data) > 4 * 1024 * 1024:
            return "这个表情超过 4 MB，存不了。"
        import hashlib
        name = "wx-" + hashlib.md5(data).hexdigest()[:12] + ext
        b.kernel.sticker_upload(emotion, name, data)
        return f"已存进「{label}」（{len(data) // 1024} KB）。"

    def settings(self, arg: str) -> str:
        from . import settings as st
        from .kernel import KernelError
        b = self.b
        name, _, value = arg.strip().partition(" ")
        if not name:
            lines = ["设置（/设置 名称 新值 修改，立即生效）："]
            for it in st.ITEMS:
                try:
                    v = self._show_setting(it, b.get_setting(it.key))
                except KernelError:
                    v = "（内核没响应）"
                lines.append(f"- {it.label}：{v}")
            return "\n".join(lines)
        it = st.find(name)
        if not it:
            return f"没有「{name}」这项。可选：" + "、".join(i.label for i in st.ITEMS)
        if not value.strip():
            extra = ("\n可选：" + " / ".join(it.choices.values())) if it.choices else ""
            extra += "\n可填：开 / 关" if it.type == "bool" else ""
            extra += "\n多个用逗号隔开，填「无」清空" if it.type == "list" else ""
            try:
                cur = self._show_setting(it, b.get_setting(it.key))
            except KernelError as e:
                cur = f"（内核没响应：{e}）"
            return f"{it.label}：{cur}\n{it.desc}{extra}"
        raw = value.strip()
        if it.key == "owners" and raw not in ("无", "空", "清空"):
            ids = []
            for part in [p for p in raw.replace("，", ",").replace("、", ",").split(",") if p.strip()]:
                chat, err = self._resolve(part)
                if err:
                    return err
                if chat.endswith("@chatroom") or chat in ("*", "filehelper"):
                    return f"「{part}」不是一个人。"
                ids.append(chat)
            raw = ",".join(ids)
        try:
            v = b.set_setting(it.key, raw)
        except ValueError as e:
            return str(e)
        except KernelError as e:
            return f"内核没改成：{e}"
        return f"已设置 {it.label}：{self._show_setting(it, v)}"

    def knowledge(self, arg: str) -> str:
        b = self.b
        cmd, _, rest = arg.strip().partition(" ")
        if cmd == "同步":
            try:
                return b.knowledge.sync(b.cfg.get("knowledge_repo"))
            except Exception as e:
                return f"同步失败：{e}"
        if cmd == "搜":
            if not rest.strip():
                return "用法：/资料 搜 浮屿怎么在 Mac 上安装"
            hits = b.knowledge.search(rest)
            if not hits:
                return "没找到够相关的资料（群里会按「不确定、转主人」处理）。"
            return "会附上这几段：\n" + "\n".join(f"- {h.title}：{h.text[:60]}…" for h in hits)
        if cmd == "问答":
            items = b.knowledge.faq_list()
            if not items:
                return "还没有自定义常见问答。新增：/资料 记 问题 | 回答"
            return f"自定义常见问答（{len(items)} 条）：\n" + "\n".join(
                f"#{it['id']} {it['q']} → {it['a'][:50]}{'…' if len(it['a']) > 50 else ''}" for it in items[:20]
            )
        if cmd == "记":
            sep = "|" if "|" in rest else ("｜" if "｜" in rest else "")
            if not sep:
                return "用法：/资料 记 问题 | 回答"
            q, _, a = rest.partition(sep)
            try:
                items = b.knowledge.faq_save(q, a)
            except ValueError as e:
                return str(e)
            if hasattr(b, "events"):
                b.events.publish("knowledge", {"result": ""})
            return f"已记下常见问答 #{items[0]['id']}（共 {len(items)} 条），立即参与检索。"
        if cmd == "删":
            n = rest.strip().lstrip("#")
            if not n.isdigit():
                return "用法：/资料 删 编号"
            try:
                items = b.knowledge.faq_delete(int(n))
            except ValueError as e:
                return str(e)
            if hasattr(b, "events"):
                b.events.publish("knowledge", {"result": ""})
            return f"已删除常见问答 #{n}（剩 {len(items)} 条）。"
        faqs = len(b.knowledge.faq_list()) if hasattr(b.knowledge, "faq_list") else 0
        return f"资料库：{b.knowledge.brief()}（自定义问答 {faqs} 条，来源 {b.cfg.get('knowledge_repo')}）\n" \
               f"产品助手群：{'、'.join(b.cfg.get('assistant_groups')) or '未设置（/设置 产品助手群 群名）'}"

    def feedback(self, arg: str) -> str:
        b = self.b
        cmd = arg.strip().lstrip("#")
        if cmd.isdigit():
            return b.feedback.detail(int(cmd))
        if cmd == "整理":
            try:
                return b.feedback.classify()
            except Exception as e:
                return f"整理失败：{e}"
        if cmd == "摘要":
            return b.feedback.digest(since=time.time() - 86400)
        if cmd == "小结":
            return b.feedback.group_summary() or "今天还没有反馈，今晚不会发群小结。"
        groups = b.cfg.get("feedback_groups")
        head = (f"反馈群：{'、'.join(groups)}；{b.feedback.brief()}\n" if groups
                else "还没设置反馈群（/设置 反馈群 群名）\n")
        return head + b.feedback.listing()

    def outreach(self, arg: str) -> str:
        b = self.b
        cmd, _, rest = arg.strip().partition(" ")
        if cmd == "试写":
            return "冷场时会发类似这样的一条：\n" + b.outreach.idle_text()
        if cmd == "欢迎":
            text, at = b.outreach.welcome_text("", [{"wxid": "", "name": "新朋友"}])
            return f"@{at} {text}"
        if cmd == "修复":
            tag = rest.strip()
            if not tag:
                return "用法：/营业 修复 v0.3.14"
            notes = b.outreach.fix_notes(tag)
            if not notes:
                return f"{tag} 没有对应的群反馈（或都已通知过）。"
            return "\n\n".join(f"→ {b.name_of(c)}：{'@' + at + ' ' if at else ''}{t}" for c, t, at, _ in notes)
        if not b.cfg.get("outreach"):
            return "群营业已关闭（/设置 群营业 开）"
        return (f"群营业：{b.outreach.brief()}\n营业时段 {b.cfg.get('open_hours')}，冷场 {b.cfg.get('idle_hours')} 小时，"
                f"修复通知{b.cfg.get('fix_notify') and {'preview': '先预览', 'direct': '直接发', 'off': '关闭'}[b.cfg.get('fix_notify')]}")

    def fix(self, arg: str) -> str:
        return self.b.fixflow.handle(arg, fix=True)

    def open_issue(self, arg: str) -> str:
        return self.b.fixflow.handle(arg, fix=False)

    def ignore(self, arg: str) -> str:
        n = arg.strip().lstrip("#")
        if not n.isdigit():
            return "用法：/忽略 3"
        return self.b.feedback.ignore(int(n))

    def fuyu(self, arg: str) -> str:
        from .fuyu import render_event
        b = self.b
        cmd, _, rest = arg.strip().partition(" ")
        num = cmd.lstrip("#")
        if cmd == "同步":
            try:
                return b.fuyu.sync(b.cfg.get("knowledge_repo"))
            except Exception as e:
                return f"同步失败：{e}"
        if num.isdigit():
            return b.fuyu.status(int(num))
        if cmd == "搜":
            if not rest.strip():
                return "用法：/浮屿 搜 设置页闪退"
            hits = b.fuyu.find(rest, k=5)
            if not hits:
                return "没找到相关的 issue。"
            return "\n".join(f"{s:.2f} {b.fuyu.status(it['number'])}" for s, it in hits)
        events = b.fuyu.recent_events(6)
        lines = [b.fuyu.version_brief(), f"仓库 {b.cfg.get('knowledge_repo')}：{b.fuyu.brief()}"]
        if events:
            lines.append("最近动态：\n" + "\n".join("- " + render_event(e) for e in reversed(events)))
        return "\n".join(lines)

    def scheduled(self, arg: str) -> str:
        from .schedule import ScheduleError
        parts = arg.split()
        if parts[:1] == ["取消"]:
            if len(parts) != 2 or not parts[1].lstrip("#").isdigit():
                return "用法：/定时 取消 编号"
            try:
                it = self.b.scheduler.cancel(int(parts[1].lstrip("#")))
            except ScheduleError as e:
                return str(e)
            return f"已取消定时消息 #{it['id']}（发给「{it['name']}」）。"
        pending = self.b.scheduler.list()["pending"]
        if not pending:
            return "没有等发的定时消息。"
        lines = [f"等发的定时消息 {len(pending)} 条："]
        for it in pending[:20]:
            what = (it["text"] or "") if not it["template"] else f"[模板 {it['template']}]"
            what = (what[:20] + "…") if len(what) > 20 else what
            lines.append(f"#{it['id']} {time.strftime('%m-%d %H:%M', time.localtime(it['at']))} → {it['name']}："
                         f"{what}{' +图' if it['has_image'] else ''}")
        lines.append("取消：/定时 取消 编号")
        return "\n".join(lines)

    def announce_list(self, _arg: str) -> str:
        return self.b.announcer.listing()

    def announce_publish(self, arg: str) -> str:
        from .announce import AnnounceError
        try:
            it = self.b.announcer.publish(arg)
        except AnnounceError as e:
            return str(e)
        if it["status"] == "sent":
            return f"#{it['code']} 已发到「{it['to']}」。"
        return f"#{it['code']} 发送失败：{it.get('error')}（不会自动重试，避免重复发）"

    def announce_cancel(self, arg: str) -> str:
        from .announce import AnnounceError
        try:
            it = self.b.announcer.cancel(arg)
        except AnnounceError as e:
            return str(e)
        return f"#{it['code']} 已取消，不会发到「{it['to']}」。"

    def _show_setting(self, it, v) -> str:
        from . import settings as st
        if it.key == "owners" and v:
            return "、".join(self.b.name_of(x) for x in v)
        return st.show(it, v)

    # ---- 工具 ----
    def _allow_brief(self) -> str:
        return self.b.allowlist.brief()

    def _resolve(self, arg: str) -> tuple[str, str]:
        arg = arg.strip()
        if not arg:
            return "", "缺少会话名，例如：/允许 浮屿用户交流群"
        if arg == "*":
            return "*", ""
        if arg == "filehelper" or arg.endswith("@chatroom") or arg.startswith("wxid_"):
            return arg, ""
        hits = self.b.reader.find_chats(arg)
        if len(hits) == 1:
            return hits[0]["username"], ""
        if not hits:
            return "", f"没找到「{arg}」（按备注/昵称/群名完全匹配）。"
        return "", f"「{arg}」匹配到 {len(hits)} 个会话，请改用更具体的名字或 chat_id：\n" + "\n".join(
            f"- {h['username']}（{h['remark'] or h['name']}）" for h in hits[:8])


COMMANDS = {
    "/帮助": "help", "/help": "help",
    "/状态": "status",
    "/暂停": "pause",
    "/恢复": "resume",
    "/名单": "list_allow",
    "/允许": "allow",
    "/禁止": "deny",
    "/测试": "test_open",
    "/搭话": "care",
    "/存表情": "save_sticker",
    "/设置": "settings",
    "/待发": "announce_list",
    "/发布": "announce_publish",
    "/取消": "announce_cancel",
    "/资料": "knowledge",
    "/浮屿": "fuyu",
    "/反馈": "feedback",
    "/忽略": "ignore",
    "/修": "fix",
    "/开issue": "open_issue",
    "/营业": "outreach",
    "/定时": "scheduled",
}
