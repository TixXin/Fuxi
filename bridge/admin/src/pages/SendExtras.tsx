import { CalendarClock, FileText, Trash2, X } from "lucide-react";
import { useState } from "react";
import { Tag } from "../components/controls";
import { Button, Card, ErrorNote, Group, Row } from "../components/ui";
import { api, type Scheduled, type Template } from "../lib/api";
import { useBridgeEvent } from "../lib/bus";
import { useApi } from "../lib/useApi";

const TONE: Record<string, "mint" | "warn" | "bad" | undefined> = { pending: "warn", sent: "mint", failed: "bad", missed: "bad" };

export function when(ts: number): string {
  const d = new Date(ts * 1000);
  const today = new Date();
  const day = d.toDateString() === today.toDateString() ? "今天" : `${d.getMonth() + 1}月${d.getDate()}日`;
  return `${day} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export function Templates({ draft, onInsert }: { draft: string; onInsert: (text: string) => void }) {
  const list = useApi(api.templates);
  const [open, setOpen] = useState<string | null>(null);
  const [vars, setVars] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  useBridgeEvent(["templates"], () => void list.reload());

  async function run(fn: () => Promise<unknown>) {
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function use(t: Template) {
    if (t.vars.length && open !== t.name) {
      setOpen(t.name);
      setVars({});
      return;
    }
    await run(async () => {
      onInsert((await api.renderTemplate(t.name, vars)).text);
      setOpen(null);
    });
  }

  return (
    <Group
      title="模板"
      meta={`${list.data?.items.length ?? 0} 个`}
      footer="模板里写 {名字} 这样的变量，填入时再给值；{date} {time} {weekday} 会自动填成当前日期、时间、星期。对外 API 也能用模板发。"
    >
      <Card>
        {list.error && <ErrorNote message={list.error} onRetry={list.reload} />}
        {error && <ErrorNote message={error} />}
        {list.data?.items.map((t) => (
          <div key={t.name}>
            <Row icon={<FileText size={16} />} title={t.name} desc={t.text.length > 50 ? t.text.slice(0, 50) + "…" : t.text}>
              <Button onClick={() => use(t)}>{open === t.name ? "填好了，填入" : "填入"}</Button>
              <Button icon={<Trash2 />} aria-label={`删除模板 ${t.name}`} onClick={() => run(() => api.deleteTemplate(t.name).then(list.reload))} />
            </Row>
            {open === t.name && (
              <div className="tpl-vars">
                {t.vars.map((v) => (
                  <label key={v}>
                    <span>{v}</span>
                    <input className="field" value={vars[v] ?? ""} onChange={(e) => setVars({ ...vars, [v]: e.target.value })} />
                  </label>
                ))}
              </div>
            )}
          </div>
        ))}
        <div className="tpl-save">
          {saving ? (
            <>
              <input className="field" placeholder="模板名" value={name} autoFocus onChange={(e) => setName(e.target.value)} />
              <Button
                variant="primary"
                disabled={!name.trim() || !draft.trim()}
                onClick={() => run(() => api.saveTemplate(name.trim(), draft).then(() => (setSaving(false), setName(""), list.reload())))}
              >
                保存
              </Button>
              <Button icon={<X />} onClick={() => setSaving(false)}>
                算了
              </Button>
            </>
          ) : (
            <Button disabled={!draft.trim()} onClick={() => setSaving(true)}>
              把上面的内容存为模板
            </Button>
          )}
        </div>
      </Card>
    </Group>
  );
}

export function ScheduleList() {
  const list = useApi(api.schedule);
  const [error, setError] = useState("");
  useBridgeEvent(["schedule"], () => void list.reload());

  async function cancel(id: number) {
    setError("");
    try {
      await api.cancelSchedule(id);
      await list.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  const d = list.data;
  if (!d || (!d.pending.length && !d.recent.length)) return null;
  const row = (s: Scheduled, pending: boolean) => (
    <Row
      key={s.id}
      icon={<CalendarClock size={16} />}
      title={
        <>
          {when(s.at)} → {s.name}
          <Tag tone={TONE[s.status]}>{s.status_zh}</Tag>
          {s.source !== "管理台" && <Tag>{s.source === "api" ? "API" : s.source}</Tag>}
        </>
      }
      desc={`#${s.id} · ${s.template ? `模板「${s.template}」` : (s.text ?? "").slice(0, 60)}${s.has_image ? " +图" : ""}${s.error ? ` — ${s.error}` : ""}`}
    >
      {pending && (
        <Button icon={<X />} onClick={() => cancel(s.id)}>
          取消
        </Button>
      )}
    </Row>
  );
  return (
    <>
      {d.pending.length > 0 && (
        <Group title="等发的定时消息" meta={`${d.pending.length} 条`} footer="到点时桥接没在运行、晚了 10 分钟以上的不补发，会私聊告诉你。">
          <Card>
            {error && <ErrorNote message={error} />}
            {d.pending.map((s) => row(s, true))}
          </Card>
        </Group>
      )}
      {d.recent.length > 0 && (
        <Group title="定时消息记录">
          <Card>{d.recent.slice(0, 10).map((s) => row(s, false))}</Card>
        </Group>
      )}
    </>
  );
}
