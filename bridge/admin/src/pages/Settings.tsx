import { useMemo, useState } from "react";
import { ChipInput, Segmented, Switch, Tabs, TextField } from "../components/controls";
import { Card, ErrorNote, Group, PageHead, Row, Skeleton } from "../components/ui";
import { api, type Setting, type SettingValue } from "../lib/api";
import { useBridgeEvent } from "../lib/bus";
import { useApi } from "../lib/useApi";
import { withOthers } from "./settingsLayout";
import "./Settings.css";

const TAB_KEY = "fuxi-admin-settings-tab";

function savedTab(): string {
  try {
    return localStorage.getItem(TAB_KEY) || "reply";
  } catch {
    return "reply";
  }
}

export function Settings({ onMenu }: { onMenu: () => void }) {
  const { data, error, reload } = useApi(api.settings);
  const [tab, setTab] = useState(savedTab);
  const [rowState, setRowState] = useState<Record<string, { saving?: boolean; error?: string; saved?: boolean }>>({});
  useBridgeEvent(["settings"], () => void reload());

  const items = data?.items ?? [];
  const byKey = useMemo(() => Object.fromEntries(items.map((i) => [i.key, i])), [items]);
  const tabs = useMemo(() => withOthers(items.map((i) => i.key)), [items]);
  const current = tabs.find((t) => t.id === tab) ?? tabs[0];

  const chooseTab = (id: string) => {
    setTab(id);
    try {
      localStorage.setItem(TAB_KEY, id);
    } catch {
      // 记不住就算了
    }
  };

  async function save(key: string, value: SettingValue) {
    setRowState((s) => ({ ...s, [key]: { saving: true } }));
    try {
      await api.saveSetting(key, value);
      await reload();
      setRowState((s) => ({ ...s, [key]: { saved: true } }));
      window.setTimeout(() => setRowState((s) => (s[key]?.saved ? { ...s, [key]: {} } : s)), 2000);
    } catch (e) {
      setRowState((s) => ({ ...s, [key]: { error: e instanceof Error ? e.message : String(e) } }));
      await reload();   // 恢复成服务端的值
    }
  }

  return (
    <div className="page">
      <PageHead title="设置" onMenu={onMenu} />
      {!data && error && (
        <Card>
          <ErrorNote message={error} onRetry={reload} />
        </Card>
      )}
      {!data && !error && <Skeleton height={260} />}
      {data && (
        <>
          <Tabs value={current.id} tabs={tabs.map((t) => ({ value: t.id, label: t.label }))} onChange={chooseTab} />
          {current.groups.map((g) => {
            const rows = g.keys.map((k) => byKey[k]).filter(Boolean) as Setting[];
            if (!rows.length) return null;
            return (
              <Group key={g.title} title={g.title}>
                <Card>
                  {rows.map((it) => (
                    <SettingRow key={it.key} item={it} state={rowState[it.key]} onSave={(v) => save(it.key, v)} />
                  ))}
                </Card>
              </Group>
            );
          })}
          <div className="settings__foot">改完立即生效，和文件传输助手里的 /设置 是同一份。</div>
        </>
      )}
    </div>
  );
}

function SettingRow({ item, state, onSave }: { item: Setting; state?: { saving?: boolean; error?: string; saved?: boolean }; onSave: (v: SettingValue) => void }) {
  const desc = (
    <>
      {item.desc}
      {state?.error && <span className="setting__error">{state.error}</span>}
      {state?.saved && <span className="setting__saved">已保存</span>}
    </>
  );
  return (
    <Row title={<>{item.label}{item.kernel && <span className="setting__kernel">内核</span>}</>} desc={desc}>
      <div className={`setting__control${state?.saving ? " is-saving" : ""}`}>
        <Control item={item} onSave={onSave} disabled={!!state?.saving} />
      </div>
    </Row>
  );
}

function Control({ item, onSave, disabled }: { item: Setting; onSave: (v: SettingValue) => void; disabled: boolean }) {
  const v = item.value;
  if (v === null) return <span className="setting__na">内核没连上</span>;
  switch (item.type) {
    case "bool":
      return <Switch label={item.label} checked={!!v} disabled={disabled} onChange={onSave} />;
    case "choice":
      return (
        <Segmented
          label={item.label}
          value={String(v)}
          disabled={disabled}
          options={Object.entries(item.choices).map(([value, label]) => ({ value, label }))}
          onChange={onSave}
        />
      );
    case "int":
      return <TextField label={item.label} numeric value={String(v)} disabled={disabled} onSave={(s) => onSave(Number(s))} />;
    case "list":
      return <ChipInput label={item.label} values={(v as string[]) ?? []} labels={item.labels} disabled={disabled} onSave={onSave} />;
    default:
      return <TextField label={item.label} value={String(v ?? "")} placeholder="空" disabled={disabled} onSave={onSave} width={170} />;
  }
}
