import { RotateCcw, Save, Sparkles } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { ChipInput, Segmented, Switch, Tag } from "../components/controls";
import { Button, Card, ConfirmBar, ErrorNote, Group, PageHead, Row, Skeleton } from "../components/ui";
import { api, type Persona as P } from "../lib/api";
import { useBridgeEvent } from "../lib/bus";
import { useApi } from "../lib/useApi";
import "./Persona.css";

type Key = keyof P;
const TEXTS: { key: Key; label: string; rows: number; hint?: string }[] = [
  { key: "appearance", label: "外貌", rows: 2 },
  { key: "personality", label: "性格", rows: 2 },
  { key: "background", label: "经历", rows: 2 },
];
const NUMS: { key: Key; label: string; desc: string; min: number; max: number }[] = [
  { key: "privateMaxChars", label: "私聊字数上限", desc: "一次回复最多多少字", min: 40, max: 800 },
  { key: "groupMaxChars", label: "群聊字数上限", desc: "群里一般说得更短", min: 20, max: 300 },
  { key: "maxBubbles", label: "最多拆几条", desc: "一次回复最多分成几条消息发", min: 1, max: 8 },
  { key: "historyTurns", label: "私聊记几轮", desc: "回复时带上的私聊历史", min: 4, max: 400 },
  { key: "groupHistoryTurns", label: "群聊记几轮", desc: "回复时带上的群聊历史", min: 2, max: 300 },
];
const SWITCHES: { key: Key; label: string; desc: string }[] = [
  { key: "proactive", label: "主动聊", desc: "会追问、接着话题往下聊" },
  { key: "opinions", label: "有自己的观点", desc: "讨论时说观点；关掉只摆事实和选项" },
  { key: "links", label: "读链接", desc: "收到网页链接时打开看内容" },
  { key: "freeChat", label: "群里主动接话", desc: "没被 @ 也可能插话" },
  { key: "masterRoast", label: "可以顶主人", desc: "主人开玩笑时可以轻轻顶一句" },
];

export function Persona({ onMenu }: { onMenu: () => void }) {
  const info = useApi(api.persona);
  const [draft, setDraft] = useState<Partial<P>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [warnings, setWarnings] = useState<string[] | null>(null);
  const [saved, setSaved] = useState("");
  const [preset, setPreset] = useState<string | null>(null);
  useBridgeEvent(["persona"], () => void info.reload());

  const base = info.data?.persona;
  const val = <K extends Key>(k: K): P[K] => (k in draft ? (draft[k] as P[K]) : (base?.[k] as P[K]));
  const set = <K extends Key>(k: K, v: P[K]) => {
    setSaved("");
    setDraft((d) => {
      const next = { ...d, [k]: v };
      if (base && JSON.stringify(base[k]) === JSON.stringify(v)) delete next[k];
      return next;
    });
  };
  const dirty = Object.keys(draft).length;
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  async function save() {
    setBusy(true);
    setError("");
    try {
      const r = await api.savePersona(draft);
      setWarnings(r.warnings);
      setSaved(r.tokenCheck ? `已保存，人设约 ${r.tokenCheck.tokens} token。下一条回复开始生效。` : "已保存，下一条回复开始生效。");
      setDraft({});
      await info.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function apply(id: string) {
    setBusy(true);
    setError("");
    try {
      const r = await api.applyPreset(id);
      setPreset(null);
      setDraft({});
      setSaved(`已套用「${r.applied?.name || id}」。`);
      await info.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const shownWarnings = warnings ?? info.data?.warnings ?? [];
  const current = useMemo(() => info.data?.presets.find((p) => p.id === base?.sourcePreset), [info.data, base]);
  const llm = info.data?.llm;

  return (
    <div className="page persona-page">
      <PageHead title="人设" onMenu={onMenu} aside={llm?.model && <span className="persona__llm">{llm.providerLabel?.split("（")[0]} · {llm.model}</span>} />
      {!base && info.error && (
        <Card>
          <ErrorNote message={info.error} onRetry={info.reload} />
        </Card>
      )}
      {!base && !info.error && <Skeleton height={320} />}
      {base && (
        <>
          {shownWarnings.length > 0 && (
            <Card>
              <div className="persona__warn">
                {shownWarnings.map((w) => (
                  <div key={w}>{w}</div>
                ))}
              </div>
            </Card>
          )}
          <Group title="她是谁" meta={current ? `来自预设「${current.name}」，改过的以这里为准` : undefined}>
            <Card>
              <div className="pform">
                <label className="pform__field">
                  <span>名字</span>
                  <input className="field" value={val("name") ?? ""} maxLength={20} onChange={(e) => set("name", e.target.value)} />
                </label>
                <label className="pform__field pform__field--short">
                  <span>年龄</span>
                  <input className="field field--num" inputMode="numeric" value={val("age") ?? ""} onChange={(e) => set("age", Number(e.target.value.replace(/\D/g, "")) || 0)} />
                </label>
                <label className="pform__field">
                  <span>职业</span>
                  <input className="field" value={val("job") ?? ""} maxLength={30} onChange={(e) => set("job", e.target.value)} />
                </label>
                {TEXTS.map((t) => (
                  <label className="pform__field pform__field--wide" key={t.key}>
                    <span>{t.label}</span>
                    <textarea className="field field--area" rows={t.rows} value={String(val(t.key) ?? "")} onChange={(e) => set(t.key, e.target.value as never)} />
                  </label>
                ))}
              </div>
            </Card>
          </Group>
          <Group title="自定义提示词" meta={`${(val("customPrompt") ?? "").length} 字`} footer="写给模型的完整人设提示词，可以留空。保存时内核会算长度，超过上限会拒绝。">
            <Card>
              <div className="pform">
                <textarea
                  className="field field--area pform__prompt"
                  aria-label="自定义提示词"
                  rows={10}
                  value={val("customPrompt") ?? ""}
                  placeholder="比如：说话带点东北口音；不聊政治；被问到是不是机器人就打个哈哈……"
                  onChange={(e) => set("customPrompt", e.target.value)}
                />
              </div>
            </Card>
          </Group>
          <Group title="说话方式">
            <Card>
              <Row title="颜文字和表情" desc="回复里用多少表情符号">
                <Segmented
                  label="颜文字和表情"
                  value={val("emoji") || "正常"}
                  options={["关", "少", "正常", "多"].map((v) => ({ value: v, label: v }))}
                  onChange={(v) => set("emoji", v)}
                />
              </Row>
              {NUMS.map((n) => (
                <Row key={n.key} title={n.label} desc={`${n.desc}（${n.min}–${n.max}）`}>
                  <input
                    className="field field--num"
                    style={{ width: 80 }}
                    aria-label={n.label}
                    inputMode="numeric"
                    value={String(val(n.key) ?? "")}
                    onChange={(e) => set(n.key, (Number(e.target.value.replace(/\D/g, "")) || 0) as never)}
                    onBlur={(e) => set(n.key, Math.max(n.min, Math.min(n.max, Number(e.target.value) || n.min)) as never)}
                  />
                </Row>
              ))}
              {SWITCHES.map((s) => (
                <Row key={s.key} title={s.label} desc={s.desc}>
                  <Switch label={s.label} checked={Boolean(val(s.key))} onChange={(v) => set(s.key, v as never)} />
                </Row>
              ))}
              <Row title="禁言的群" desc="在这些群里一句话都不说，填群名；* 表示所有群">
                <ChipInput
                  label="禁言的群"
                  values={val("mutedGroups") ?? []}
                  labels={{ "*": "所有群" }}
                  placeholder="群名，回车添加"
                  onSave={(v) => set("mutedGroups", v)}
                />
              </Row>
            </Card>
          </Group>
          <Group title="预设" meta="套用会覆盖上面的人设" footer="预设在内核的 assets/personas 里，按攻击力从低到高排。">
            <Card>
              {info.data!.presets.map((p) => (
                <div key={p.id}>
                  <Row
                    icon={<Sparkles size={16} />}
                    title={
                      <>
                        {p.name}
                        {p.id === base.sourcePreset && <Tag tone="mint">在用</Tag>}
                      </>
                    }
                    desc={p.description ?? undefined}
                  >
                    <Button disabled={busy} onClick={() => setPreset(p.id)}>
                      套用
                    </Button>
                  </Row>
                  {preset === p.id && (
                    <ConfirmBar
                      text={`套用「${p.name}」会覆盖现在的名字、性格、提示词和说话方式${dirty ? "，没保存的改动也会丢掉" : ""}。`}
                      action="套用"
                      busy={busy}
                      onConfirm={() => apply(p.id)}
                      onCancel={() => setPreset(null)}
                    />
                  )}
                </div>
              ))}
            </Card>
          </Group>
          <div className={`savebar${dirty || error || saved ? " savebar--on" : ""}`}>
            <span className="savebar__text">{error ? <span className="savebar__err">{error}</span> : dirty ? `改了 ${dirty} 处，还没保存` : saved}</span>
            {dirty > 0 && (
              <>
                <Button icon={<RotateCcw />} disabled={busy} onClick={() => (setDraft({}), setError(""))}>
                  还原
                </Button>
                <Button variant="primary" icon={<Save />} busy={busy} onClick={save}>
                  保存
                </Button>
              </>
            )}
          </div>
        </>
      )}
    </div>
  );
}
