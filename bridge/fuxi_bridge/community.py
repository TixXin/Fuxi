"""社区 PR & 插件市场上新播报：
  - 监控插件市场仓库（默认 TixXin/fuyu-plugins）与主仓库（TixXin/Fuyu）的外部贡献者 PR；
  - 排除仓库拥有者本人与常见 bot（以及 community_ignore_users）；
  - 新 PR（非 Draft 或 Draft 转 Ready）→ 立刻私聊主人提醒审核（附快速审 PR 提示）；
  - PR 合并 → 私聊主人 + 在营业时段内发到用户群致谢/上新播报（非营业时段暂存，次日开营发出）；
  - 首次同步只建基线，绝不把历史老 PR 刷出来；已通知事件落盘去重。
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

from . import outreach
from .sending import SendJob

if TYPE_CHECKING:
    from .app import Bridge

log = logging.getLogger("fuxi.community")

API = "https://api.github.com/repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=30"
SYNC_EVERY = 10 * 60
NOTIFIED_MAX = 400
BOT_USERS = {"dependabot", "renovate", "github-actions", "claude", "pre-commit-ci", "codecov", "copilot"}


def _clean_summary(body: str, limit: int = 120) -> str:
    """从 PR 正文里抽出一句可读摘要（去掉 Markdown 标题、注释、图片、代码块和多余空行）。"""
    text = re.sub(r"<!--[\s\S]*?-->", " ", body or "")
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    lines = []
    for raw in text.splitlines():
        s = re.sub(r"^[#>*\-\s`]+", "", raw).strip()
        if not s or len(s) < 4:
            continue
        if s.lower().startswith(("fixes #", "closes #", "resolves #", "signed-off-by", "co-authored-by")):
            continue
        lines.append(s)
        if len(" ".join(lines)) >= limit:
            break
    out = " ".join(lines).strip()
    return (out[:limit - 1].rstrip() + "…") if len(out) > limit else out


class CommunityWatcher:
    def __init__(self, bridge: "Bridge", root: str, token: str | None = None, http=None):
        self.b = bridge
        self.root = root
        self.token = token if token is not None else os.environ.get("FUXI_GITHUB_TOKEN", "")
        self.http = http or self._http
        os.makedirs(root, mode=0o700, exist_ok=True)
        self._lock = threading.RLock()
        self.state: dict = self._load()

    def _path(self) -> str:
        return os.path.join(self.root, "state.json")

    def _load(self) -> dict:
        try:
            with open(self._path(), encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {"plugins_repo": "", "plugins_etag": "", "plugins_synced_at": 0.0,
                "plugins_prs": {}, "notified": [], "pending_group": [], "last_error": ""}

    def _save(self) -> None:
        self.state["notified"] = (self.state.get("notified") or [])[-NOTIFIED_MAX:]
        p = self._path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)

    def _http(self, url: str, etag: str = "") -> tuple[int, dict, bytes]:
        headers = {"User-Agent": "fuxi-bridge", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if etag:
            headers["If-None-Match"] = etag
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.status, dict(r.headers), r.read(4 << 20)
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, dict(e.headers), b""
            raise RuntimeError(f"GitHub 返回 {e.code}") from None

    def _get_json(self, url: str, etag: str = ""):
        for attempt in range(3):
            try:
                status, headers, body = self.http(url, etag)
                if status == 304:
                    return 304, headers, None
                return status, headers, json.loads((body or b"[]").decode("utf-8"))
            except (OSError, http.client.HTTPException, ValueError) as e:
                if attempt == 2:
                    raise RuntimeError(f"网络中断（{type(e).__name__}）") from None
                time.sleep(2 * (attempt + 1))

    # ---- 过滤与去重 ----
    def is_external(self, user: str, repo: str) -> bool:
        """排除仓库作者本人、常见 bot 及 community_ignore_users。"""
        u = (user or "").strip().lower()
        if not u:
            return False
        owner = (repo or "").split("/")[0].strip().lower()
        if owner and u == owner:
            return False
        if u.endswith("[bot]") or u in BOT_USERS:
            return False
        ignore = {str(x).strip().lower() for x in (self.b.cfg.get("community_ignore_users") or []) if x}
        return u not in ignore

    def _mark_once(self, key: str) -> bool:
        seen = self.state.setdefault("notified", [])
        if key in seen:
            return False
        seen.append(key)
        return True

    def _target_groups(self) -> list[str]:
        b = self.b
        chats: list[str] = []
        ann = (b.cfg.get("announce_to") or "").strip()
        if ann:
            try:
                chats.append(b.resolve_chat(ann))
            except Exception:
                pass
        if not chats:
            for g in (b.cfg.get("feedback_groups") or []) + (b.cfg.get("assistant_groups") or []):
                try:
                    c = b.resolve_chat(g) if not g.endswith("@chatroom") else g
                except Exception:
                    continue
                if c not in chats:
                    chats.append(c)
        return [c for c in chats if b._allowed_chat(c)]

    def _send_owner(self, text: str) -> None:
        if hasattr(self.b, "notify_owner"):
            self.b.notify_owner(text)
            return
        to = self.b.owner_chat() if hasattr(self.b, "owner_chat") else "filehelper"

        def go():
            job = self.b.send(SendJob(chat=to, text=text, source="control"))
            if not job.ok:
                log.warning("社区 PR 私聊通知没发出去（%s）：%s", to, job.error)
        threading.Thread(target=go, name="comm-owner", daemon=True).start()

    # ---- 同步插件市场仓库 ----
    def sync_plugins(self, repo: str | None = None, now: float | None = None) -> str:
        repo = (repo or self.b.cfg.get("plugins_repo") or "").strip()
        if not repo:
            return "未设置插件市场仓库"
        now = now or time.time()
        with self._lock:
            changed_repo = repo != self.state.get("plugins_repo")
            first = changed_repo or not self.state.get("plugins_synced_at")
            etag = "" if first else (self.state.get("plugins_etag") or "")
            try:
                status, headers, data = self._get_json(API.format(repo=repo), etag)
            except Exception as e:
                self.state["last_error"] = str(e)[:160]
                self._save()
                raise
            self.state["plugins_synced_at"] = now
            self.state["last_error"] = ""
            if changed_repo:
                self.state["plugins_repo"] = repo
                self.state["plugins_prs"] = {}
            if status == 304:
                self._save()
                return f"{repo}：无新变化"
            new_etag = headers.get("ETag") or headers.get("etag") or ""
            if new_etag:
                self.state["plugins_etag"] = new_etag
            prev_map: dict = self.state.setdefault("plugins_prs", {})
            owner_msgs: list[str] = []
            queued = 0
            for raw in data or []:
                num = raw.get("number")
                if not num:
                    continue
                cur = {
                    "number": num,
                    "title": raw.get("title") or "",
                    "user": (raw.get("user") or {}).get("login") or "",
                    "state": raw.get("state") or "",
                    "draft": bool(raw.get("draft")),
                    "merged_at": raw.get("merged_at"),
                    "html_url": raw.get("html_url") or "",
                    "body": (raw.get("body") or "")[:800],
                }
                prev = prev_map.get(str(num))
                prev_map[str(num)] = cur
                if first or not self.is_external(cur["user"], repo):
                    continue
                # 1) 外部新 PR（非 draft，或由 draft 转为 ready）→ 立刻私聊主人
                became_ready = prev is not None and prev.get("draft") and not cur["draft"] and cur["state"] == "open"
                is_new_open = prev is None and cur["state"] == "open" and not cur["draft"]
                if (is_new_open or became_ready) and self._mark_once(f"{repo}#{num}:opened"):
                    owner_msgs.append(self._fmt_owner_opened("plugin", repo, cur))
                # 2) 外部 PR 合并（上架）→ 私聊主人 + 入群播报队列
                if cur.get("merged_at") and not (prev or {}).get("merged_at") and self._mark_once(f"{repo}#{num}:merged"):
                    owner_msgs.append(self._fmt_owner_merged("plugin", repo, cur))
                    if self.b.cfg.get("community_notify"):
                        self._enqueue_group("plugin", repo, cur, now)
                        queued += 1
            self._save()
        if owner_msgs:
            self._send_owner("\n\n".join(owner_msgs))
        if queued:
            self.flush_group(now)
        return f"{repo}：已同步（缓存 {len(prev_map)} 个 PR，新通知 {len(owner_msgs)} 条）"

    # ---- 接入主仓库 PR 事件（由 groupwork._fuyu_events 调用） ----
    def on_main_events(self, events: list[dict], repo: str, now: float | None = None) -> None:
        if not events:
            return
        now = now or time.time()
        owner_msgs: list[str] = []
        queued = 0
        with self._lock:
            for e in events:
                t = e.get("type")
                if t not in ("pr_opened", "pr_merged"):
                    continue
                user = e.get("user") or ""
                if not self.is_external(user, repo):
                    continue
                num = e.get("number")
                cur = {
                    "number": num,
                    "title": e.get("title") or "",
                    "user": user,
                    "draft": bool(e.get("draft")),
                    "html_url": e.get("url") or f"https://github.com/{repo}/pull/{num}",
                    "body": e.get("body") or "",
                }
                if t == "pr_opened" and not cur["draft"] and self._mark_once(f"{repo}#{num}:opened"):
                    owner_msgs.append(self._fmt_owner_opened("main", repo, cur))
                elif t == "pr_merged" and self._mark_once(f"{repo}#{num}:merged"):
                    owner_msgs.append(self._fmt_owner_merged("main", repo, cur))
                    if self.b.cfg.get("community_notify"):
                        self._enqueue_group("main", repo, cur, now)
                        queued += 1
            if owner_msgs or queued:
                self._save()
        if owner_msgs:
            self._send_owner("\n\n".join(owner_msgs))
        if queued:
            self.flush_group(now)

    # ---- 文案与群播报队列 ----
    @staticmethod
    def _fmt_owner_opened(kind: str, repo: str, pr: dict) -> str:
        tag = "插件市场新 PR" if kind == "plugin" else "主仓库外部新 PR"
        summary = _clean_summary(pr.get("body") or "", 100)
        lines = [f"【{tag}】{repo} #{pr['number']}（by @{pr['user']}）", pr["title"]]
        if summary:
            lines.append(f"简介：{summary}")
        lines.append(pr["html_url"])
        where = "插件市场 " if kind == "plugin" else ""
        lines.append(f"（回复「让 mac 看下{where}#{pr['number']} PR」可让电脑上的 Claude 帮你审完私发意见）")
        return "\n".join(lines)

    @staticmethod
    def _fmt_owner_merged(kind: str, repo: str, pr: dict) -> str:
        tag = "插件已上架合并" if kind == "plugin" else "外部 PR 已合并"
        return f"【{tag}】{repo} #{pr['number']}（by @{pr['user']}）：{pr['title']}\n{pr['html_url']}"

    def _enqueue_group(self, kind: str, repo: str, pr: dict, now: float) -> None:
        q = self.state.setdefault("pending_group", [])
        key = f"{repo}#{pr['number']}"
        if any(f"{x['repo']}#{x['number']}" == key for x in q):
            return
        q.append({
            "kind": kind, "repo": repo, "number": pr["number"], "title": pr["title"],
            "user": pr["user"], "summary": _clean_summary(pr.get("body") or "", 90),
            "url": pr["html_url"], "queued_at": now,
        })

    @staticmethod
    def _render_group_announcement(items: list[dict]) -> str:
        plugins = [x for x in items if x["kind"] == "plugin"]
        mains = [x for x in items if x["kind"] != "plugin"]
        blocks: list[str] = []
        if plugins:
            lines = ["🎉 浮屿插件市场上新啦！"]
            for p in plugins:
                desc = f"\n  {p['summary']}" if p.get("summary") else ""
                lines.append(f"- 《{p['title']}》（感谢 @{p['user']} 贡献）{desc}\n  {p['url']}")
            lines.append("在浮屿「设置 → 插件市场」里刷新即可搜索安装～")
            blocks.append("\n".join(lines))
        if mains:
            lines = ["🎉 感谢社区贡献者为浮屿带来新更新！"]
            for m in mains:
                lines.append(f"- {m['title']}（PR #{m['number']}，by @{m['user']}）\n  {m['url']}")
            lines.append("已合入主分支，将在下个版本发布～")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def flush_group(self, now: float | None = None) -> int:
        now = now or time.time()
        b = self.b
        if not b.cfg.get("community_notify") or b.paused or b.in_quiet_hours():
            return 0
        if not outreach.in_window(b.cfg.get("open_hours") or "", now):
            return 0
        with self._lock:
            pending = list(self.state.get("pending_group") or [])
            if not pending:
                return 0
            # 丢弃超过 3 天的陈旧条目
            fresh = [x for x in pending if now - float(x.get("queued_at") or 0) <= 3 * 86400]
            if not fresh:
                self.state["pending_group"] = []
                self._save()
                return 0
            text = self._render_group_announcement(fresh)
            chats = self._target_groups()
            if not chats:
                return 0
            self.state["pending_group"] = []
            self._save()

        def go():
            for chat in chats:
                job = b.send(SendJob(chat=chat, text=text, source="control"))
                if job.ok:
                    log.info("社区 PR 播报已发到 %s（%d 条）", b.name_of(chat), len(fresh))
                else:
                    log.warning("社区 PR 播报没发到 %s：%s", b.name_of(chat), job.error)
        threading.Thread(target=go, name="comm-group", daemon=True).start()
        return len(fresh)

    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        repo = (self.b.cfg.get("plugins_repo") or "").strip()
        if repo and now - float(self.state.get("plugins_synced_at") or 0) >= SYNC_EVERY:
            try:
                log.info("插件市场：%s", self.sync_plugins(repo, now))
            except Exception as e:
                log.warning("插件市场同步失败：%s", e)
        self.flush_group(now)

    def brief(self) -> str:
        repo = self.state.get("plugins_repo") or self.b.cfg.get("plugins_repo") or "未设置"
        ts = self.state.get("plugins_synced_at") or 0
        when = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "未同步"
        cnt = len(self.state.get("plugins_prs") or {})
        pend = len(self.state.get("pending_group") or [])
        extra = f"，待群播报 {pend} 条" if pend else ""
        err = f"（上次出错：{self.state['last_error']}）" if self.state.get("last_error") else ""
        return f"{repo}：{cnt} 个 PR，同步于 {when}{extra}{err}"
