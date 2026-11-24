import {
  BookOpen,
  Inbox,
  LayoutDashboard,
  Megaphone,
  MessagesSquare,
  Send,
  SlidersHorizontal,
  Sparkles,
  Users,
  type LucideIcon,
} from "lucide-react";

// 侧栏和页面的对应；分组、图标见 docs/design/admin.md「侧栏和页面」。
export interface NavItem {
  id: string;
  label: string;
  icon: LucideIcon;
  phase?: string;   // 还没做的页面写在哪一期做
}

export const NAV: { label: string; items: NavItem[] }[] = [
  {
    label: "",
    items: [
      { id: "overview", label: "概览", icon: LayoutDashboard },
      { id: "stream", label: "对话流", icon: MessagesSquare },
    ],
  },
  {
    label: "机器人",
    items: [
      { id: "chats", label: "会话", icon: Users },
      { id: "persona", label: "人设", icon: Sparkles, phase: "D" },
      { id: "settings", label: "设置", icon: SlidersHorizontal },
    ],
  },
  {
    label: "浮屿",
    items: [
      { id: "announce", label: "发版公告", icon: Megaphone },
      { id: "feedback", label: "群反馈", icon: Inbox },
      { id: "knowledge", label: "资料库", icon: BookOpen },
    ],
  },
  { label: "工具", items: [{ id: "send", label: "发送", icon: Send, phase: "D" }] },
];

export const ALL = NAV.flatMap((g) => g.items);
