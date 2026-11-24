import { useEffect, useRef, useState } from "react";
import { Segmented } from "../components/controls";
import { Card, ErrorNote, Group, Row, Skeleton } from "../components/ui";
import { api, type ChatStats } from "../lib/api";
import { colorOf, initial } from "../lib/color";
import { useApi } from "../lib/useApi";
import "./Rhythm.css";

const DAYS = [
  { value: "7", label: "7 天" },
  { value: "14", label: "14 天" },
  { value: "30", label: "30 天" },
];

export function Rhythm() {
  const [days, setDays] = useState("14");
  const stats = useApi(() => api.stats(Number(days)));

  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    void stats.reload();
  }, [days]);

  return (
    <Group
      title="回复节奏"
      meta={<Segmented label="统计天数" value={days} options={DAYS} onChange={setDays} />}
      footer="按聊天记录算，10 分钟更新一次。回得太长就去「人设」调字数上限和拆条；群里没被 @ 就开口太多，就去「设置 → 主动与限速」调插话概率和冷却。"
    >
      <Card>
        {stats.error && <ErrorNote message={stats.error} onRetry={stats.reload} />}
        {!stats.data && !stats.error && <Skeleton height={120} />}
        {stats.data?.items.length === 0 && <div className="empty-note">白名单里还没有会话</div>}
        {stats.data?.items.map((s) => <ChatRow key={s.chat_id} s={s} />)}
      </Card>
    </Group>
  );
}

function ChatRow({ s }: { s: ChatStats }) {
  const cells: [string, string][] = [
    ["收到", `${s.incoming} 条${s.group ? ` · ${s.senders} 人` : ""}`],
    ["她回", s.turns ? `${s.turns} 次 · ${s.bubbles} 条` : "没回过"],
    ["每条长度", s.len_p50 == null ? "—" : `中位 ${s.len_p50} 字 · 九成 ≤ ${s.len_p90}`],
    ["回复用时", s.latency_p50 == null ? "—" : `中位 ${s.latency_p50} 秒 · 九成 ≤ ${s.latency_p90}`],
    ["连发", s.turns ? `${s.multi} 次（最多 ${s.max_run} 条）` : "—"],
  ];
  if (s.group) cells.push(["没被 @ 就开口", s.unprompted == null ? "—" : `${s.unprompted} 次`]);
  return (
    <div className="rhythm">
      <Row
        icon={<span className="chat-avatar" style={{ background: colorOf(s.name) }}>{initial(s.name)}</span>}
        title={s.name}
        desc={`${s.group ? "群聊" : "私聊"} · 有消息的天数 ${s.active_days}`}
      />
      <dl className="rhythm__grid">
        {cells.map(([k, v]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
