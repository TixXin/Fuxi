import { FileText, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Segmented, Switch, Tag } from "../components/controls";
import { Card, ConfirmBar, ErrorNote, Group, PageHead, Row, Skeleton } from "../components/ui";
import { api, type Chat } from "../lib/api";
import { useBridgeEvent } from "../lib/bus";
import { colorOf, initial } from "../lib/color";
import { clock } from "../lib/format";
import { useApi } from "../lib/useApi";
import "./Chats.css";

type Filter = "all" | "private" | "group";

export function Chats({ onMenu }: { onMenu: () => void }) {
  const allow = useApi(api.allow);
  const recent = useApi(api.recentChats);
  const [q, setQ] = useState("");
  const [found, setFound] = useState<Chat[] | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [confirming, setConfirming] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");

  useBridgeEvent(["allow", "settings"], () => {
    void allow.reload();
    void recent.reload();
  });

  useEffect(() => {
    const needle = q.trim();
    if (!needle) return setFound(null);
    const t = window.setTimeout(() => {
      api.searchChats(needle).then((r) => setFound(r.items)).catch((e) => setError(String(e.message ?? e)));
    }, 250);
    return () => window.clearTimeout(t);
  }, [q]);

  async function change(chat: Chat, on: boolean) {
    setBusy(chat.chat_id);
    setError("");
    try {
      await api.changeAllow(on ? [chat.chat_id] : [], on ? [] : [chat.chat_id]);
      setConfirming(null);
      await Promise.all([allow.reload(), recent.reload()]);
      if (found) setFound(found.map((c) => (c.chat_id === chat.chat_id ? { ...c, allowed: on } : c)));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const keep = (c: Chat) => filter === "all" || (filter === "group") === c.group;
  const allowed = useMemo(() => (allow.data?.items ?? []).filter(keep), [allow.data, filter]);
  const others = useMemo(() => (recent.data?.items ?? []).filter((c) => !c.allowed && keep(c)), [recent.data, filter]);

  const row = (c: Chat) => (
    <div key={c.chat_id}>
      <Row
        icon={c.chat_id === "filehelper" ? <FileText size={16} /> : <span className="chat-avatar" style={{ background: colorOf(c.name) }}>{initial(c.name)}</span>}
        title={
          <>
            {c.name}
            {c.roles?.map((r) => (
              <Tag key={r} tone="mint">
                {r}
              </Tag>
            ))}
          </>
        }
        desc={[c.chat_id === "*" ? "所有会话都回" : c.group ? "群聊 · 只在被 @ 或引用时回" : c.chat_id === "filehelper" ? "遥控指令在这里发" : "私聊", c.last ? `最近 ${clock(c.last)}` : ""].filter(Boolean).join(" · ")}
      >
        <Switch
          label={`${c.allowed ? "移出" : "加入"}白名单：${c.name}`}
          checked={c.allowed}
          disabled={busy === c.chat_id}
          onChange={(on) => (on ? change(c, true) : setConfirming(c.chat_id))}
        />
      </Row>
      {confirming === c.chat_id && (
        <ConfirmBar
          text={`移出后浮汐不再回「${c.name}」的消息${c.group ? "" : "，主动搭话也会停"}。`}
          action="移出"
          busy={busy === c.chat_id}
          onConfirm={() => change(c, false)}
          onCancel={() => setConfirming(null)}
        />
      )}
    </div>
  );

  return (
    <div className="page">
      <PageHead title="会话" onMenu={onMenu} />
      <div className="toolbar">
        <label className="toolbar__search">
          <Search size={14} />
          <input className="field" placeholder="搜索联系人或群" value={q} onChange={(e) => setQ(e.target.value)} />
        </label>
        <Segmented
          label="筛选"
          value={filter}
          options={[
            { value: "all", label: "全部" },
            { value: "private", label: "私聊" },
            { value: "group", label: "群聊" },
          ]}
          onChange={setFilter}
        />
      </div>
      {error && (
        <Card>
          <ErrorNote message={error} />
        </Card>
      )}
      {found !== null ? (
        <Group title="搜索结果" meta={`${found.length} 个`}>
          <Card>{found.length ? found.filter(keep).map(row) : <div className="empty-note">没找到</div>}</Card>
        </Group>
      ) : (
        <>
          <Group title="白名单" meta={`只回这些会话 · ${allow.data?.items.length ?? "…"} 个`}>
            {!allow.data && !allow.error && <Skeleton height={120} />}
            {allow.error && (
              <Card>
                <ErrorNote message={allow.error} onRetry={allow.reload} />
              </Card>
            )}
            {allow.data && <Card>{allowed.length ? allowed.map(row) : <div className="empty-note">白名单是空的，浮汐谁都不回</div>}</Card>}
          </Group>
          <Group title="最近说过话" meta="打开开关就加进白名单">
            {!recent.data && !recent.error && <Skeleton height={160} />}
            {recent.error && (
              <Card>
                <ErrorNote message={recent.error} onRetry={recent.reload} />
              </Card>
            )}
            {recent.data && <Card>{others.length ? others.map(row) : <div className="empty-note">没有别的会话</div>}</Card>}
          </Group>
        </>
      )}
    </div>
  );
}
