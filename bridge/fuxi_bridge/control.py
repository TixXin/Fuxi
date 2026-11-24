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
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

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
        if b.vision_mode != "0":
            lines.append("看图：" + ("已就绪" if b.images.uin is not None else "还没有图片密钥（收到图片后自动推算）")
                         + ("，只看私聊" if b.vision_mode == "private" else ""))
        up = int(time.time() - b.started)
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
        items = self.b.allowlist.items
        if not items:
            return "白名单为空（谁都不回）。"
        return "白名单：\n" + "\n".join(f"- {'全部放行' if i == '*' else self.b.name_of(i)}" for i in items)

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
            return "主动搭话已关闭（FUXI_CARE=0）。"
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

    # ---- 工具 ----
    def _allow_brief(self) -> str:
        items = self.b.allowlist.items
        return "全部放行" if "*" in items else f"{len(items)} 个会话"

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
}
