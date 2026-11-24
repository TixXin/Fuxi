import { useCallback, useEffect, useRef, useState } from "react";

// 拉一次数据并定时刷新；刷新时保留旧数据不闪，出错只记下原因。
export function useApi<T>(load: () => Promise<T>, everyMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const loadRef = useRef(load);
  loadRef.current = load;

  const reload = useCallback(async () => {
    try {
      setData(await loadRef.current());
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
    if (!everyMs) return;
    const t = window.setInterval(() => {
      if (document.visibilityState === "visible") void reload();
    }, everyMs);
    return () => window.clearInterval(t);
  }, [reload, everyMs]);

  return { data, error, loading, reload };
}
