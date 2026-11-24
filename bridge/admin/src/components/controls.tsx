import { Plus, X } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import "./controls.css";

export function Switch({ checked, onChange, disabled, label }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label: string }) {
  return (
    <label className="switch">
      <input type="checkbox" role="switch" aria-label={label} checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span className="switch__track" />
    </label>
  );
}

export function Segmented<T extends string>({ value, options, onChange, disabled, label }: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
  disabled?: boolean;
  label: string;
}) {
  return (
    <div className="segmented" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.value}
          role="radio"
          aria-checked={o.value === value}
          disabled={disabled}
          onClick={() => o.value !== value && onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

// 失焦或回车保存；Esc 放弃修改。外部值变了（别处改了设置）就跟着变，除非正在编辑。
export function TextField({ value, onSave, placeholder, numeric, disabled, label, width }: {
  value: string;
  onSave: (v: string) => void;
  placeholder?: string;
  numeric?: boolean;
  disabled?: boolean;
  label: string;
  width?: number;
}) {
  const [draft, setDraft] = useState(value);
  const editing = useRef(false);
  useEffect(() => {
    if (!editing.current) setDraft(value);
  }, [value]);

  const commit = () => {
    editing.current = false;
    if (draft.trim() !== value.trim()) onSave(draft.trim());
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") e.currentTarget.blur();
    if (e.key === "Escape") {
      editing.current = false;
      setDraft(value);
      e.currentTarget.blur();
    }
  };
  return (
    <input
      className={`field${numeric ? " field--num" : ""}`}
      style={width ? { width } : undefined}
      aria-label={label}
      value={draft}
      placeholder={placeholder}
      inputMode={numeric ? "numeric" : undefined}
      disabled={disabled}
      onFocus={() => (editing.current = true)}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={onKey}
    />
  );
}

export function ChipInput({ values, labels, onSave, disabled, label, placeholder }: {
  values: string[];
  labels?: Record<string, string>;
  onSave: (v: string[]) => void;
  disabled?: boolean;
  label: string;
  placeholder?: string;
}) {
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");
  const add = () => {
    const parts = draft.split(/[,，、\n]+/).map((s) => s.trim()).filter(Boolean);
    setDraft("");
    setAdding(false);
    const next = [...values, ...parts.filter((p) => !values.includes(p))];
    if (next.length !== values.length) onSave(next);
  };
  return (
    <div className="chips" aria-label={label}>
      {values.map((v) => (
        <span className="chip" key={v} title={labels?.[v] ? v : undefined}>
          {labels?.[v] && labels[v] !== v ? labels[v] : v}
          <button className="chip__x" aria-label={`移除 ${labels?.[v] ?? v}`} disabled={disabled} onClick={() => onSave(values.filter((x) => x !== v))}>
            <X size={12} />
          </button>
        </span>
      ))}
      {adding ? (
        <input
          className="field chip__input"
          autoFocus
          value={draft}
          placeholder={placeholder ?? "回车添加"}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={add}
          onKeyDown={(e) => {
            if (e.key === "Enter") add();
            if (e.key === "Escape") {
              setDraft("");
              setAdding(false);
            }
          }}
        />
      ) : (
        <button className="chip chip--add" disabled={disabled} onClick={() => setAdding(true)}>
          <Plus size={12} />
          添加
        </button>
      )}
    </div>
  );
}

export function Tabs<T extends string>({ value, tabs, onChange }: { value: T; tabs: { value: T; label: string }[]; onChange: (v: T) => void }) {
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const i = tabs.findIndex((t) => t.value === value);
    const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
    onChange(next.value);
    (e.currentTarget.querySelector(`[data-tab="${next.value}"]`) as HTMLButtonElement | null)?.focus();
  };
  return (
    <div className="tabs" role="tablist" onKeyDown={onKey}>
      {tabs.map((t) => (
        <button key={t.value} data-tab={t.value} role="tab" className="tab" aria-selected={t.value === value} tabIndex={t.value === value ? 0 : -1} onClick={() => onChange(t.value)}>
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function Tag({ tone, children }: { tone?: "accent" | "mint" | "warn" | "bad"; children: ReactNode }) {
  return <span className={`tag${tone ? ` tag--${tone}` : ""}`}>{children}</span>;
}
