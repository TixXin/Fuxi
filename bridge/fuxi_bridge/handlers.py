"""对外 API（api.py）和管理台（admin.py）共用的处理逻辑。两边只管认证和路由，干活都在这里。"""

from __future__ import annotations

import logging
import re
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import Bridge

log = logging.getLogger("fuxi.handlers")


class HandlerError(Exception):
    def __init__(self, msg: str, code: int = 400, **extra):
        super().__init__(msg)
        self.code = code
        self.extra = extra


def version() -> str:
    return os.environ.get("FUXI_BUILD", "dev")


def health(b: "Bridge") -> dict:
    probs = b.problems()
    return {"healthy": not probs, "problems": probs, "wechat": b.last_send_ok, "halted": b.halted,
            "paused": b.paused, "open_chat": b.sender.open_stats(), "latency": b.latency.summary(),
            "version": version()}


def settings_items(b: "Bridge") -> list[dict]:
    from . import settings as st
    from .kernel import KernelError
    items = []
    for it in st.ITEMS:
        try:
            v = b.get_setting(it.key)
        except KernelError:
            v = None
        row = {"key": it.key, "label": it.label, "type": it.type, "value": v,
               "default": it.default, "desc": it.desc, "choices": it.choices,
               "kernel": bool(it.kernel), "min": it.min, "max": it.max}
        if it.type == "list" and isinstance(v, list):   # wxid / chat_id 旁边显示名字
            row["labels"] = {x: b.name_of(x) for x in v if _is_id(x)}
        items.append(row)
    return items


def _is_id(x: str) -> bool:
    return x.startswith(("wxid_", "gh_")) or x.endswith(("@chatroom", "@openim")) or x == "filehelper"


def set_settings(b: "Bridge", body: dict, who: str = "API") -> list[dict]:
    from . import settings as st
    from .kernel import KernelError
    for k in body:
        if k not in st.BY_KEY:
            raise HandlerError(f"没有设置项 {k}")
    for k, v in body.items():
        try:
            b.set_setting(k, v)
        except (ValueError, KernelError) as e:
            raise HandlerError(str(e), key=k) from None
    log.warning("设置经%s修改：%s", who, "、".join(body))
    if hasattr(b, "events"):
        b.events.publish("settings", {"keys": list(body)})
    return settings_items(b)


def resume(b: "Bridge", who: str = "API") -> str:
    was, b.halted = b.halted, ""
    log.warning("人工解除熔断（%s；之前：%s）", who, was or "未熔断")
    return was


def overview(b: "Bridge") -> dict:
    """管理台概览：总状态、三个服务、今天的数字、需要主人看一眼的事。"""
    probs = b.problems()
    now = time.time()
    if b.halted:
        state, text = "bad", "已熔断，暂停一切发送"
    elif b.paused:
        state, text = "warn", "已暂停回复"
    elif probs:
        state, text = "warn", probs[0]
    elif b.in_quiet_hours():
        state, text = "idle", "静默时段中，暂不回复"
    else:
        state, text = "ok", "正常回复中"
    window = b.sender.window_state()
    wechat = {"state": "ok" if window == "ok" and len(b.keyring.keys) > 0 else "bad",
              "text": {"ok": "已登录", "login": "待登录（需在手机上确认）", "missing": "找不到微信窗口",
                       "tray": "主窗口被隐藏到托盘"}.get(window, window),
              "detail": f"{len(b.keyring.keys)} 个库已解密"}
    kernel_ok = now - b.kernel_ok_at <= 90
    kernel = {"state": "ok" if kernel_ok else "bad", "text": "在线" if kernel_ok else "不可达",
              "detail": f"{int(now - b.kernel_ok_at)} 秒前心跳"}
    poll_ok = now - b.last_poll_ok_at <= 120
    bridge = {"state": "bad" if b.halted or not poll_ok else "ok",
              "text": "已熔断" if b.halted else ("轮询停滞" if not poll_ok else "正常"),
              "detail": b.halted or f"白名单 {len(b.allowlist.items)} 个会话"}
    lat = b.latency.summary()
    todo = []
    for it in b.announcer.pending():
        todo.append({"kind": "announce", "code": it["code"], "title": f"发版公告等确认 #{it['code']}",
                     "desc": f"发到「{it['to']}」", "created": it["created"]})
    for ev in b.events.recent(limit=5, kind="handoff"):
        if now - ev["ts"] < 24 * 3600:
            todo.append({"kind": "handoff", "title": f"群里有人问「{ev.get('question', '')[:40]}」",
                         "desc": f"资料里没找到 · {ev.get('chat_name', '')} · {ev.get('sender', '')}",
                         "created": ev["ts"]})
    return {
        "state": state, "text": text, "problems": probs,
        "paused": b.paused, "halted": b.halted, "quiet": b.quiet, "in_quiet": b.in_quiet_hours(),
        "allow_count": len(b.allowlist.items), "version": version(), "uptime": int(now - b.started),
        "services": {"wechat": wechat, "kernel": kernel, "bridge": bridge},
        "today": b.events.counts(),
        "latency": {"n": lat.get("n", 0), "first": lat.get("first"), "settle": lat.get("settle"),
                    "kernel": lat.get("kernel"), "send": round(sum(lat.get(k, 0) or 0 for k in
                                                                   ("queue", "open", "paste", "verify")), 2)
                    if lat.get("n") else None},
        "todo": todo,
    }


def pause(b: "Bridge", on: bool, who: str = "管理台") -> dict:
    """暂停 / 恢复。恢复时和遥控 /恢复 一样：顺带解除熔断、忘掉当前会话（下次重新进入核对）。"""
    was = ""
    if on:
        b.set_paused(True)
    else:
        was = resume(b, who)
        b.set_paused(False)
        b.sender.forget_chat()
    log.warning("%s：%s", who, "暂停回复" if on else "恢复回复")
    b.events.publish("status", {"paused": b.paused, "halted": b.halted})
    return {"paused": b.paused, "halted": b.halted, "was": was}


# ---- 资料库（产品助手的浮屿文档） ----
def knowledge_info(b: "Bridge") -> dict:
    k = b.knowledge
    m = k.meta
    sources: dict[str, int] = {}
    for c in k.index.chunks:
        kind = ("版本说明" if c.source.startswith("docs/releases/") else
                "功能说明" if c.source.startswith("docs/features/") else "其他")
        sources[kind] = sources.get(kind, 0) + 1
    return {"repo": m.get("repo") or b.cfg.get("knowledge_repo"), "synced_at": m.get("synced_at"),
            "files": m.get("files", 0), "fetched": m.get("fetched", 0), "chunks": len(k.index.chunks),
            "sources": sources, "error": m.get("error", ""), "failed_at": m.get("failed_at"),
            "syncing": getattr(k, "syncing", False), "enabled": bool(b.cfg.get("assistant_groups")),
            "groups": b.cfg.get("assistant_groups") or []}


def knowledge_sync(b: "Bridge") -> None:
    """后台同步一次，完成后推送 knowledge 事件；正在同步时抛错。"""
    import threading
    k = b.knowledge
    if getattr(k, "syncing", False):
        raise HandlerError("正在同步，稍等", 409)
    k.syncing = True

    def go():
        result = ""
        try:
            result = k.sync(b.cfg.get("knowledge_repo"))
        except Exception as e:
            result = f"同步失败：{e}"
        finally:
            k.syncing = False
            b.events.publish("knowledge", {"result": result})
    threading.Thread(target=go, name="knowledge-sync", daemon=True).start()


def knowledge_search(b: "Bridge", q: str, judge: bool = False) -> dict:
    """试搜：粗筛候选的分数、覆盖率，最终会附给内核的段落；judge=True 再让模型筛一遍（和群里一样）。"""
    from . import knowledge as kn
    k = b.knowledge
    q = q.strip()[:200]
    if not q:
        raise HandlerError("要搜什么？")
    cands = k.candidates(q, 6)
    picked = k.search(q)
    judged = None
    if judge and picked:
        judged = [c.title for c in b._judge(q, picked)]
    cq = kn.clean(q)
    return {"query": q, "question": kn.is_question(q), "floor": k.floor, "cover": k.cover,
            "picked": [c.title for c in picked], "judged": judged,
            "candidates": [{"score": round(s, 1), "coverage": round(k.index.coverage(cq, c), 2),
                            "source": c.source, "title": c.title, "text": c.text[:400]} for s, c in cands]}


# ---- 会话与白名单 ----
def _chat_row(b: "Bridge", cid: str, allowed: set[str], **extra) -> dict:
    return {"chat_id": cid, "name": "全部会话" if cid == "*" else b.name_of(cid),
            "group": cid.endswith("@chatroom"), "allowed": cid in allowed or "*" in allowed, **extra}


def allow_list(b: "Bridge") -> list[dict]:
    items = list(b.allowlist.items)
    roles = _roles(b)
    return [_chat_row(b, c, set(items), roles=roles.get(c, [])) for c in items]


def _roles(b: "Bridge") -> dict[str, list[str]]:
    """会话在各功能里的角色（产品助手群、反馈群、发版群），会话页上做小标签。"""
    out: dict[str, list[str]] = {}
    for key, label in (("assistant_groups", "产品助手"), ("feedback_groups", "收集反馈")):
        for name in b.cfg.get(key) or []:
            try:
                out.setdefault(b.resolve_chat(name), []).append(label)
            except Exception:
                pass
    if b.cfg.get("announce_to"):
        try:
            out.setdefault(b.resolve_chat(b.cfg.get("announce_to")), []).append("发版群")
        except Exception:
            pass
    return out


def recent_chats(b: "Bridge", limit: int = 60) -> list[dict]:
    """微信会话列表里最近有动静的（不含公众号），给会话页「最近说过话」用；不带消息摘要。"""
    allowed = set(b.allowlist.items)
    roles = _roles(b)
    out = []
    for s in b.reader.sessions(limit * 2):
        cid = s["username"]
        if not cid or cid.startswith("gh_") or cid.endswith("@openim") or "@" in cid and not cid.endswith("@chatroom"):
            continue
        out.append(_chat_row(b, cid, allowed, last=s.get("last_timestamp") or 0, roles=roles.get(cid, [])))
        if len(out) >= limit:
            break
    return out


def search_chats(b: "Bridge", q: str) -> list[dict]:
    q = q.strip()
    if not q:
        return []
    allowed = set(b.allowlist.items)
    hits = [c for c in b.reader.contacts() if not c["username"].startswith("gh_")
            and (q in c["name"] or q in c["remark"] or q == c["alias"])][:30]
    return [_chat_row(b, c["username"], allowed) for c in hits]


def allow_change(b: "Bridge", add: list, remove: list, confirm: bool = False) -> list[dict]:
    """按 chat_id 增删白名单。移出要 confirm（移出后不再回复，主动搭话也停）。"""
    def ids(xs):
        out = []
        for x in xs or []:
            x = str(x).strip()
            if not x:
                continue
            if not (x in ("*", "filehelper") or x.endswith("@chatroom") or x.startswith("wxid_") or re.fullmatch(r"[\w-]{6,}", x)):
                raise HandlerError(f"不认识的会话 ID：{x}")
            out.append(x)
        return out
    add, remove = ids(add), ids(remove)
    if remove and not confirm:
        raise HandlerError("移出白名单需要确认", 400)
    cur = list(b.allowlist.items)
    new = [i for i in cur if i not in remove] + [i for i in add if i not in cur]
    names = {i: b.name_of(i) for i in new if i != "*"}
    b.allowlist.set(new, names)
    log.warning("白名单经管理台修改：+%d -%d", len(add), len(remove))
    b.events.publish("allow", {"count": len(new)})
    return allow_list(b)


# ---- 发版公告 ----
def announce_list(b: "Bridge") -> dict:
    a = b.announcer
    pending = a.pending()
    with a._lock:
        done = sorted((i for i in a.items.values() if i["status"] != "pending"), key=lambda i: -i.get("updated", 0))[:20]

    def row(it):
        return {**a.public(it), "text": it.get("text", ""), "has_image": bool(it.get("image") and os.path.exists(it["image"]))}
    return {"pending": [row(i) for i in pending], "recent": [row(i) for i in done], "target": b.name_of(t) if (t := b.cfg.get("announce_to")) else ""}


def announce_image(b: "Bridge", code: str) -> tuple[bytes, str]:
    for it in b.announcer.pending():
        if it["code"] == code and it.get("image") and os.path.exists(it["image"]):
            with open(it["image"], "rb") as f:
                return f.read(), "image/jpeg" if it["image"].endswith(".jpg") else "image/png"
    raise HandlerError("没有这张图（公告已发出、取消或过期后图片会删掉）", 404)


def announce_act(b: "Bridge", code: str, action: str, confirm: bool = False) -> dict:
    from .announce import AnnounceError
    try:
        if action == "publish":
            if not confirm:
                raise HandlerError("发布需要确认")
            it = b.announcer.publish(code)
        elif action == "cancel":
            it = b.announcer.cancel(code)
        else:
            raise HandlerError("不支持的操作", 404)
    except AnnounceError as e:
        raise HandlerError(str(e), getattr(e, "code", 400)) from None
    log.warning("公告 #%s 经管理台%s：%s", code, "发布" if action == "publish" else "取消", it["status"])
    b.events.publish("announce", {"code": code, "status": it["status"]})
    if it["status"] == "failed":
        raise HandlerError(f"发送失败：{it.get('error') or '未知原因'}", 502)
    return b.announcer.public(it)


# ---- 群反馈 ----
def feedback_list(b: "Bridge", days: int = 30, ignored: bool = False) -> dict:
    from .feedback import CATEGORIES
    fb = b.feedback
    cut = time.time() - days * 86400
    items = [i for i in fb.items if i["last_seen"] >= cut and (ignored or i["status"] != "ignored")]
    items.sort(key=lambda i: -i["last_seen"])
    rows = []
    for it in items[:200]:
        try:
            progress = fb.progress(it)
        except Exception:
            progress = ""
        rows.append({k: it.get(k) for k in ("id", "category", "title", "detail", "platform", "status", "first_seen",
                                             "last_seen", "count", "issue", "fixed_in")}
                    | {"category_zh": CATEGORIES.get(it["category"], it["category"]),
                       "reporters": [r.get("name") or "" for r in it.get("reporters", [])],
                       "quotes": it.get("quotes", [])[-5:], "progress": progress,
                       "agent": fb._agent_line(it)})
    return {"items": rows, "brief": fb.brief(), "groups": b.cfg.get("feedback_groups") or [],
            "classifying": getattr(fb, "classifying", False), "last_error": fb.meta.get("last_error", ""),
            "digest_time": b.cfg.get("digest_time"), "group_summary_time": b.cfg.get("group_summary_time")}


def feedback_ignore(b: "Bridge", iid: int, on: bool = True) -> dict:
    fb = b.feedback
    it = fb.get(iid)
    if not it:
        raise HandlerError(f"没有 #{iid}", 404)
    if on:
        fb.ignore(iid)
    else:
        fb.set_fields(it, status="known" if it.get("issue") else "new")
    b.events.publish("feedback", {"id": iid})
    return {"id": iid, "status": it["status"]}


def feedback_classify(b: "Bridge") -> None:
    import threading
    fb = b.feedback
    if getattr(fb, "classifying", False):
        raise HandlerError("正在整理，稍等", 409)
    fb.classifying = True

    def go():
        try:
            result = fb.classify()
        except Exception as e:
            result = f"整理失败：{e}"
        finally:
            fb.classifying = False
        b.events.publish("feedback", {"result": result})
    threading.Thread(target=go, name="feedback-classify", daemon=True).start()


def feedback_preview(b: "Bridge", kind: str) -> str:
    if kind == "digest":
        return b.feedback.digest(since=time.time() - 86400)
    if kind == "group":
        return b.feedback.group_summary() or "今天还没有反馈，今晚不会发群小结。"
    raise HandlerError("只有 digest / group", 404)
