import { seconds } from "../lib/format";

// 把桥接的耗时分段（timing.py 的 STAGES）合成 5 段给人看。
const SEGMENTS: { label: string; keys: string[]; color: string }[] = [
  { label: "等待", keys: ["discover", "settle"], color: "#8e8e93" },
  { label: "识别", keys: ["vision"], color: "#f4a6d7" },
  { label: "模型", keys: ["kernel"], color: "#74d6ff" },
  { label: "打字", keys: ["typing"], color: "#a99bff" },
  { label: "发送", keys: ["queue", "open", "paste", "verify"], color: "#35e985" },
];

export function TimingBar({ timing }: { timing: Record<string, number> }) {
  const parts = SEGMENTS.map((s) => ({ ...s, v: s.keys.reduce((a, k) => a + (timing[k] ?? 0), 0) })).filter((p) => p.v >= 0.05);
  const total = parts.reduce((a, p) => a + p.v, 0) || 1;
  return (
    <div className="timing">
      <div className="timing__bar" aria-hidden="true">
        {parts.map((p) => (
          <i key={p.label} style={{ width: `${(p.v / total) * 100}%`, background: p.color }} />
        ))}
      </div>
      <div className="timing__legend">
        <span className="timing__first">首条 {seconds(timing.first)}</span>
        {parts.map((p) => (
          <span key={p.label} style={{ ["--c" as string]: p.color }}>
            {p.label} {seconds(p.v)}
          </span>
        ))}
      </div>
    </div>
  );
}
