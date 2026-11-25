// 设置页的分页和分组。只列放在哪，控件由设置项的类型决定；没列到的新设置项自动进「其他」，不会漏。
export interface SettingsGroup {
  title: string;
  keys: string[];
}

export interface SettingsTab {
  id: string;
  label: string;
  groups: SettingsGroup[];
}

export const TABS: SettingsTab[] = [
  {
    id: "reply",
    label: "回复",
    groups: [
      { title: "回复范围", keys: ["reply_scope", "blocked_chats"] },
      { title: "谁和怎么回", keys: ["self_names", "owners", "quiet", "stickers"] },
      { title: "节奏", keys: ["settle_ms", "group_settle_ms", "stale_sec", "boot_catchup_min", "reply_self"] },
    ],
  },
  { id: "understand", label: "理解消息", groups: [{ title: "图片和语音", keys: ["vision", "voice"] }] },
  {
    id: "proactive",
    label: "主动与限速",
    groups: [
      { title: "主动", keys: ["care_on"] },
      { title: "限速", keys: ["rate_chat", "rate_global"] },
      { title: "群里插话", keys: ["master_always_reply", "free_chat_pct", "free_chat_cooldown", "group_max_chars"] },
      { title: "和人共用微信", keys: ["human_quiet_s", "human_max_wait_s"] },
    ],
  },
  {
    id: "fuyu",
    label: "浮屿",
    groups: [
      { title: "产品助手", keys: ["assistant_groups", "knowledge_repo", "fuyu_qa_pct", "fuyu_qa_delay", "fuyu_feedback_pct", "fuyu_chat_pct", "fuyu_chat_cooldown"] },
      { title: "群反馈", keys: ["feedback_groups", "digest_time", "group_summary_time"] },
      { title: "群营业", keys: ["outreach", "idle_hours", "open_hours", "fix_notify"] },
      { title: "浮屿仓库", keys: ["fuyu_sync", "fuyu_notify", "plugins_repo", "community_notify", "community_ignore_users", "agent_token_expires"] },
      { title: "发版公告", keys: ["announce_to"] },
    ],
  },
];

export function withOthers(allKeys: string[]): SettingsTab[] {
  const placed = new Set(TABS.flatMap((t) => t.groups.flatMap((g) => g.keys)));
  const rest = allKeys.filter((k) => !placed.has(k));
  return rest.length ? [...TABS, { id: "other", label: "其他", groups: [{ title: "其他", keys: rest }] }] : TABS;
}
