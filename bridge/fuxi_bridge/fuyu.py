"""浮屿仓库只读同步（F2，见 docs/feedback-loop.md）：版本、issue、PR 缓存到 /state/fuyu/，供查进度、反馈查重用。

来源：公开仓库（默认 TixXin/Fuyu，与产品助手共用 /设置 资料仓库）。桥接每 30 分钟同步一次：
  releases   GET /releases：第一次分页全拉，之后只拉第一页并带 ETag（没变化时 GitHub 回 304，不占次数）
  issue / PR GET /issues?state=all&since=上次（issue 接口同时返回 PR，按 pull_request 字段分开）
未登录每小时 60 次，第一次全量约 30 次请求，之后每轮 2 次左右。部署时可在 .env 设 FUXI_GITHUB_TOKEN 提高额度
（只读、只授权浮屿仓库即可）。机房直连 GitHub API 约 2 秒，不用走代理；但大响应常读到一半断，所以分小页、断了重试。

存在 /state/fuyu/：
  releases.json  [{tag_name, name, body, published_at, html_url, prerelease}]，新的在前，不含草稿
  issues.json    [{number, title, state, state_reason, labels, user, created_at, updated_at, closed_at, html_url,
                   comments, body}]，body 截到 2000 字
  prs.json       同上，另有 merged_at
  meta.json      repo、synced_at、releases_etag、since、error
  events.json    最近 200 条变化：new_release / issue_opened / issue_closed / agent_stuck / pr_opened / pr_merged（第一次全量同步不算变化）

「修好了没」：PR 标题或正文里写了 fixes/closes/修复/关闭 #N，或带着 issue 标题里的编号（如 [B93]），
且已合并，就算修了；包含在合并之后发布的第一个版本里。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import http.client
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime

log = logging.getLogger("fuxi.fuyu")

API = "https://api.github.com/repos/{repo}"
SYNC_EVERY = 30 * 60
MAX_PAGES = 60
RELEASE_PAGE = 20   # 版本说明很长：一页 100 个约 1 MB，机房跨境链路经常读到一半断
BODY_MAX = 2000
EVENTS_MAX = 200

_CLOSE = re.compile(r"(?i)(?:\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b|修复|修了|关闭|解决)\s*[:：]?\s*#(\d+)")
_CODE = re.compile(r"\[([A-Z]{1,3}\d{1,5})\]")


def _ts(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _day(iso: str | None) -> str:
    t = _ts(iso)
    return time.strftime("%Y-%m-%d", time.localtime(t)) if t else ""


def _release(r: dict) -> dict:
    return {k: r.get(k) for k in ("tag_name", "name", "published_at", "html_url", "prerelease")} | {
        "body": (r.get("body") or "")[:BODY_MAX * 4]}


def _item(i: dict) -> dict:
    out = {k: i.get(k) for k in ("number", "title", "state", "state_reason", "created_at", "updated_at",
                                  "closed_at", "html_url", "comments")}
    out["labels"] = [lb.get("name") if isinstance(lb, dict) else lb for lb in i.get("labels") or []]
    out["user"] = (i.get("user") or {}).get("login", "")
    out["body"] = (i.get("body") or "")[:BODY_MAX]
    if "pull_request" in i:
        out["merged_at"] = (i.get("pull_request") or {}).get("merged_at")
    return out


def tokens(text: str) -> list[str]:
    """中文按字的二元组、英文按词（与 knowledge.py 同一口径）。"""
    text = (text or "").lower()
    out = re.findall(r"[a-z][a-z0-9+#.-]{1,}", text)
    for run in re.findall(r"[一-鿿]+", text):
        out += [run[i:i + 2] for i in range(len(run) - 1)]
    return out


class Fuyu:
    def __init__(self, root: str, repo: str = "TixXin/Fuyu", token: str | None = None, http=None):
        self.root = root
        self.repo = repo
        self.token = token if token is not None else (os.environ.get("FUXI_GITHUB_TOKEN") or os.environ.get("FUXI_GITHUB_ISSUE_TOKEN", ""))
        self.http = http or self._http
        os.makedirs(root, mode=0o700, exist_ok=True)
        self._lock = threading.Lock()
        self.meta: dict = self._read("meta.json", {})
        self.releases: list[dict] = self._read("releases.json", [])
        self.issues: dict[int, dict] = {i["number"]: i for i in self._read("issues.json", [])}
        self.prs: dict[int, dict] = {i["number"]: i for i in self._read("prs.json", [])}
        self.on_events = None   # 回调：同步发现变化时调用，参数为新事件列表

    # ---- 文件 ----
    def _read(self, name: str, default):
        try:
            with open(os.path.join(self.root, name), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def _write(self, name: str, obj) -> None:
        p = os.path.join(self.root, name)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(p + ".tmp", p)

    # ---- 网络 ----
    def _http(self, url: str, etag: str = "") -> tuple[int, dict, bytes]:
        headers = {"User-Agent": "fuxi-bridge", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if etag:
            headers["If-None-Match"] = etag
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, dict(r.headers), r.read(8 << 20)
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, dict(e.headers), b""
            if e.code in (403, 429) and e.headers.get("X-RateLimit-Remaining") == "0":
                reset = int(e.headers.get("X-RateLimit-Reset") or 0)
                raise RuntimeError(f"GitHub 限流，{time.strftime('%H:%M', time.localtime(reset))} 恢复") from None
            raise RuntimeError(f"GitHub 返回 {e.code}") from None

    def _get_json(self, url: str, etag: str = ""):
        # 跨境链路偶尔读到一半断开：有时抛异常，有时悄悄返回半截（解码或 JSON 解析才失败），都重试
        for attempt in range(4):
            try:
                status, headers, body = self.http(url, etag)
                if status == 304:
                    return 304, headers, None
                return status, headers, json.loads((body or b"null").decode("utf-8"))
            except (OSError, http.client.HTTPException, ValueError) as e:
                if attempt == 3:
                    raise RuntimeError(f"网络中断（{type(e).__name__}）") from None
                log.info("浮屿仓库：%s 读取不完整，%d 秒后重试", url.split("?")[0].rsplit("/", 1)[-1], 3 * (attempt + 1))
                time.sleep(3 * (attempt + 1))

    # ---- 同步 ----
    def sync(self, repo: str | None = None) -> str:
        """同步一次；返回说明。失败记进 meta.json 后抛异常。"""
        repo = repo or self.repo
        with self._lock:
            try:
                return self._sync(repo)
            except Exception as e:
                self.meta = {**self.meta, "error": f"{e}"[:200], "failed_at": time.time()}
                self._write("meta.json", self.meta)
                raise

    def _sync(self, repo: str) -> str:
        base = API.format(repo=repo)
        if repo != self.meta.get("repo"):   # 换了仓库：从头来
            self.meta, self.releases, self.issues, self.prs = {"repo": repo}, [], {}, {}
        first = not self.meta.get("synced_at")
        events: list[dict] = []
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # releases：第一次全部分页拉；之后只看第一页（新版本总在最前面），没变化时 304
        old = {r["tag_name"]: r for r in self.releases}
        status, headers, data = self._get_json(f"{base}/releases?per_page={RELEASE_PAGE}",
                                               "" if first else self.meta.get("releases_etag", ""))
        if status != 304:
            etag = headers.get("ETag") or headers.get("etag") or ""
            got = list(data or [])
            if first:
                for page in range(2, MAX_PAGES + 1):
                    if len(data or []) < RELEASE_PAGE:
                        break
                    _, _, data = self._get_json(f"{base}/releases?per_page={RELEASE_PAGE}&page={page}")
                    got += data or []
            fresh = {r["tag_name"]: _release(r) for r in got if not r.get("draft")}
            if not first:
                events += [{"type": "new_release", "tag": r["tag_name"], "prerelease": r["prerelease"],
                            "url": r["html_url"], "at": r["published_at"]} for t, r in fresh.items() if t not in old]
            merged = {**old, **fresh}
            if not first:   # 第一页里消失的（删掉或改回草稿）只可能是最近的版本：比第一页最旧的还新就去掉
                oldest = min((_ts(r["published_at"]) for r in fresh.values()), default=0)
                merged = {t: r for t, r in merged.items() if t in fresh or _ts(r["published_at"]) < oldest}
            self.releases = sorted(merged.values(), key=lambda r: _ts(r["published_at"]), reverse=True)
            self.meta["releases_etag"] = etag
            self._write("releases.json", self.releases)

        # issues + PRs（增量）
        q = {"state": "all", "sort": "updated", "direction": "asc", "per_page": "50"}
        if self.meta.get("since"):
            q["since"] = self.meta["since"]
        url = base + "/issues?" + urllib.parse.urlencode(q)
        changed = 0
        for _ in range(MAX_PAGES):
            status, headers, page = self._get_json(url)
            for raw in page or []:
                it = _item(raw)
                is_pr = "pull_request" in raw
                book = self.prs if is_pr else self.issues
                prev = book.get(it["number"])
                if not first:
                    events += self._diff(prev, it, is_pr)
                book[it["number"]] = it
                changed += 1
            nxt = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("Link") or headers.get("link") or "")
            if not nxt:
                break
            url = nxt.group(1)
        self.meta["since"] = now_iso
        self._write("issues.json", sorted(self.issues.values(), key=lambda i: -i["number"]))
        self._write("prs.json", sorted(self.prs.values(), key=lambda i: -i["number"]))

        if events:
            log_ = self._read("events.json", [])
            stamp = time.time()
            log_ = (log_ + [{**e, "seen_at": stamp} for e in events])[-EVENTS_MAX:]
            self._write("events.json", log_)
        self.meta.update({"repo": repo, "synced_at": time.time(), "error": "", "releases": len(self.releases),
                          "issues": len(self.issues), "prs": len(self.prs)})
        self._write("meta.json", self.meta)
        self.repo = repo
        if events and self.on_events:
            try:
                self.on_events(events)
            except Exception as e:   # 通知失败不影响同步
                log.warning("浮屿变化通知失败：%s", e)
        return (f"同步完成：{len(self.releases)} 个版本、{len(self.issues)} 个 issue、{len(self.prs)} 个 PR"
                f"（本次更新 {changed} 条，变化 {len(events)} 条）")

    @staticmethod
    def _diff(prev: dict | None, it: dict, is_pr: bool) -> list[dict]:
        base = {"number": it["number"], "title": it["title"], "url": it["html_url"]}
        if is_pr:
            refs = sorted({int(n) for n in _CLOSE.findall(f"{it.get('title') or ''}\n{it.get('body') or ''}")})
            if it.get("merged_at") and not (prev or {}).get("merged_at"):
                return [{"type": "pr_merged", **base, "refs": refs, "at": it["merged_at"]}]
            if prev is None and it["state"] == "open":   # 新开的 PR（含 draft）；refs = 正文里关闭关键字指向的 issue
                return [{"type": "pr_opened", **base, "refs": refs, "user": it.get("user", ""), "at": it["created_at"]}]
            return []
        if prev is None:
            out = [{"type": "issue_opened", **base, "at": it["created_at"]}]
            if "claude-stuck" in (it.get("labels") or []):
                out.append({"type": "agent_stuck", **base, "at": it.get("updated_at")})
            return out
        if "claude-stuck" in (it.get("labels") or []) and "claude-stuck" not in (prev.get("labels") or []):
            return [{"type": "agent_stuck", **base, "at": it.get("updated_at")}]
        if prev.get("state") == "open" and it["state"] == "closed":
            return [{"type": "issue_closed", **base, "reason": it.get("state_reason") or "", "at": it["closed_at"]}]
        return []

    def loop(self, repo_of=lambda: None, enabled=lambda: True) -> None:
        time.sleep(30)   # 让启动时的密钥扫描先跑
        while True:
            try:
                due = time.time() - float(self.meta.get("synced_at") or 0) > SYNC_EVERY
                repo = repo_of() or self.repo
                if enabled() and (due or repo != self.meta.get("repo")):
                    log.info("浮屿仓库：%s", self.sync(repo))
            except Exception as e:
                log.warning("浮屿仓库同步失败：%s", e)
            time.sleep(300)

    # ---- 查询 ----
    def latest(self, prerelease: bool = False) -> dict | None:
        for r in self.releases:
            if bool(r.get("prerelease")) == prerelease:
                return r
        return None

    def release_after(self, iso: str | None, stable_only: bool = True) -> dict | None:
        """在 iso 之后发布的第一个版本（找「包含这次修复的版本」）。"""
        t = _ts(iso)
        if not t:
            return None
        cands = [r for r in self.releases if _ts(r.get("published_at")) >= t
                 and (not stable_only or not r.get("prerelease"))]
        return min(cands, key=lambda r: _ts(r["published_at"])) if cands else None

    def fixes_for(self, number: int) -> list[dict]:
        """引用了这个 issue 的 PR（关闭关键字 + #N，或 issue 标题里的编号如 [B93]），按合并时间排序。"""
        issue = self.issues.get(number) or {}
        codes = set(_CODE.findall(issue.get("title") or ""))
        out = []
        for pr in self.prs.values():
            text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
            refs = {int(n) for n in _CLOSE.findall(text)}
            if number in refs or any(re.search(rf"(?<![A-Za-z0-9]){c}(?!\d)", text) for c in codes):
                out.append(pr)
        return sorted(out, key=lambda p: _ts(p.get("merged_at")) or float("inf"))

    def status(self, number: int) -> str:
        """一句话说明某个 issue / PR 的进度，给 bot 回答「修了没」或给主人看摘要。"""
        if number in self.prs:
            pr = self.prs[number]
            if pr.get("merged_at"):
                rel = self.release_after(pr["merged_at"]) or self.release_after(pr["merged_at"], False)
                where = f"，包含在 {rel['tag_name']}（{_day(rel['published_at'])} 发布）" if rel else "，还没发版"
                return f"PR #{number}「{pr['title']}」已合并（{_day(pr['merged_at'])}）{where}"
            return f"PR #{number}「{pr['title']}」" + ("未合并就关闭了" if pr["state"] == "closed" else "还在进行中")
        it = self.issues.get(number)
        if not it:
            return f"#{number} 不在缓存里"
        head = f"#{number}「{it['title']}」"
        merged = [p for p in self.fixes_for(number) if p.get("merged_at")]
        if merged:
            pr = merged[-1]
            rel = self.release_after(pr["merged_at"])
            beta = None if rel else self.release_after(pr["merged_at"], False)
            if rel:
                where = f"，包含在 {rel['tag_name']}（{_day(rel['published_at'])} 发布）"
            elif beta:
                where = f"，测试版 {beta['tag_name']} 已包含，正式版还没发"
            else:
                where = "，还没发版"
            return f"{head}已修复：PR #{pr['number']} 合并于 {_day(pr['merged_at'])}{where}"
        if it["state"] == "closed":
            reason = {"completed": "已解决关闭", "not_planned": "关闭（不计划处理）"}.get(it.get("state_reason") or "", "已关闭")
            return f"{head}{reason}（{_day(it['closed_at'])}）"
        return f"{head}还没修（{_day(it['created_at'])} 提出）"

    def find(self, text: str, k: int = 3, include_closed: bool = True) -> list[tuple[float, dict]]:
        """按标题（权重高）和正文找相关 issue，给反馈查重和回答「修了没」用。分数 0~1，调用方自己定阈值。"""
        q = Counter(tokens(text))
        if not q:
            return []
        items = [i for i in self.issues.values() if include_closed or i["state"] == "open"]
        df: Counter = Counter()
        docs = []
        for it in items:
            tt, bt = set(tokens(it["title"])), set(tokens(it.get("body") or "")[:400])
            docs.append((it, tt, bt))
            df.update(tt | bt)
        n = max(1, len(items))
        idf = {t: math.log(1 + n / (1 + df[t])) for t in q}
        total = sum(idf.values()) or 1
        scored = []
        for it, tt, bt in docs:
            s = sum(idf[t] * (1.0 if t in tt else 0.4 if t in bt else 0) for t in q) / total
            if s > 0:
                scored.append((round(s, 3), it))
        scored.sort(key=lambda x: (-x[0], -x[1]["number"]))
        return scored[:k]

    def version_brief(self) -> str:
        """「浮屿最新正式版 v0.3.13（11-24 发布）……」给内核或遥控 /浮屿 用。"""
        stable, beta = self.latest(), self.latest(prerelease=True)
        if not stable and not beta:
            return "还没有同步到浮屿的版本信息"
        parts = []
        if stable:
            parts.append(f"浮屿最新正式版 {stable['tag_name']}（{_day(stable['published_at'])} 发布，{stable['html_url']}）")
        if beta and (not stable or _ts(beta["published_at"]) > _ts(stable["published_at"])):
            parts.append(f"最新测试版 {beta['tag_name']}（{_day(beta['published_at'])}）")
        return "；".join(parts)

    def brief(self) -> str:
        at = self.meta.get("synced_at")
        when = time.strftime("%m-%d %H:%M", time.localtime(at)) if at else "从未"
        err = f"；上次失败：{self.meta['error']}" if self.meta.get("error") else ""
        open_issues = sum(1 for i in self.issues.values() if i["state"] == "open")
        latest = self.latest()
        ver = f"，最新正式版 {latest['tag_name']}" if latest else ""
        return (f"{len(self.releases)} 个版本{ver}，issue {len(self.issues)}（未关闭 {open_issues}），"
                f"PR {len(self.prs)}，同步于 {when}{err}")

    def recent_events(self, n: int = 10) -> list[dict]:
        return self._read("events.json", [])[-n:]


def render_event(e: dict) -> str:
    t = e.get("type")
    if t == "new_release":
        return f"发布了{'测试版' if e.get('prerelease') else '正式版'} {e['tag']}"
    if t == "agent_stuck":
        return f"代理没修成 issue #{e['number']}：{e['title']}"
    if t == "pr_opened":
        return f"新 PR #{e['number']}：{e['title']}"
    if t == "pr_merged":
        return f"合并 PR #{e['number']}：{e['title']}"
    if t == "issue_closed":
        return f"关闭 issue #{e['number']}：{e['title']}"
    if t == "issue_opened":
        return f"新 issue #{e['number']}：{e['title']}"
    return str(e)
