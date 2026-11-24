import { Bug, EyeOff, Eye, HelpCircle, Lightbulb, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Segmented, Switch, Tag } from "../components/controls";
import { Button, Card, ErrorNote, Group, PageHead, Skeleton } from "../components/ui";
import { api, type FeedbackItem } from "../lib/api";
import { useBridgeEvent } from "../lib/bus";
import { clock } from "../lib/format";
import { useApi } from "../lib/useApi";
import "./Feedback.css";

type Cat = "all" | FeedbackItem["category"];
const ICON = { bug: <Bug size={16} />, suggestion: <Lightbulb size={16} />, question: <HelpCircle size={16} /> };
const TONE: Record<string, "mint" | "warn" | "bad" | "accent" | undefined> = { new: "warn", known: "accent", fixed: "mint", answered: "mint" };

export function Feedback({ onMenu }: { onMenu: () => void }) {
  const [ignored, setIgnored] = useState(false);
  const [cat, setCat] = useState<Cat>("all");
  const [preview, setPreview] = useState<{ kind: "digest" | "group"; text: string } | null>(null);
  const [error, setError] = useState("");
  const list = useApi(() => api.feedback(ignored));
  useBridgeEvent(["feedback"], () => void list.reload());
  useEffect(() => void list.reload(), [ignored]);

  const d = list.data;
  const items = useMemo(() => (d?.items ?? []).filter((i) => (cat === "all" || i.category === cat) && (ignored || i.status !== "ignored")), [d, cat, ignored]);
  const count = (c: Cat) => (d?.items ?? []).filter((i) => (c === "all" || i.category === c) && (ignored || i.status !== "ignored")).length;

  async function run(fn: () => Promise<unknown>) {
    setError("");
    try {
      await fn();
      await list.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function showPreview(kind: "digest" | "group") {
    if (preview?.kind === kind) return setPreview(null);
    setError("");
    try {
      setPreview({ kind, text: (await api.feedbackPreview(kind)).text });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className="page">
      <PageHead
        title="群反馈"
        onMenu={onMenu}
        aside={
          <Button icon={<RefreshCw />} busy={d?.classifying} onClick={() => run(api.feedbackClassify)}>
            {d?.classifying ? "整理中" : "立即整理"}
          </Button>
        }
      />
      {!d && list.error && (
        <Card>
          <ErrorNote message={list.error} onRetry={list.reload} />
        </Card>
      )}
      {!d && !list.error && <Skeleton height={240} />}
      {d && (
        <>
          <Group
            title="概况"
            meta={d.groups.length ? `收集 ${d.groups.join("、")}` : "没设置收集反馈的群（/设置 收集反馈群）"}
            footer={`每天 ${d.digest_time || "—"} 私信你日报，${d.group_summary_time || "—"} 在群里发进度。下面两个按钮只看会发什么，不会真的发。`}
          >
            <Card>
              <div className="fb-brief">
                <span>{d.brief || "最近没有反馈"}</span>
                <span className="fb-brief__actions">
                  <Button variant={preview?.kind === "digest" ? "primary" : undefined} onClick={() => showPreview("digest")}>
                    日报预览
                  </Button>
                  <Button variant={preview?.kind === "group" ? "primary" : undefined} onClick={() => showPreview("group")}>
                    群进度预览
                  </Button>
                </span>
              </div>
              {preview && <pre className="fb-preview">{preview.text || "（现在没有可发的内容）"}</pre>}
              {d.last_error && <ErrorNote message={`上次整理出错：${d.last_error}`} />}
            </Card>
          </Group>
          {error && (
            <Card>
              <ErrorNote message={error} />
            </Card>
          )}
          <div className="toolbar fb-toolbar">
            <Segmented
              label="分类"
              value={cat}
              options={[
                { value: "all", label: `全部 ${count("all")}` },
                { value: "bug", label: `问题 ${count("bug")}` },
                { value: "suggestion", label: `建议 ${count("suggestion")}` },
                { value: "question", label: `提问 ${count("question")}` },
              ]}
              onChange={setCat}
            />
            <label className="fb-toggle">
              <Switch label="显示已忽略" checked={ignored} onChange={setIgnored} />
              显示已忽略
            </label>
          </div>
          <Group meta="最近 30 天">
            <Card>
              {items.length === 0 && <div className="empty-note">没有反馈</div>}
              {items.map((it) => (
                <Item key={it.id} it={it} onIgnore={(on) => run(() => api.feedbackIgnore(it.id, on))} />
              ))}
            </Card>
          </Group>
        </>
      )}
    </div>
  );
}

function Item({ it, onIgnore }: { it: FeedbackItem; onIgnore: (on: boolean) => void }) {
  const [open, setOpen] = useState(false);
  const off = it.status === "ignored";
  return (
    <div className={`fb${off ? " fb--off" : ""}`}>
      <span className="fb__icon">{ICON[it.category]}</span>
      <div className="fb__body">
        <button className="fb__title" onClick={() => setOpen(!open)} aria-expanded={open}>
          {it.title}
        </button>
        <div className="fb__tags">
          <Tag>{it.category_zh}</Tag>
          <Tag tone={TONE[it.status]}>{it.progress}</Tag>
          {it.issue && <Tag tone="accent">#{it.issue}</Tag>}
          {it.platform && <Tag>{it.platform}</Tag>}
          <span className="fb__meta">
            {it.count} 人次 · {it.reporters.filter(Boolean).slice(0, 3).join("、")}
            {it.reporters.length > 3 ? " 等" : ""} · {clock(it.last_seen)}
          </span>
        </div>
        {open && (
          <div className="fb__more">
            {it.detail && <p>{it.detail}</p>}
            {it.quotes.length > 0 && (
              <ul className="fb__quotes">
                {it.quotes.map((q, i) => (
                  <li key={i}>{q}</li>
                ))}
              </ul>
            )}
            {it.agent && <p className="fb__agent">{it.agent}</p>}
          </div>
        )}
      </div>
      <Button className="fb__act" icon={off ? <Eye /> : <EyeOff />} onClick={() => onIgnore(!off)}>
        {off ? "取消忽略" : "忽略"}
      </Button>
    </div>
  );
}
