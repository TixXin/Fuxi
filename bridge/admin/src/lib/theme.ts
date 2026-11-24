// 深浅色：默认跟随系统；手动切换后存 localStorage（读写失败就当没存过）。
export type Theme = "light" | "dark";
const KEY = "fuxi-admin-theme";

export function savedTheme(): Theme | null {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch {
    return null;
  }
}

export function currentTheme(): Theme {
  const v = savedTheme();
  if (v) return v;
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

export function applyTheme(theme: Theme | null): void {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}

export function toggleTheme(): Theme {
  const next: Theme = currentTheme() === "dark" ? "light" : "dark";
  try {
    localStorage.setItem(KEY, next);
  } catch {
    // 存不了就只在这次生效
  }
  applyTheme(next);
  return next;
}
