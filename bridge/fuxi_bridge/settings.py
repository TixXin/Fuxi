"""桥接的行为设置：/state/settings.json，改了自动生效（不用重建容器）。

分工：
  .env            只放部署相关（地址、令牌、端口、目录），改了要重启。
  settings.json   桥接的行为开关，这里登记的每一项。遥控 /设置、对外 API（以后的管理台）都改它。
  内核配置        内核自己管的（模型、表情包开关、搭话时段…）。登记项里标了 kernel 的只是转发给内核，
                  桥接不另存一份。

第一次生成 settings.json 时，从旧的环境变量（FUXI_CARE 等）取初始值，之后环境变量不再起作用。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_ON = {"1", "true", "on", "yes", "开", "开启", "打开", "是"}
_OFF = {"0", "false", "off", "no", "关", "关闭", "否"}
_EMPTY = {"", "无", "空", "-", "none", "清空"}
_QUIET = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class Item:
    key: str                 # settings.json 里的键，也是 Bridge 上的属性名
    label: str               # 中文名（遥控指令里用）
    type: str                # bool | int | text | list | choice
    default: object
    desc: str
    choices: dict = field(default_factory=dict)   # choice：值 → 中文
    env: str = ""            # 旧环境变量名：只在第一次生成文件时取初始值
    kernel: tuple = ()       # 非空 = 内核管的配置路径，只转发
    min: int = 0
    max: int = 0
    scale: int = 1           # 内核存小数、这里用整数显示时的倍数（如概率 0.03 显示成 3%）


ITEMS: list[Item] = [
    Item("vision", "看图", "choice", "all", "收到图片时用内核的看图模型看懂内容",
         {"all": "全部", "private": "只看私聊", "off": "关闭"}, env="FUXI_VISION"),
    Item("voice", "语音转文字", "choice", "all", "收到语音时用内核的语音识别转成文字（服务商跟随内核 voice 配置）",
         {"all": "全部", "private": "只听私聊", "off": "关闭"}),
    Item("care_on", "主动搭话", "bool", True, "一阵子没人说话时主动找主人聊一句（时段/间隔/上限由内核定）",
         env="FUXI_CARE"),
    Item("free_chat_pct", "插话概率", "int", 6, "没人 @ 她时，每条群消息有百分之几的概率接一句（要先在人设里打开「群里主动接话」）",
         kernel=("group", "freeChatChance"), min=0, max=30, scale=100),
    Item("free_chat_cooldown", "插话冷却", "int", 0, "她在群里说完话后多少秒内不主动插话（被 @ 照样回）",
         kernel=("group", "cooldownSec"), min=0, max=7200),
    Item("stickers", "表情包", "bool", False, "回复后按情绪发表情包（图库用 /存表情 填）",
         kernel=("features", "stickers")),
    Item("owners", "主人", "list", [], "主人的 wxid：主动搭话只找主人，主人能用内核管理指令",
         env="FUXI_OWNER_WXIDS"),
    Item("self_names", "bot名字", "list", [], "群里有人 @ 这些名字也算 @ bot（微信昵称和群昵称会自动加上）",
         env="FUXI_SELF_NAMES"),
    Item("assistant_groups", "产品助手群", "list", [], "这些群里有人 @ bot 提问时，先从浮屿资料里找相关段落给内核参考；"
         "找不到又像是提问，就私聊主人（群名或 chat_id，多个用逗号）"),
    Item("source_url", "源码地址", "text", "https://github.com/TixXin/Fuxi",
         "有人问 bot 的源码时直接回这个地址（内核是 AGPL-3.0，要让对话的人拿得到源码）；空 = 不回"),
    Item("knowledge_repo", "资料仓库", "text", "TixXin/Fuyu", "产品助手的资料来源：公开 GitHub 仓库（owner/name），每 6 小时同步一次"),
    Item("feedback_groups", "反馈群", "list", [], "收集这些群里对浮屿的反馈（bug / 建议 / 提问），整理后发摘要和群小结"
         "（群名或 chat_id，多个用逗号；空 = 不收集）"),
    Item("digest_time", "反馈摘要时间", "text", "12:00", "每天几点私聊主人发反馈摘要（过去 24 小时）；空 = 不发"),
    Item("group_summary_time", "群小结时间", "text", "20:00", "每天几点在反馈群发当天的反馈小结（不带人名，没有反馈不发）；空 = 不发"),
    Item("outreach", "群营业", "bool", True, "在反馈群里：新人进群欢迎、冷场时每天说一句、修好后 @ 反馈人"),
    Item("idle_hours", "冷场时长", "int", 2, "反馈群多少小时没人说话算冷场（每天最多说一次）", min=1, max=12),
    Item("open_hours", "营业时段", "text", "10:00-21:30", "进群欢迎和冷场发言只在这段时间里发，如 10:00-21:30"),
    Item("fix_notify", "修复通知", "choice", "preview", "浮屿发正式版时 @ 当初反馈这些问题的人",
         {"preview": "先预览", "direct": "直接发", "off": "关闭"}),
    Item("fuyu_sync", "浮屿同步", "bool", True, "每 30 分钟同步浮屿仓库的版本、issue、PR 到 /state/fuyu（仓库同「资料仓库」），"
         "用来查进度、反馈查重"),
    Item("fuyu_notify", "浮屿动态", "choice", "release", "同步发现变化时私聊主人（发到文件传输助手）",
         {"release": "只通知发版", "all": "发版+PR合并+issue开关", "off": "关闭"}),
    Item("agent_token_expires", "代理凭据到期", "text", "", "浮屿仓库 secret CLAUDE_CODE_OAUTH_TOKEN 的到期日（如 2027-11-24），"
         "到期前 14 天每天提醒；空 = 不提醒（开 issue 的 token 到期日自动从 GitHub 读）"),
    Item("announce_to", "发版群", "text", "", "发版公告（POST /announce）默认发到哪个群：群名或 chat_id；空 = 调用方必须指定"),
    Item("quiet", "静默时段", "text", "", "这段时间不自动回复、不主动搭话，如 00:30-07:30；空 = 不静默",
         env="FUXI_QUIET"),
    Item("rate_chat", "单会话限速", "int", 6, "每个会话每分钟最多发几条", env="FUXI_RATE_CHAT", min=1, max=60),
    Item("rate_global", "全局限速", "int", 20, "所有会话合计每分钟最多发几条", env="FUXI_RATE_GLOBAL", min=1, max=200),
    Item("reply_self", "回复自己", "bool", False, "回复自己发到文件传输助手的消息（调试用）",
         env="FUXI_REPLY_SELF"),
    Item("settle_ms", "合并等待", "int", 1500, "收到消息后再等多少毫秒，把连发的几条合在一起回",
         env="FUXI_SETTLE_MS", min=0, max=30000),
    Item("human_quiet_s", "让人先用", "int", 8, "有人在网页版微信里动键盘鼠标时先不发，等停手这么多秒再发；0 = 不等",
         min=0, max=120),
    Item("human_max_wait_s", "最多等人", "int", 120, "等人停手最多等多少秒，超过就这条不发（记为没发出去）",
         min=10, max=900),
    Item("stale_sec", "过期秒数", "int", 120, "超过这么多秒的旧消息不再回复（含停机补回复）",
         env="FUXI_STALE_SEC", min=10, max=3600),
]
BY_KEY = {i.key: i for i in ITEMS}
BY_LABEL = {i.label.replace(" ", "").lower(): i for i in ITEMS}


def find(name: str) -> Item | None:
    return BY_KEY.get(name) or BY_LABEL.get(name.replace(" ", "").lower())


def parse(item: Item, raw) -> object:
    """把用户输入（字符串或 JSON 值）转成该项的值；不合法抛 ValueError（中文说明）。"""
    t = item.type
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in _ON:
            return True
        if s in _OFF:
            return False
        raise ValueError(f"{item.label} 只能是 开 / 关")
    if t == "int":
        try:
            v = int(str(raw).strip())
        except ValueError:
            raise ValueError(f"{item.label} 要填整数") from None
        if item.max and not item.min <= v <= item.max:
            raise ValueError(f"{item.label} 要在 {item.min}~{item.max} 之间")
        return v
    if t == "choice":
        s = str(raw).strip()
        legacy = {"1": "all", "0": "off"}   # 旧 FUXI_VISION 的写法
        s = legacy.get(s, s)
        for k, zh in item.choices.items():
            if s in (k, zh):
                return k
        raise ValueError(f"{item.label} 只能是：" + " / ".join(item.choices.values()))
    if t == "list":
        if isinstance(raw, list):
            vals = [str(x).strip() for x in raw]
        else:
            s = str(raw).strip()
            vals = [] if s.lower() in _EMPTY else re.split(r"[,，、\s]+", s)
        return [v for v in dict.fromkeys(vals) if v]
    # text
    s = str(raw).strip()
    if s.lower() in _EMPTY:
        return ""
    if item.key in ("digest_time", "group_summary_time") and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", s):
        raise ValueError(f"{item.label} 格式：12:00")
    if item.key == "agent_token_expires" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ValueError(f"{item.label} 格式：2027-11-24")
    if item.key in ("quiet", "open_hours") and not _QUIET.match(s):
        raise ValueError(f"{item.label} 格式：00:30-07:30")
    return s


def show(item: Item, v) -> str:
    if item.type == "bool":
        return "开" if v else "关"
    if item.type == "choice":
        return item.choices.get(v, str(v))
    if item.type == "list":
        return "、".join(v) if v else "（空）"
    return str(v) if v != "" else "（空）"


class Settings:
    def __init__(self, path: str, env: dict | None = None):
        self.path = path
        self.values: dict = {}
        self._mtime = 0.0
        self._lock = threading.Lock()
        if os.path.exists(path):
            self.reload()
        else:
            self._seed(env or {})

    def _seed(self, env: dict) -> None:
        """第一次：把还在用的旧环境变量搬进 settings.json。"""
        for it in ITEMS:
            if it.env and env.get(it.env, "").strip() != "":
                try:
                    self.values[it.key] = parse(it, env[it.env])
                except ValueError as e:
                    log.warning("旧环境变量 %s 的值不合法，用默认值：%s", it.env, e)
        self._save()
        if self.values:
            log.info("已从环境变量生成 settings.json：%s", "、".join(BY_KEY[k].label for k in self.values))

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.values, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._mtime = os.path.getmtime(self.path)

    def reload(self) -> bool:
        """文件被外部改过就重新读；返回是否有变化。坏文件保持旧值。"""
        try:
            mt = os.path.getmtime(self.path)
        except OSError:
            return False
        if mt == self._mtime:
            return False
        try:
            with open(self.path) as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            log.warning("settings.json 读不了，保持原设置：%s", e)
            self._mtime = mt
            return False
        vals = {}
        for k, v in (raw.items() if isinstance(raw, dict) else []):
            it = BY_KEY.get(k)
            if not it or it.kernel:
                continue
            try:
                vals[k] = parse(it, v)
            except ValueError as e:
                log.warning("settings.json 里 %s 不合法，用默认值：%s", k, e)
        self._mtime = mt
        changed = vals != self.values
        self.values = vals
        return changed

    def get(self, key: str):
        it = BY_KEY[key]
        v = self.values.get(key, it.default)
        return list(v) if isinstance(v, list) else v

    def set(self, key: str, raw) -> object:
        it = BY_KEY[key]
        if it.kernel:
            raise ValueError(f"{it.label} 由内核管理")
        v = parse(it, raw)
        with self._lock:
            self.values[key] = v
            self._save()
        return v
