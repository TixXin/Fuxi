// 时间、耗时的写法见 docs/design/admin.md「文案」。
export function clock(ts: number, now = Date.now() / 1000): string {
  const d = new Date(ts * 1000);
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const today = new Date(now * 1000);
  if (d.toDateString() === today.toDateString()) return hm;
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;
}

export function seconds(s: number | null | undefined): string {
  return s == null ? "—" : `${s.toFixed(1)}s`;
}

export function uptime(sec: number): string {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (h >= 48) return `${Math.floor(h / 24)} 天`;
  return h ? `${h} 小时 ${m} 分` : `${m} 分钟`;
}

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

export function clockSec(ts: number): string {
  const d = new Date(ts * 1000);
  return `${clock(ts)}:${pad(d.getSeconds())}`;
}

export function ago(ts: number | null | undefined, now = Date.now() / 1000): string {
  if (!ts) return "从未";
  const s = Math.max(0, now - ts);
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return clock(ts, now);
}
