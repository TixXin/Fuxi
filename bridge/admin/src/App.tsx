import { useCallback, useEffect, useState } from "react";
import { Sidebar } from "./components/Sidebar";
import { api, subscribe } from "./lib/api";
import { emit } from "./lib/bus";
import { applyTheme, savedTheme, toggleTheme } from "./lib/theme";
import { useApi } from "./lib/useApi";
import { ALL } from "./nav";
import { Overview } from "./pages/Overview";
import { Announce } from "./pages/Announce";
import { Chats } from "./pages/Chats";
import { Feedback } from "./pages/Feedback";
import { Knowledge } from "./pages/Knowledge";
import { Persona } from "./pages/Persona";
import { Send } from "./pages/Send";
import { Placeholder } from "./pages/Placeholder";
import { Settings } from "./pages/Settings";
import { Stream } from "./pages/Stream";
import "./App.css";

function pageFromHash(): string {
  const id = location.hash.replace(/^#\/?/, "");
  return ALL.some((n) => n.id === id) ? id : "overview";
}

export function App() {
  const [page, setPage] = useState(pageFromHash);
  const [navOpen, setNavOpen] = useState(false);
  const [live, setLive] = useState(false);
  const overview = useApi(api.overview, 10_000);
  const { reload } = overview;

  useEffect(() => applyTheme(savedTheme()), []);

  useEffect(() => {
    const onHash = () => setPage(pageFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    let timer = 0;
    return subscribe(
      (e) => {
        emit(e);
        if (e.kind === "knowledge") return;
        window.clearTimeout(timer);   // 连着来的事件合并成一次刷新
        timer = window.setTimeout(() => void reload(), 400);
      },
      setLive,
    );
  }, [reload]);

  const navigate = useCallback((id: string) => {
    location.hash = `/${id}`;
    setNavOpen(false);
    document.querySelector(".app__main")?.scrollTo(0, 0);
  }, []);

  const openNav = useCallback(() => setNavOpen(true), []);
  const item = ALL.find((n) => n.id === page)!;
  const pending = overview.data?.todo.filter((t) => t.kind === "announce").length ?? 0;
  const offline = !!overview.error && overview.error.startsWith("连不上");

  return (
    <div className="app">
      <Sidebar
        page={page}
        badges={{ announce: pending }}
        version={overview.data?.version ?? "…"}
        open={navOpen}
        onNavigate={navigate}
        onClose={() => setNavOpen(false)}
        onToggleTheme={toggleTheme}
      />
      <main className="app__main">
        {offline && <div className="conn-bar">连不上浮汐，10 秒后重试</div>}
        {page === "overview" && <Overview {...overview} live={live} onMenu={openNav} onNavigate={navigate} />}
        {page === "stream" && <Stream live={live} onMenu={openNav} />}
        {page === "settings" && <Settings onMenu={openNav} />}
        {page === "knowledge" && <Knowledge onMenu={openNav} onNavigate={navigate} />}
        {page === "chats" && <Chats onMenu={openNav} />}
        {page === "announce" && <Announce onMenu={openNav} />}
        {page === "feedback" && <Feedback onMenu={openNav} />}
        {page === "persona" && <Persona onMenu={openNav} />}
        {page === "send" && <Send onMenu={openNav} />}
        {!["overview", "stream", "settings", "knowledge", "chats", "announce", "feedback", "persona", "send"].includes(page) && (
          <Placeholder title={item.label} phase={item.phase} onMenu={openNav} />
        )}
      </main>
    </div>
  );
}
