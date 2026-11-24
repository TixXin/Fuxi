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
  kind: "message" | "handoff" | "settings" | "status" | "knowledge" | "allow" | "announce" | "feedback" | "persona" | "sent" | "schedule" | "templates";
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

export interface SentEvent extends BridgeEvent {
  kind: "sent";
  chat: string;
  chat_name: string;
  group: boolean;
  text: string;
  ok: boolean;
  verified: boolean | null;
  error: string;
}

export type StreamItem = MessageEvent_ | HandoffEvent | SentEvent;

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

export interface FaqItem {
  id: number;
  q: string;
  a: string;
  updated?: number;
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

export interface Chat {
  chat_id: string;
  name: string;
  group: boolean;
  allowed: boolean;
  roles?: string[];
  last?: number;
}

export interface Announcement {
  key: string;
  code: string;
  chat_id: string;
  to: string;
  status: string;
  status_zh: string;
  error: string | null;
  created: number;
  updated: number;
  text: string;
  has_image: boolean;
}

export interface FeedbackItem {
  id: number;
  category: "bug" | "suggestion" | "question";
  category_zh: string;
  title: string;
  detail: string;
  platform: string;
  status: string;
  first_seen: number;
  last_seen: number;
  count: number;
  issue: number | null;
  fixed_in: string | null;
  reporters: string[];
  quotes: string[];
  progress: string;
  agent: string;
}

export interface FeedbackList {
  items: FeedbackItem[];
  brief: string;
  groups: string[];
  classifying: boolean;
  last_error: string;
  digest_time: string;
  group_summary_time: string;
}

export interface Persona {
  name: string;
  age: number;
  job: string;
  appearance: string;
  personality: string;
  background: string;
  customPrompt: string;
  emoji: string;
  privateMaxChars: number;
  groupMaxChars: number;
  maxBubbles: number;
  historyTurns: number;
  groupHistoryTurns: number;
  proactive: boolean;
  opinions: boolean;
  links: boolean;
  freeChat: boolean;
  masterRoast: boolean;
  mutedGroups: string[];
  sourcePreset: string;
}

export interface PersonaInfo {
  persona: Persona;
  warnings: string[];
  llm: { providerLabel?: string; model?: string; mock?: boolean };
  presets: { id: string; name: string; description: string | null; chars: number | null }[];
}

export interface ChatStats {
  chat_id: string;
  name: string;
  group: boolean;
  incoming: number;
  senders: number;
  active_days: number;
  turns: number;
  bubbles: number;
  unprompted: number | null;
  len_p50: number | null;
  len_p90: number | null;
  len_max: number | null;
  latency_p50: number | null;
  latency_p90: number | null;
  multi: number;
  max_run: number;
}

export interface Template {
  name: string;
  text: string;
  vars: string[];
}

export interface Scheduled {
  id: number;
  chat_id: string;
  name: string;
  text: string | null;
  template: string | null;
  at: number;
  created: number;
  status: string;
  status_zh: string;
  error: string;
  sent_at: number | null;
  verified: boolean | null;
  source: string;
  has_image: boolean;
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
  knowledgeFaqs: () => request<{ items: FaqItem[] }>("GET", "/api/knowledge/faq"),
  saveFaq: (q: string, a: string, id?: number) =>
    request<{ items: FaqItem[] }>("POST", "/api/knowledge/faq", id !== undefined ? { id, q, a } : { q, a }),
  deleteFaq: (id: number) => request<{ items: FaqItem[] }>("POST", "/api/knowledge/faq/delete", { id }),
  allow: () => request<{ items: Chat[] }>("GET", "/api/allow"),
  recentChats: () => request<{ items: Chat[] }>("GET", "/api/chats/recent"),
  searchChats: (q: string) => request<{ items: Chat[] }>("GET", `/api/chats?q=${encodeURIComponent(q)}`),
  changeAllow: (add: string[], remove: string[]) =>
    request<{ items: Chat[] }>("POST", "/api/allow", { add, remove, confirm: remove.length > 0 }),
  announce: () => request<{ pending: Announcement[]; recent: Announcement[]; target: string }>("GET", "/api/announce"),
  announceAct: (code: string, action: "publish" | "cancel") =>
    request<{ item: Announcement }>("POST", `/api/announce/${code}/${action}`, { confirm: action === "publish" }),
  feedback: (ignored: boolean) => request<FeedbackList>("GET", `/api/feedback?days=30${ignored ? "&ignored=1" : ""}`),
  feedbackIgnore: (id: number, on: boolean) => request<{ status: string }>("POST", `/api/feedback/${id}/${on ? "ignore" : "unignore"}`),
  feedbackClassify: () => request<{ started: boolean }>("POST", "/api/feedback/classify"),
  feedbackPreview: (kind: "digest" | "group") => request<{ text: string }>("GET", `/api/feedback/preview/${kind}`),
  templates: () => request<{ items: Template[] }>("GET", "/api/templates"),
  saveTemplate: (name: string, text: string) => request<{ items: Template[] }>("POST", "/api/templates", { name, text }),
  deleteTemplate: (name: string) => request<{ items: Template[] }>("POST", "/api/templates/delete", { name }),
  renderTemplate: (name: string, vars: Record<string, string>) => request<{ text: string }>("POST", "/api/templates/render", { name, vars }),
  schedule: () => request<{ pending: Scheduled[]; recent: Scheduled[] }>("GET", "/api/schedule"),
  addSchedule: (chat_id: string, text: string, at: number) =>
    request<{ item: Scheduled }>("POST", "/api/schedule", { chat_id, text, at, confirm: true }),
  cancelSchedule: (id: number) => request<{ item: Scheduled }>("POST", `/api/schedule/${id}/cancel`, {}),
  stats: (days: number) => request<{ days: number; at: number; items: ChatStats[] }>("GET", `/api/stats?days=${days}`),
  persona: () => request<PersonaInfo>("GET", "/api/persona"),
  savePersona: (patch: Partial<Persona>) =>
    request<{ warnings: string[]; tokenCheck?: { tokens: number; limit: number } }>("POST", "/api/persona", { patch }),
  applyPreset: (id: string) => request<{ applied: { id: string; name: string } }>("POST", "/api/persona/preset", { id, confirm: true }),
  sendProbe: (chat_id: string) => request<{ name: string; seconds: number }>("POST", "/api/send", { chat_id, dry_run: true }),
  send: (chat_id: string, text: string) =>
    request<{ name: string; verified: boolean | null; error: string }>("POST", "/api/send", { chat_id, text, confirm: true }),
  knowledgeSearch: (q: string, judge: boolean) =>
    request<KnowledgeSearch>("GET", `/api/knowledge/search?q=${encodeURIComponent(q)}${judge ? "&judge=1" : ""}`),
};

export function subscribe(onEvent: (e: BridgeEvent) => void, onState: (live: boolean) => void): () => void {
  const es = new EventSource("/api/events");
  const kinds = ["message", "handoff", "settings", "status", "knowledge", "allow", "announce", "feedback", "persona", "sent", "schedule", "templates"] as const;
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
