"""产品知识库：浮屿的说明文档切成小段，群里被 @ 提问时检索最相关的几段交给内核参考。

来源（公开仓库，默认 TixXin/Fuyu，见 /设置 资料仓库）：README.md、docs/privacy.md、docs/features/*.md、
全部正式版和比最新正式版还新的测试版的 docs/releases/v*.md（只取「更新内容」，一条一段；
正式版的说明已经包含它之前测试版的全部改动，所以旧测试版不要）。桥接每 6 小时同步一次：用 contents API
列 4 个目录（根、docs、docs/features、docs/releases）拿文件名和 sha，只下载 sha 变了的文件。文件内容按 sha 走
blobs API（raw 模式）：机房到 raw.githubusercontent.com 很慢且常超时，到 api.github.com 约 1 秒。
未登录每小时 60 次（和 F2 浮屿同步共用）：首次约 50 次，之后通常只有 4 次；中途次数用完时已下的文件留着，
10 分钟后接着下。.env 设了 FUXI_GITHUB_TOKEN 就带上。

存在 /state/knowledge/：files/<路径>@<sha> 原文（按 sha 存，中途失败下次接着下）、index.json（路径 → sha）、
meta.json（上次同步）。
版本信息（发布时间、状态）以后改读 F2 的 /state/fuyu/releases.json。

检索：中文按字的二元组、英文按词，BM25 打分。分数太低当作「资料里没有」，由调用方决定要不要转人工。
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass

log = logging.getLogger("fuxi.knowledge")

LIST = "https://api.github.com/repos/{repo}/contents/{dir}"
BLOB = "https://api.github.com/repos/{repo}/git/blobs/{sha}"
DIRS = ("", "docs", "docs/features", "docs/releases")
CHUNK = 700
SYNC_EVERY = 6 * 3600
# 问句里常见、但区分不了内容的二元组
STOP = set("怎么 什么 可以 浮屿 一下 这个 那个 能不 不能 为什 么能 么办 怎样 如何 有没 没有 请问 是不 不是 我的 你们 "
           "一个 现在 还是 就是 的话 吗？ 了吗 么样 在哪 哪里 知道 问一 问下 支持 功能 设置".split())
NOISE = re.compile(r"怎么用|怎么样|怎么办|怎么|如何|为什么|为啥|能不能|可不可以|会不会|有没有|是不是|是否|请问|一下|吗|呢|吧|啊|呀|会|要|能|的")
QUESTION = re.compile(r"[?？]|吗|怎么|如何|为什么|为啥|能不能|可不可以|有没有|哪里|在哪|怎样|咋|是否|求助|请问|什么|多久|多少|多大|哪个|哪些|几个|报错|不行|失败|打不开|闪退|没反应|崩溃|用不了")
# 群友口语问法 → 文档里的规范词；既用于 BM25 扩展召回，也用于覆盖率计算（命中任一同义词即视为覆盖该口语概念）
SYNONYMS: tuple[tuple[re.Pattern, tuple[str, ...]], ...] = (
    (re.compile(r"已损坏|损毁|打不开|无法打开|拦住|拦截|信任|验签|废纸篓"),
     ("无法验证", "仍要打开", "quarantine", "SmartScreen", "未知发布者")),
    (re.compile(r"未知发布者|报毒|杀毒|安全警告|风险提示"),
     ("SmartScreen", "未知发布者", "仍要运行", "代码签名")),
    (re.compile(r"(?:^|[^a-z])mac(?:os)?|苹果电脑|苹果", re.I),
     ("macOS", "Mac", "应用程序")),
    (re.compile(r"(?:^|[^a-z])win(?:dows|10|11)?|微软", re.I),
     ("Windows",)),
    (re.compile(r"收费|付费|免费|价格|多少钱|卖多少"),
     ("开源协议", "AGPL")),
    (re.compile(r"贴屏|钉在屏幕|固定在屏幕|置顶截图"),
     ("贴图", "截图")),
    (re.compile(r"复制记录|粘贴板|剪切板"),
     ("剪贴板", "剪贴板历史")),
    (re.compile(r"热键|按键|组合键"),
     ("快捷键",)),
    (re.compile(r"云端|上传|隐私|安全吗|本地保存|数据在哪|存在哪"),
     ("隐私", "本地", "家目录")),
    (re.compile(r"灵动岛|小岛|顶部岛|胶囊"),
     ("浮屿", "悬浮岛")),
    (re.compile(r"linux|ubuntu|debian|arch", re.I),
     ("Linux", "Windows", "macOS")),
    (re.compile(r"卸载|删掉|删除软件"),
     ("卸载", "废纸篓")),
)
MAX_FAQS = 200


def _version_key(name: str) -> tuple:
    m = re.match(r"v(\d+)\.(\d+)\.(\d+)(?:-[a-z]+\.(\d+))?\.md$", name)
    if not m:
        return ()
    a, b, c, pre = m.groups()
    return (int(a), int(b), int(c), int(pre) if pre else 10 ** 6)


def pick(paths: list[str]) -> list[str]:
    """仓库文件列表里挑出要进知识库的。"""
    out = [p for p in paths if p in ("README.md", "docs/privacy.md", "docs/faq.md", "docs/FAQ.md")
           or re.fullmatch(r"docs/features/[\w.-]+\.md", p)]
    rel = {p: _version_key(p.rsplit("/", 1)[1]) for p in paths if re.fullmatch(r"docs/releases/v[\w.-]+\.md", p)}
    rel = {p: k for p, k in rel.items() if k}
    stable = [k for k in rel.values() if k[3] == 10 ** 6]
    top = max(stable) if stable else ()
    keep = [p for p, k in rel.items() if k[3] == 10 ** 6 or k > top]
    return out + sorted(keep, key=lambda p: rel[p], reverse=True)


def latest_stable(paths: list[str]) -> str:
    rel = [(k, p) for p in paths if p.startswith("docs/releases/")
           for k in [_version_key(p.rsplit("/", 1)[1])] if k and k[3] == 10 ** 6]
    return max(rel)[1] if rel else ""


def _clean(md: str) -> str:
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    md = re.sub(r"<[^>]+>", "", md)
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)
    md = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", md)
    md = re.sub(r"\*\*([^*]+)\*\*", r"\1", md)
    md = re.sub(r"`([^`]+)`", r"\1", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


@dataclass
class Chunk:
    source: str     # 文件路径
    title: str      # 「微信提醒上岛 › 使用」「v0.3.9 更新内容」
    text: str


def chunk_file(path: str, md: str, about: bool = False) -> list[Chunk]:
    name = path.rsplit("/", 1)[-1]
    release = name if path.startswith("docs/releases/") else ""
    if release:
        # 版本说明只要「更新内容」（含修复），下载链接那些不要
        m = re.search(r"^## (更新内容|What's New)\s*$(.*?)(?=^## |\Z)", md, flags=re.M | re.S)
        if not m or m.group(1) != "更新内容":
            m = re.search(r"^## 更新内容\s*$(.*?)(?=^## |\Z)", md, flags=re.M | re.S)
            if not m:
                return []
            body = m.group(1)
        else:
            body = m.group(2)
        out = _release_items(path, release[:-3], body)
        if about:   # 最新正式版的「关于这个版本」：下载、升级、安装提示（未知发布者、Mac 仍要打开）
            a = re.search(r"^## 关于这个版本\s*$(.*?)(?=^## |\Z)", md, flags=re.M | re.S)
            if a:
                out += _split(path, f"{release[:-3]} 下载、安装和升级", _clean(a.group(1)))
        return out
    out: list[Chunk] = []
    doc_title = ""
    title, buf = "", []
    for line in md.split("\n"):
        h = re.match(r"^(#{1,3})\s+(.*)", line)
        if h:
            if buf:
                out += _split(path, title or doc_title or name, _clean("\n".join(buf)))
            buf = []
            head = _clean(h.group(2))
            if len(h.group(1)) == 1 and not doc_title:
                doc_title = head
                title = head
            elif head:
                title = f"{doc_title} › {head}" if doc_title else head
            else:
                title = doc_title or name
            continue
        buf.append(line)
    if buf:
        out += _split(path, title or doc_title or name, _clean("\n".join(buf)))
    return out


def _release_items(path: str, version: str, body: str) -> list[Chunk]:
    """版本说明：每个顶层条目一段，标题带版本号、分组和条目的加粗短语。"""
    out, group = [], ""
    for block in re.split(r"\n(?=- |#{3,4} )", "\n" + body):
        block = block.strip()
        if not block:
            continue
        h = re.match(r"#{3,4}\s+(.*)", block)
        if h:
            group = _clean(h.group(1).split("\n", 1)[0])
            rest = block.split("\n", 1)[1] if "\n" in block else ""
            if not rest.strip():
                continue
            block = rest.strip()
        lead = re.match(r"- \*\*([^*]+)\*\*", block)
        name = lead.group(1).rstrip("：:") if lead else ""
        title = " · ".join(x for x in (version, group, name) if x)
        out += _split(path, title, _clean(block))
    return out


def _split(path: str, title: str, text: str) -> list[Chunk]:
    if len(text) < 20:
        return []
    paras, out, cur = re.split(r"\n(?=- |\d+\. |\n)", text), [], ""
    for p in paras:
        p = p.strip()
        if not p:
            continue
        while len(p) > CHUNK:            # 超长段落硬切
            if cur:
                out.append(cur)
                cur = ""
            out.append(p[:CHUNK])
            p = p[CHUNK:]
        if len(cur) + len(p) + 1 > CHUNK and cur:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur}\n{p}" if cur else p
    if cur:
        out.append(cur)
    return [Chunk(path, title, t) for t in out]


def tokens(text: str) -> list[str]:
    text = text.lower()
    out = [w for w in re.findall(r"[a-z][a-z0-9+#.-]{1,}", text)]
    for run in re.findall(r"[一-鿿]+", text):
        out += [run[i:i + 2] for i in range(len(run) - 1)]
    return [t for t in out if t not in STOP]


class Index:
    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self.tf = [Counter(tokens(c.title + "\n" + c.title + "\n" + c.text)) for c in chunks]  # 标题算两遍
        self.len = [sum(t.values()) for t in self.tf]
        self.avg = (sum(self.len) / len(self.len)) if self.len else 1
        df: Counter = Counter()
        for t in self.tf:
            df.update(t.keys())
        n = len(chunks)
        self.idf = {k: math.log(1 + (n - v + 0.5) / (v + 0.5)) for k, v in df.items()}

    def coverage(self, query: str, chunk: Chunk) -> float:
        """问句里（语料中出现过的）词按 idf 加权，有多少落在这段里。跨词的二元组很少见、idf 高，
        但通常不在任何段里；按权重算比按个数算稳。"""
        q = [t for t in set(tokens(query)) if t in self.idf]
        total = sum(self.idf[t] for t in q)
        if not total:
            return 0.0
        have = set(tokens(chunk.title + "\n" + chunk.text))
        return sum(self.idf[t] for t in q if t in have) / total

    def search(self, query: str, k: int = 3) -> list[tuple[float, Chunk]]:
        q = set(tokens(query))
        if not q or not self.chunks:
            return []
        scored = []
        for i, tf in enumerate(self.tf):
            s, hit = 0.0, 0
            for t in q:
                f = tf.get(t)
                if not f:
                    continue
                hit += 1
                s += self.idf.get(t, 0) * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * self.len[i] / self.avg))
            if hit:
                scored.append((s, i))
        scored.sort(reverse=True)
        return [(s, self.chunks[i]) for s, i in scored[:k]]


class Knowledge:
    def __init__(self, root: str, repo: str = "TixXin/Fuyu"):
        self.root = root
        self.files = os.path.join(root, "files")
        self.repo = repo
        os.makedirs(self.files, mode=0o700, exist_ok=True)
        self._lock = threading.Lock()
        self.meta = self._read("meta.json", {})
        self.index = Index([])
        self._rebuild()

    def _read(self, name: str, default):
        try:
            with open(os.path.join(self.root, name)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def _write(self, name: str, obj) -> None:
        p = os.path.join(self.root, name)
        with open(p + ".tmp", "w") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(p + ".tmp", p)

    def _local(self, path: str, sha: str) -> str:
        return os.path.join(self.files, path.replace("/", "__") + "@" + sha)

    def _rebuild(self) -> None:
        chunks: list[Chunk] = []
        index = self._read("index.json", {})
        paths = sorted(index)
        latest = latest_stable(paths)
        for path in paths:
            try:
                with open(self._local(path, index[path]), encoding="utf-8") as f:
                    chunks += chunk_file(path, f.read(), about=path == latest)
            except OSError:
                continue
        for it in self._read_faqs():
            chunks.append(Chunk("local/faq", f"常见问答 · {it['q']}", f"问：{it['q']}\n答：{it['a']}"))
        self.index = Index(chunks)

    # ---- 本地自定义 FAQ（管理台维护，优先级高于功能/版本文档） ----
    def _read_faqs(self) -> list[dict]:
        if not getattr(self, "root", ""):
            return []
        raw = self._read("faq.json", [])
        if not isinstance(raw, list):
            return []
        return [it for it in raw if isinstance(it, dict) and it.get("q") and it.get("a")]

    def faq_list(self) -> list[dict]:
        with self._lock:
            items = self._read_faqs()
        return sorted(items, key=lambda i: -int(i.get("id") or 0))

    def faq_save(self, q: str, a: str, fid: int | None = None) -> list[dict]:
        q, a = str(q or "").strip(), str(a or "").strip()
        if not q or len(q) > 120:
            raise ValueError("问题不能为空，且不超过 120 字")
        if not a or len(a) > 1000:
            raise ValueError("回答不能为空，且不超过 1000 字")
        with self._lock:
            items = self._read_faqs()
            now = int(time.time())
            if fid is not None:
                hit = next((it for it in items if int(it.get("id") or 0) == int(fid)), None)
                if not hit:
                    raise ValueError(f"没有编号为 {fid} 的问答")
                hit["q"], hit["a"], hit["updated"] = q, a, now
            else:
                if len(items) >= MAX_FAQS:
                    raise ValueError(f"自定义问答最多 {MAX_FAQS} 条")
                nid = max((int(it.get("id") or 0) for it in items), default=0) + 1
                items.append({"id": nid, "q": q, "a": a, "updated": now})
            self._write("faq.json", items)
            self._rebuild()
        return self.faq_list()

    def faq_delete(self, fid: int) -> list[dict]:
        with self._lock:
            items = self._read_faqs()
            kept = [it for it in items if int(it.get("id") or 0) != int(fid)]
            if len(kept) == len(items):
                raise ValueError(f"没有编号为 {fid} 的问答")
            self._write("faq.json", kept)
            self._rebuild()
        return self.faq_list()

    def brief(self) -> str:
        n = len({c.source for c in self.index.chunks})
        at = self.meta.get("synced_at")
        when = time.strftime("%m-%d %H:%M", time.localtime(at)) if at else "从未"
        err = f"；上次失败：{self.meta['error']}" if self.meta.get("error") else ""
        return f"{n} 个文件 {len(self.index.chunks)} 段，同步于 {when}{err}"

    # ---- 同步 ----
    def _get(self, url: str, raw: bool = False, timeout: float = 30) -> bytes:
        accept = "application/vnd.github.raw" if raw else "application/vnd.github+json"
        headers = {"User-Agent": "fuxi-bridge", "Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        token = (os.environ.get("FUXI_GITHUB_TOKEN") or os.environ.get("FUXI_GITHUB_ISSUE_TOKEN", ""))
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, headers=headers)
        for attempt in range(3):   # 机房跨境读大响应常读到一半断
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = r.read(2 << 20)
                    if not raw:
                        json.loads(data)
                    return data
            except urllib.error.HTTPError as e:
                if e.code in (403, 429) and e.headers.get("X-RateLimit-Remaining") == "0":
                    reset = int(e.headers.get("X-RateLimit-Reset") or 0)
                    raise RuntimeError(f"GitHub 次数用完，{time.strftime('%H:%M', time.localtime(reset))} 后恢复") from None
                raise
            except (OSError, http.client.HTTPException, ValueError):
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
        raise AssertionError("unreachable")

    def sync(self, repo: str | None = None) -> str:
        """同步一次；返回说明。网络失败抛异常前会记进 meta.json。"""
        repo = repo or self.repo
        with self._lock:
            try:
                blobs = {}
                for d in DIRS:
                    for item in json.loads(self._get(LIST.format(repo=repo, dir=d))):
                        if item.get("type") == "file":
                            blobs[item["path"]] = item["sha"]
                want = pick(list(blobs))
                if not want:
                    raise RuntimeError("仓库里没找到文档")
                old = self._read("index.json", {}) if repo == self.meta.get("repo") else {}
                new, fetched = {}, 0
                for path in want:
                    sha = blobs[path]
                    local = self._local(path, sha)
                    if not os.path.exists(local):
                        data = self._get(BLOB.format(repo=repo, sha=sha), raw=True)
                        with open(local + ".tmp", "wb") as f:
                            f.write(data)
                        os.replace(local + ".tmp", local)
                        fetched += 1
                    new[path] = sha
                for path, sha in old.items():
                    if new.get(path) != sha:
                        try:
                            os.remove(self._local(path, sha))
                        except OSError:
                            pass
                self._write("index.json", new)
                self.meta = {"repo": repo, "synced_at": time.time(), "files": len(new), "fetched": fetched}
                self._write("meta.json", self.meta)
                self._rebuild()
                self.repo = repo
                return f"同步完成：{len(new)} 个文件（新下载 {fetched} 个），{len(self.index.chunks)} 段"
            except Exception as e:
                self.meta = {**self.meta, "error": f"{type(e).__name__}: {e}"[:200], "failed_at": time.time()}
                self._write("meta.json", self.meta)
                raise

    def loop(self, repo_of=lambda: None, enabled=lambda: True) -> None:
        while True:
            try:
                due = time.time() - float(self.meta.get("synced_at") or 0) > SYNC_EVERY
                repo = repo_of() or self.repo
                if enabled() and (due or repo != self.meta.get("repo")):
                    log.info("资料库：%s", self.sync(repo))
            except Exception as e:
                log.warning("资料库同步失败：%s", e)
            time.sleep(600)

    # ---- 检索 ----
    # 这里只粗筛、宁多勿漏：资料到底能不能回答，由桥接再让模型判断一次（Bridge._judge）。
    floor = 5.0    # BM25 分数下限
    cover = 0.5    # 至少覆盖问句一半（按 idf 加权，含同义词概念）
    boost = 1.6    # 功能说明比版本说明优先：版本说明段落多，按词频容易挤到前面
    faq_boost = 2.2  # 人工维护的常见问答优先级最高

    def _weight(self, c: Chunk) -> float:
        if c.source == "local/faq" or c.source.lower() == "docs/faq.md":
            return self.faq_boost
        return 1.0 if c.source.startswith("docs/releases/") else self.boost

    def coverage(self, query: str, chunk: Chunk) -> float:
        """问句对 chunk 的覆盖率：先按原词算，若问句命中了口语同义词组、且 chunk 命中该组任一规范词，
        也计入覆盖（避免「Mac 已损坏」因文档写的是「无法验证/仍要打开」被覆盖率误杀）。"""
        cq = clean(query)
        base = self.index.coverage(cq, chunk)
        have = set(tokens(chunk.title + "\n" + chunk.text))
        matched_spans: list[tuple[str, tuple[str, ...]]] = []
        for pat, syns in SYNONYMS:
            ms = [m.group(0) for m in pat.finditer(cq)]
            if ms:
                matched_spans.append((" ".join(ms), syns))
        if not matched_spans:
            return base
        # 把命中的同义词组对应的口语词视作「只要 chunk 命中任一规范词就算覆盖」
        syn_covered: set[str] = set()
        for span_text, syns in matched_spans:
            if any(set(tokens(s)) <= have and tokens(s) for s in syns):
                syn_covered.update(tokens(span_text))
        if not syn_covered:
            return base
        q_toks = [t for t in set(tokens(cq)) if t in self.index.idf]
        total = sum(self.index.idf[t] for t in q_toks)
        if not total:
            return base
        covered = sum(self.index.idf[t] for t in q_toks if t in have or t in syn_covered)
        return max(base, covered / total)

    def candidates(self, query: str, k: int = 4) -> list[tuple[float, Chunk]]:
        """粗筛的候选（同义词扩展 + 按来源加权、从高到低）；管理台「试搜」也用它看分数。"""
        expanded = expand(clean(query))
        hits = [(s * self._weight(c), c) for s, c in self.index.search(expanded, 40)]
        hits.sort(key=lambda h: -h[0])
        return hits[:k]

    def search(self, query: str, k: int = 4, budget: int = 1200) -> list[Chunk]:
        cands = self.candidates(query, max(k * 2, 8))
        passed = [(s, c) for s, c in cands if s >= self.floor and self.coverage(query, c) >= self.cover]
        if not passed:
            return []
        top = passed[0][0]
        out, used = [], 0
        for s, c in passed:
            if len(out) >= k or s < top * 0.45 or used + len(c.text) > budget:
                continue
            out.append(c)
            used += len(c.text)
        return out


def clean(query: str) -> str:
    """去掉问句里的「怎么、吗、能」之类：它们和前后字拼出的二元组（如「么用」）会误中标题里的「怎么用」。"""
    return NOISE.sub(" ", query)


def expand(query: str) -> str:
    """按浮屿常见口语同义词表扩展检索词，用于 BM25 召回。"""
    extra: list[str] = []
    for pat, syns in SYNONYMS:
        if pat.search(query):
            extra.extend(syns)
    return f"{query} {' '.join(dict.fromkeys(extra))}" if extra else query


def is_question(text: str) -> bool:
    return bool(QUESTION.search(text))


def render(chunks: list[Chunk]) -> str:
    return "\n\n".join(f"【{c.title}】\n{c.text}" for c in chunks)
