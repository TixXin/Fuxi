import { Megaphone, Send, X } from "lucide-react";
import { useState } from "react";
import { Tag } from "../components/controls";
import { Button, Card, ConfirmBar, ErrorNote, Group, PageHead, Row, Skeleton } from "../components/ui";
import { api, type Announcement } from "../lib/api";
import { useBridgeEvent } from "../lib/bus";
import { clock } from "../lib/format";
import { useApi } from "../lib/useApi";
import "./Announce.css";

const TONE: Record<string, "mint" | "warn" | "bad" | undefined> = { sent: "mint", pending: "warn", failed: "bad", sending: "warn" };

export function Announce({ onMenu }: { onMenu: () => void }) {
  const list = useApi(api.announce);
  useBridgeEvent(["announce"], () => void list.reload());
  const d = list.data;
  return (
    <div className="page">
      <PageHead title="发版公告" onMenu={onMenu} />
      {!d && list.error && (
        <Card>
          <ErrorNote message={list.error} onRetry={list.reload} />
        </Card>
      )}
      {!d && !list.error && <Skeleton height={200} />}
      {d && (
        <>
          <Group title="等你确认" meta={`默认发到「${d.target || "未设置（/设置 发版群）"}」`} footer="浮屿发版时调用 POST /announce 送到这里，24 小时内不确认就过期。也可以在文件传输助手回 /发布 编号。">
            <Card>
              {d.pending.length === 0 && <div className="empty-note">没有等确认的公告</div>}
              {d.pending.map((a) => (
                <Pending key={a.code} a={a} onDone={list.reload} />
              ))}
            </Card>
          </Group>
          {d.recent.length > 0 && (
            <Group title="最近">
              <Card>
                {d.recent.map((a) => (
                  <Row
                    key={a.key}
                    icon={<Megaphone size={16} />}
                    title={
                      <>
                        {a.key}
                        <Tag tone={TONE[a.status]}>{a.status_zh}</Tag>
                      </>
                    }
                    desc={`#${a.code} → ${a.to} · ${clock(a.updated)}${a.error ? ` · ${a.error}` : ""}`}
                  />
                ))}
              </Card>
            </Group>
          )}
        </>
      )}
    </div>
  );
}

function Pending({ a, onDone }: { a: Announcement; onDone: () => Promise<void> }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function act(action: "publish" | "cancel") {
    setBusy(true);
    setError("");
    try {
      await api.announceAct(a.code, action);
      await onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="announce">
      <div className="announce__head">
        <b>{a.key}</b>
        <Tag tone="warn">#{a.code}</Tag>
        <span className="announce__meta">
          发到「{a.to}」 · {clock(a.created)} 送来 · {Math.max(0, Math.round((a.created + 86400 - Date.now() / 1000) / 3600))} 小时后过期
        </span>
      </div>
      <div className="announce__preview">
        {a.has_image && <img src={`/api/announce/${a.code}/image`} alt="公告配图" loading="lazy" />}
        {a.text && <div className="announce__text">{a.text}</div>}
      </div>
      {error && <ErrorNote message={error} />}
      {confirming ? (
        <ConfirmBar
          text={`马上发到「${a.to}」，群里所有人都会看到，发出后撤不回。`}
          action="发布"
          busy={busy}
          onConfirm={() => act("publish")}
          onCancel={() => setConfirming(false)}
        />
      ) : (
        <div className="announce__actions">
          <Button icon={<X />} busy={busy} onClick={() => act("cancel")}>
            放弃
          </Button>
          <Button variant="primary" icon={<Send />} busy={busy} onClick={() => setConfirming(true)}>
            发布
          </Button>
        </div>
      )}
    </div>
  );
}
