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
