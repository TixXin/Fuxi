import { Bot, LifeBuoy, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Segmented, Tag } from "../components/controls";
import { Button, Card, ErrorNote, PageHead, Skeleton } from "../components/ui";
import { api, type BridgeEvent, type MessageEvent_, type StreamItem } from "../lib/api";
import { useBridgeEvent } from "../lib/bus";
import { colorOf, initial } from "../lib/color";
import { clockSec } from "../lib/format";
import { TimingBar } from "./TimingBar";
import "./Stream.css";

type Filter = "all" | "replied" | "private" | "group";
const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "replied", label: "有回复" },
  { value: "private", label: "私聊" },
  { value: "group", label: "群聊" },
];
const KIND_ZH: Record<string, string> = { image: "图片", voice: "语音", emoji: "表情", pat: "拍一拍", link: "链接", file: "文件", video: "视频" };

export function Stream({ live, onMenu }: { live: boolean; onMenu: () => void }) {
  const [items, setItems] = useState<StreamItem[] | null>(null);
  const [error, setError] = useState("");
  const [more, setMore] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");
  const [q, setQ] = useState("");

  const load = async () => {
    try {
      const r = await api.messages(undefined, 50);
      setItems(r.items);
      setMore(r.items.length === 50);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  useEffect(() => void load(), []);

  useBridgeEvent(["message", "handoff"], (e: BridgeEvent) => {
    setItems((cur) => (cur && !cur.some((x) => x.id === e.id) ? [e as StreamItem, ...cur] : cur));
  });

  const older = async () => {
    if (!items?.length) return;
    setLoadingMore(true);
    try {
      const r = await api.messages(items[items.length - 1].id, 50);
      setItems([...items, ...r.items]);
      setMore(r.items.length === 50);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoadingMore(false);
    }
  };

  const shown = useMemo(() => {
    const needle = q.trim();
    return (items ?? []).filter((it) => {
      if (it.kind === "message") {
        if (filter === "replied" && !it.replies.length) return false;
        if (filter === "private" && it.group) return false;
        if (filter === "group" && !it.group) return false;
      } else if (filter === "private" || filter === "replied") {
        return false;
      }
      if (!needle) return true;
      const hay = it.kind === "message" ? [it.chat_name, it.sender, it.text, ...it.replies] : [it.chat_name, it.sender, it.question];
      return hay.some((s) => s?.includes(needle));
    });
  }, [items, filter, q]);

  return (
    <div className="page">
      <PageHead title="对话流" onMenu={onMenu} aside={<span className={`live${live ? " is-live" : ""}`}>{live ? "实时" : "已断开，重连中"}</span>} />
      <div className="toolbar">
        <label className="toolbar__search">
          <Search size={14} />
          <input className="field" placeholder="搜索内容或会话" value={q} onChange={(e) => setQ(e.target.value)} />
        </label>
        <Segmented label="筛选" value={filter} options={FILTERS} onChange={setFilter} />
      </div>
      {items === null && !error && <Skeleton height={300} />}
      {error && (
        <Card>
          <ErrorNote message={error} onRetry={load} />
        </Card>
      )}
      {items !== null && (
        <Card>
          {shown.length === 0 && (
            <div className="stream__empty">{items.length ? "没有符合条件的消息" : "重启以来还没有消息。白名单里的会话发来消息后会出现在这里。"}</div>
          )}
          {shown.map((it) => (it.kind === "message" ? <MessageItem key={it.id} m={it} /> : <HandoffItem key={it.id} h={it} />))}
        </Card>
      )}
      {items !== null && more && !q && filter === "all" && (
        <div className="stream__more">
          <Button busy={loadingMore} onClick={older}>
            加载更早的
          </Button>
        </div>
      )}
      <div className="stream__foot">只保留桥接重启以来最近 500 条，不落盘。</div>
    </div>
  );
}

function Avatar({ name }: { name: string }) {
  return (
    <span className="avatar" style={{ background: colorOf(name) }}>
      {initial(name)}
    </span>
  );
}

function MessageItem({ m }: { m: MessageEvent_ }) {
  const kinds = m.kinds.filter((k) => k !== "text").map((k) => KIND_ZH[k] ?? k);
  return (
    <article className="msg">
      <Avatar name={m.chat_name} />
      <div className="msg__body">
        <div className="msg__head">
          <b>{m.chat_name}</b>
          {m.group ? m.sender && <Tag>{m.sender}</Tag> : <Tag>私聊</Tag>}
          {kinds.map((k) => (
            <Tag key={k}>{k}</Tag>
          ))}
          {m.count > 1 && <Tag>合并 {m.count} 条</Tag>}
          <time className="msg__time">{clockSec(m.ts)}</time>
        </div>
        <div className="msg__in">{m.text}</div>
        {m.replies.length > 0 && (
          <div className="msg__out">
            <div className="msg__who">
              <Bot size={12} />
              浮汐{m.replies.length > 1 ? ` · ${m.replies.length} 条气泡` : ""}
            </div>
            {m.replies.map((r, i) => (
              <p key={i}>{r}</p>
            ))}
          </div>
        )}
        {m.reason && <div className={`msg__reason${m.replies.length ? " is-partial" : ""}`}>{m.replies.length ? "没回完：" : "没回："}{m.reason}</div>}
        {m.timing && <TimingBar timing={m.timing} />}
      </div>
    </article>
  );
}

function HandoffItem({ h }: { h: Extract<StreamItem, { kind: "handoff" }> }) {
  return (
    <article className="msg msg--handoff">
      <span className="avatar avatar--icon">
        <LifeBuoy size={16} />
      </span>
      <div className="msg__body">
        <div className="msg__head">
          <b>转人工</b>
          <Tag tone="warn">{h.chat_name}</Tag>
          {h.sender && <Tag>{h.sender}</Tag>}
          <time className="msg__time">{clockSec(h.ts)}</time>
        </div>
        <div className="msg__in">「{h.question}」资料里没找到，已私聊你</div>
      </div>
    </article>
  );
}
