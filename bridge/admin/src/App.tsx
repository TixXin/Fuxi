import { Waves } from "lucide-react";

// 骨架：A 期在这里接侧栏、路由（location.hash）和各页面，见 docs/admin.md「前端」。
export function App() {
  return (
    <main className="boot">
      <span className="boot__mark">
        <Waves size={18} strokeWidth={2.4} />
      </span>
      <h1>浮汐管理台</h1>
      <p>骨架已就绪，页面按 docs/admin.md 的分期实现。</p>
    </main>
  );
}
