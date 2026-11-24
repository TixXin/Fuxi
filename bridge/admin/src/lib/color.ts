// 头像底色：按会话名取一个固定的颜色，同一个会话每次都一样。
const PALETTE = ["#74d6ff", "#a99bff", "#ffb27a", "#5eead4", "#f4a6d7", "#ffd166", "#6aabff", "#5cd68d"];

export function colorOf(name: string): string {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.codePointAt(0)!) >>> 0;
  return PALETTE[h % PALETTE.length];
}

export function initial(name: string): string {
  return [...(name || "?").trim()][0] ?? "?";
}
