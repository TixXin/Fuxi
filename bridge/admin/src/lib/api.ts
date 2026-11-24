// 管理接口的封装：读写都带防跨站头；服务端推送用 EventSource（同源，口令由反代补上）。
export type Level = "ok" | "warn" | "bad" | "idle";

export interface Service {
  state: Level;
  text: string;
  detail: string;
}

export interface TodoItem {
  kind: "announce" | "handoff";
  title: string;
  desc: string;
  created: number;
  code?: string;
}

export interface Overview {
  state: Level;
  text: string;
  problems: string[];
  paused: boolean;
  halted: string;
  quiet: string;
  in_quiet: boolean;
  allow_count: number;
  version: string;
  uptime: number;
  services: { wechat: Service; kernel: Service; bridge: Service };
  today: { received: number; replied: number; handoffs: number; hourly: [number, number][]; since: number };
  latency: { n: number; first: number | null; settle: number | null; kernel: number | null; send: number | null };
  todo: TodoItem[];
}

export interface BridgeEvent {
  id: number;
  kind: "message" | "handoff" | "settings" | "status" | "knowledge";
  ts: number;
  [key: string]: unknown;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, headers: { Accept: "application/json" }, credentials: "same-origin" };
  if (method !== "GET") {
    init.headers = { ...init.headers, "Content-Type": "application/json", "X-Requested-With": "fuxi-admin" };
    init.body = JSON.stringify(body ?? {});
  }
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError("连不上浮汐", 0);
  }
  const data = await res.json().catch(() => ({ ok: false, error: `服务端返回了 ${res.status}` }));
  if (!res.ok || data.ok === false) throw new ApiError(data.error || `请求失败（${res.status}）`, res.status);
  return data as T;
}

export interface MessageEvent_ extends BridgeEvent {
  kind: "message";
  chat: string;
  chat_name: string;
  group: boolean;
  sender: string;
  count: number;
  kinds: string[];
  text: string;
  msg_time: number;
  replies: string[];
  reason: string;
  timing: Record<string, number> | null;
}

export interface HandoffEvent extends BridgeEvent {
  kind: "handoff";
  chat: string;
  chat_name: string;
  sender: string;
  question: string;
}

export type StreamItem = MessageEvent_ | HandoffEvent;

export type SettingType = "bool" | "int" | "text" | "list" | "choice";
export type SettingValue = boolean | number | string | string[] | null;

export interface Setting {
  key: string;
  label: string;
  type: SettingType;
  value: SettingValue;
  default: SettingValue;
  desc: string;
  choices: Record<string, string>;
  kernel: boolean;
  min: number;
  max: number;
  labels?: Record<string, string>;
}

export interface KnowledgeInfo {
  repo: string;
  synced_at: number | null;
  files: number;
  fetched: number;
  chunks: number;
  sources: Record<string, number>;
  error: string;
  failed_at: number | null;
  syncing: boolean;
  enabled: boolean;
  groups: string[];
}

export interface KnowledgeSearch {
  query: string;
  question: boolean;
  floor: number;
  cover: number;
  picked: string[];
  judged: string[] | null;
  candidates: { score: number; coverage: number; source: string; title: string; text: string }[];
}

export const api = {
  overview: () => request<Overview>("GET", "/api/overview"),
  pause: () => request<{ paused: boolean }>("POST", "/api/pause"),
  resume: () => request<{ paused: boolean; was: string }>("POST", "/api/resume"),
  messages: (before?: number, limit = 50) =>
    request<{ items: StreamItem[] }>("GET", `/api/messages?limit=${limit}${before ? `&before=${before}` : ""}`),
  settings: () => request<{ items: Setting[] }>("GET", "/api/settings"),
  saveSetting: (key: string, value: SettingValue) => request<{ items: Setting[] }>("POST", "/api/settings", { [key]: value }),
  knowledge: () => request<KnowledgeInfo>("GET", "/api/knowledge"),
  knowledgeSync: () => request<{ started: boolean }>("POST", "/api/knowledge/sync"),
  knowledgeSearch: (q: string, judge: boolean) =>
    request<KnowledgeSearch>("GET", `/api/knowledge/search?q=${encodeURIComponent(q)}${judge ? "&judge=1" : ""}`),
};

export function subscribe(onEvent: (e: BridgeEvent) => void, onState: (live: boolean) => void): () => void {
  const es = new EventSource("/api/events");
  const kinds = ["message", "handoff", "settings", "status", "knowledge"] as const;
  const handler = (ev: MessageEvent) => {
    try {
      onEvent(JSON.parse(ev.data));
    } catch {
      // 坏数据丢掉，不影响后面的事件
    }
  };
  kinds.forEach((k) => es.addEventListener(k, handler));
  es.onopen = () => onState(true);
  es.onerror = () => onState(false);   // EventSource 会按服务端给的 retry 自己重连
  return () => es.close();
}
