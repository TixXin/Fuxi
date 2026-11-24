"""会话白名单：文件热加载 + API 修改 + 同步到内核。

文件 /state/allow.txt，每行一个 chat_id，`#` 之后是注释（写显示名方便人看）：

    filehelper          # 文件传输助手
    wxid_xxxxxxxx       # 某人
    *                   # 放行全部（慎用）

语义：
- 空名单 = 谁都不回（安全默认）；`*` = 全部放行。
- 文件不存在时用环境变量 FUXI_ALLOW 生成初始名单（之后以文件为准）。
- 每次轮询读一下文件（很小），内容变了就生效，**改完即生效，无需重启**。
- 变化后同步到内核：wechat.whitelistOnly + care.whitelist（内核发送前的第二道防线）。

桥接这层必须保留：内核的白名单只在「发送前」拦截，不在名单里的消息仍会进内核、调大模型。

放宽范围不改这个文件，走设置（settings.json，管理台可改）：
- 「回复范围」scope：list 只回名单里的（默认）/ private 另外回全部私聊 / groups 另外回全部群 / all 全部；
- 「不回的会话」blocked：群名、备注或 chat_id，任何范围下都不回，优先级最高。
放宽后内核的 whitelistOnly 会关掉（否则内核会把名单外的拦下），桥接这层仍按范围和黑名单把关。
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable

log = logging.getLogger("fuxi.allow")

ALL = "*"
SCOPES = {"list": "只回名单里的", "private": "名单 + 全部私聊", "groups": "名单 + 全部群", "all": "全部私聊和群"}


def _parse(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        item = line.split("#", 1)[0].strip()
        if item and item not in out:
            out.append(item)
    return out


class AllowList:
    def __init__(self, path: str, seed: str, on_change: Callable[[list[str]], None] | None = None):
        self.path = path
        self.on_change = on_change
        self._lock = threading.Lock()
        self._raw: str | None = None
        self.items: list[str] = []
        self.scope = "list"                 # 见 SCOPES；Bridge.apply_settings 从设置抄过来
        self.blocked: set[str] = set()      # chat_id 或名字
        self.name_of: Callable[[str], str] | None = None
        if not os.path.exists(path):
            self._write([s.strip() for s in seed.split(",") if s.strip()], {})
            log.info("已用 FUXI_ALLOW 生成初始白名单 %s", path)
        self.reload(force=True)

    # ---- 读 ----
    def reload(self, force: bool = False) -> bool:
        """文件变了就重读，返回是否有变化。"""
        # 直接比较文件内容：文件很小，而 mtime 在快速连续写入时可能不变（时间戳粒度）
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = f.read()
        except FileNotFoundError:
            raw = ""
        if not force and raw == self._raw:
            return False
        with self._lock:
            self._raw = raw
            new = _parse(raw)
            changed = new != self.items
            self.items = new
        if changed or force:
            log.info("白名单%s：%s", "已加载" if force else "已更新", ", ".join(self._masked()) or "（空，谁都不回）")
            if self.on_change:
                try:
                    self.on_change(list(new))
                except Exception as e:  # 同步内核失败不影响桥接自己的白名单
                    log.warning("同步白名单到内核失败：%s", e)
        return changed

    def _masked(self) -> list[str]:
        return [i if (i in (ALL, "filehelper") or "@" in i) else i[:6] + "…" for i in self.items]

    def is_blocked(self, chat: str) -> bool:
        if not self.blocked:
            return False
        if chat in self.blocked:
            return True
        try:
            return bool(self.name_of) and self.name_of(chat) in self.blocked
        except Exception:
            return False

    def allows(self, chat: str) -> bool:
        if chat != "filehelper" and self.is_blocked(chat):
            return False
        items = self.items
        if ALL in items or chat in items:
            return True
        group = chat.endswith("@chatroom")
        return (self.scope == "all" or (self.scope == "private" and not group)
                or (self.scope == "groups" and group))

    @property
    def open(self) -> bool:
        """名单以外也会回（范围放宽了或名单里有 *）。"""
        return self.scope != "list" or ALL in self.items

    def brief(self) -> str:
        if ALL in self.items:
            text = "全部放行"
        elif self.scope == "list":
            text = f"白名单 {len(self.items)} 个会话"
        else:
            text = f"{SCOPES.get(self.scope, self.scope)}（名单 {len(self.items)} 个）"
        return text + (f"，{len(self.blocked)} 个不回" if self.blocked else "")

    # ---- 写（API 用） ----
    def _write(self, items: list[str], names: dict[str, str]) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        lines = ["# 浮汐白名单：每行一个 chat_id，# 后为注释；空 = 谁都不回，* = 全部放行", ""]
        for i in items:
            n = names.get(i, "")
            lines.append(f"{i:<40} # {n}" if n else i)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, self.path)

    def set(self, items: list[str], names: dict[str, str]) -> list[str]:
        clean = []
        for i in items:
            if i and i not in clean:
                clean.append(i)
        with self._lock:
            self._write(clean, names)
        self.reload(force=True)
        return self.items
