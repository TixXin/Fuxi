import { CheckCircle2, Search, Send as SendIcon, TriangleAlert, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Tag } from "../components/controls";
import { Button, Card, ConfirmBar, ErrorNote, Group, PageHead, Row } from "../components/ui";
import { api, type Chat } from "../lib/api";
import { colorOf, initial } from "../lib/color";
import { clock } from "../lib/format";
import { useApi } from "../lib/useApi";
import "./Send.css";

interface Done {
  at: number;
  name: string;
  text: string;
  ok: boolean;
  note: string;
}

const MAX = 2000;

export function Send({ onMenu }: { onMenu: () => void }) {
  const recent = useApi(api.recentChats);
  const [q, setQ] = useState("");
  const [found, setFound] = useState<Chat[] | null>(null);
  const [chat, setChat] = useState<Chat | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState<"" | "probe" | "send">("");
  const [probe, setProbe] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState<Done[]>([]);

  useEffect(() => {
    const needle = q.trim();
    if (!needle) return setFound(null);
    const t = window.setTimeout(() => {
      api.searchChats(needle).then((r) => setFound(r.items)).catch((e) => setError(String(e.message ?? e)));
    }, 250);
    return () => window.clearTimeout(t);
  }, [q]);

  function pick(c: Chat) {
    setChat(c);
    setQ("");
    setProbe("");
    setConfirming(false);
    setError("");
  }

  async function runProbe() {
    if (!chat) return;
    setBusy("probe");
    setError("");
    try {
      const r = await api.sendProbe(chat.chat_id);
      setProbe(`已在微信里打开「${r.name}」并核对了标题，用时 ${r.seconds} 秒，没有发送任何内容。`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }

  async function runSend() {
    if (!chat) return;
    setBusy("send");
    setError("");
    try {
      const r = await api.send(chat.chat_id, text);
      setDone((d) => [{ at: Date.now() / 1000, name: r.name, text, ok: true, note: r.verified ? "已核对发出" : "已发出，核对没通过，去微信看一眼" }, ...d].slice(0, 20));
      setText("");
      setConfirming(false);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      setDone((d) => [{ at: Date.now() / 1000, name: chat.name, text, ok: false, note: msg }, ...d].slice(0, 20));
      setConfirming(false);
    } finally {
      setBusy("");
    }
  }

  const list = found ?? (recent.data?.items ?? []).slice(0, 8);
  const avatar = (c: Chat) => <span className="chat-avatar" style={{ background: colorOf(c.name) }}>{initial(c.name)}</span>;

  return (
    <div className="page">
      <PageHead title="发送" onMenu={onMenu} />
      <Group title="发给谁">
        <Card>
          {chat ? (
            <Row
              icon={avatar(chat)}
              title={
                <>
                  {chat.name}
                  {!chat.allowed && <Tag tone="warn">不在白名单</Tag>}
                </>
              }
              desc={chat.group ? "群聊" : "私聊"}
            >
              <Button icon={<X />} onClick={() => setChat(null)}>
                换一个
              </Button>
            </Row>
          ) : (
            <>
              <div className="send__search">
                <label className="toolbar__search">
                  <Search size={14} />
                  <input className="field" placeholder="搜索联系人或群" value={q} autoFocus onChange={(e) => setQ(e.target.value)} />
                </label>
                {!found && <span className="send__hint">最近说过话的</span>}
              </div>
              {list.map((c) => (
                <button key={c.chat_id} className="send__pick" onClick={() => pick(c)}>
                  {avatar(c)}
                  <span className="send__pick-name">{c.name}</span>
                  <span className="send__pick-meta">{c.group ? "群聊" : "私聊"}</span>
                </button>
              ))}
              {found && found.length === 0 && <div className="empty-note">没找到</div>}
            </>
          )}
        </Card>
      </Group>
      <Group title="内容" meta={`${text.length} / ${MAX}`} footer="以你的微信身份发出，和浮汐自动回复走同一条发送队列。先「空跑」能确认会话打得开、找得对。">
        <Card>
          <div className="send__compose">
            <textarea
              className="field field--area"
              aria-label="要发送的内容"
              rows={5}
              maxLength={MAX}
              value={text}
              placeholder="要发的话"
              onChange={(e) => {
                setText(e.target.value);
                setConfirming(false);
              }}
            />
            {probe && (
              <div className="send__probe">
                <CheckCircle2 size={14} />
                {probe}
              </div>
            )}
            {error && <ErrorNote message={error} />}
            {!confirming && (
              <div className="send__actions">
                <Button disabled={!chat || !!busy} busy={busy === "probe"} onClick={runProbe}>
                  空跑
                </Button>
                <Button variant="primary" icon={<SendIcon />} disabled={!chat || !text.trim() || !!busy} onClick={() => setConfirming(true)}>
                  发送
                </Button>
              </div>
            )}
          </div>
          {confirming && chat && (
            <ConfirmBar
              text={`马上以你的身份发给「${chat.name}」，发出后撤不回。`}
              action="发送"
              busy={busy === "send"}
              onConfirm={runSend}
              onCancel={() => setConfirming(false)}
            />
          )}
        </Card>
      </Group>
      {done.length > 0 && (
        <Group title="刚才发的" meta="只在这个页面里记着">
          <Card>
            {done.map((d, i) => (
              <Row
                key={i}
                icon={d.ok ? <CheckCircle2 size={16} className="send__ok" /> : <TriangleAlert size={16} className="send__bad" />}
                title={`${d.name} · ${clock(d.at)}`}
                desc={`${d.text.length > 60 ? d.text.slice(0, 60) + "…" : d.text} — ${d.note}`}
              />
            ))}
          </Card>
        </Group>
      )}
    </div>
  );
}
