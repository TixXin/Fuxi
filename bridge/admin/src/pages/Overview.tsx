import { LifeBuoy, Megaphone, MessageCircle, Pause, Play, Reply, Timer } from "lucide-react";
import { useState } from "react";
import { Button, Card, ErrorNote, Group, PageHead, Row, Skeleton, StatusPill, Dot } from "../components/ui";
import { Rhythm } from "./Rhythm";
import { api, type Overview as Data } from "../lib/api";
import { clock, seconds, uptime } from "../lib/format";
import "./Overview.css";

interface Props {
  data: Data | null;
  error: string;
  reload: () => Promise<void>;
  live: boolean;
  onMenu: () => void;
  onNavigate: (id: string) => void;
  onAddFaq: (q: string) => void;
}

const SERVICE_NAMES = { wechat: "微信", kernel: "内核", bridge: "桥接" } as const;

export function Overview({ data, error, reload, live, onMenu, onNavigate, onAddFaq }: Props) {
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");

  async function togglePause() {
    if (!data) return;
    setBusy(true);
    setActionError("");
    try {
      await (data.paused || data.halted ? api.resume() : api.pause());
      await reload();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const head = <PageHead title="概览" onMenu={onMenu} aside={<LiveTag live={live} />} />;
  if (!data) {
    return (
      <div className="page">
        {head}
        {error ? (
          <Card>
            <ErrorNote message={error} onRetry={reload} />
          </Card>
        ) : (
          <div className="overview__skeleton">
            <Skeleton height={168} />
            <Skeleton height={104} />
          </div>
        )}
      </div>
    );
  }

  const stopped = data.paused || !!data.halted;
  const t = data.today;
  return (
    <div className="page">
      {head}
      <div className="hero">
        <div className="hero__top">
          <StatusPill level={data.state}>{data.text}</StatusPill>
          <div className="hero__title">
            <h2>{stopped ? "浮汐已停下" : `浮汐在线，已运行 ${uptime(data.uptime)}`}</h2>
            <p>
              白名单 {data.allow_count} 个会话 · 静默时段 {data.quiet || "无"}
              {data.in_quiet ? "（静默中）" : ""}
            </p>
          </div>
          <div className="hero__actions">
            <Button
              variant={stopped ? "primary" : undefined}
              icon={stopped ? <Play /> : <Pause />}
              busy={busy}
              onClick={togglePause}
            >
              {data.halted ? "解除熔断并恢复" : stopped ? "恢复回复" : "暂停回复"}
            </Button>
          </div>
        </div>
        {actionError && <div className="hero__error">{actionError}</div>}
        {data.halted && <div className="hero__error">熔断原因：{data.halted}</div>}
        <div className="hero__services">
          {(Object.keys(SERVICE_NAMES) as (keyof typeof SERVICE_NAMES)[]).map((k) => (
            <div className="service" key={k}>
              <div className="service__name">
                <Dot level={data.services[k].state} />
                {SERVICE_NAMES[k]}
                <span className="service__text">{data.services[k].text}</span>
              </div>
              <div className="service__detail mono">{data.services[k].detail}</div>
            </div>
          ))}
        </div>
      </div>

      <Group title="今天" meta={`从 ${clock(Math.max(t.since, startOfDay()))} 起 · 重启后从零数`}>
        <div className="stats">
          <Stat icon={<MessageCircle />} label="收到" value={t.received} bars={t.hourly.map((h) => h[0])} />
          <Stat icon={<Reply />} label="回复" value={t.replied} bars={t.hourly.map((h) => h[1])} />
          <Stat
            icon={<Timer />}
            label="首条回复用时"
            value={seconds(data.latency.first)}
            sub={
              data.latency.n
                ? `合并 ${seconds(data.latency.settle)} · 模型 ${seconds(data.latency.kernel)} · 发送 ${seconds(data.latency.send)}`
                : "还没有回复过"
            }
          />
          <Stat icon={<LifeBuoy />} label="转人工" value={t.handoffs} sub="产品助手答不上的" />
        </div>
      </Group>

      {data.problems.length > 0 && (
        <Group title="问题">
          <Card>
            {data.problems.map((p) => (
              <Row key={p} icon={<Dot level="bad" />} title={p} />
            ))}
          </Card>
        </Group>
      )}

      <Group title="需要你看一眼">
        <Card>
          {data.todo.length === 0 && <div className="overview__empty">没有要处理的事</div>}
          {data.todo.map((it, i) => (
            <Row
              key={i}
              icon={it.kind === "announce" ? <Megaphone size={16} /> : <LifeBuoy size={16} />}
              title={it.title}
              desc={`${it.desc} · ${clock(it.created)}`}
            >
              {it.kind === "announce" && <Button onClick={() => onNavigate("announce")}>去处理</Button>}
              {it.kind === "handoff" && it.question && <Button onClick={() => onAddFaq(it.question!)}>收入问答</Button>}
            </Row>
          ))}
        </Card>
      </Group>
      <Rhythm />
      {error && <div className="overview__stale">刷新失败：{error}（显示的是上一次的数据）</div>}
    </div>
  );
}

function Stat({ icon, label, value, sub, bars }: { icon: React.ReactNode; label: string; value: number | string; sub?: string; bars?: number[] }) {
  const max = Math.max(1, ...(bars ?? []));
  const hour = new Date().getHours();
  return (
    <div className="stat">
      <div className="stat__label">
        {icon}
        {label}
      </div>
      <div className="stat__value">{value}</div>
      {bars ? (
        <div className="stat__bars" aria-hidden="true">
          {bars.slice(0, hour + 1).map((v, i) => (
            <i key={i} style={{ height: `${Math.max(6, (v / max) * 100)}%` }} />
          ))}
        </div>
      ) : (
        <div className="stat__sub">{sub}</div>
      )}
    </div>
  );
}

function LiveTag({ live }: { live: boolean }) {
  return <span className={`live${live ? " is-live" : ""}`}>{live ? "实时" : "已断开，重连中"}</span>;
}

function startOfDay(): number {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.getTime() / 1000;
}
