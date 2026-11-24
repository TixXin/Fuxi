import { SunMoon, Waves, X } from "lucide-react";
import { NAV } from "../nav";
import "./Sidebar.css";

interface Props {
  page: string;
  badges: Record<string, number>;
  version: string;
  open: boolean;
  onNavigate: (id: string) => void;
  onClose: () => void;
  onToggleTheme: () => void;
}

export function Sidebar({ page, badges, version, open, onNavigate, onClose, onToggleTheme }: Props) {
  return (
    <>
      <div className={`sidebar-scrim${open ? " is-open" : ""}`} onClick={onClose} />
      <aside className={`sidebar${open ? " is-open" : ""}`} aria-label="导航">
        <div className="sidebar__brand">
          <span className="sidebar__mark">
            <Waves size={14} strokeWidth={2.4} />
          </span>
          浮汐
          <button className="icon-button sidebar__close" onClick={onClose} aria-label="关闭导航">
            <X size={16} />
          </button>
        </div>
        <nav className="sidebar__nav">
          {NAV.map((g, gi) => (
            <div className="sidebar__group" key={gi}>
              {g.label && <div className="sidebar__label">{g.label}</div>}
              <ul>
                {g.items.map(({ id, label, icon: Icon }) => (
                  <li key={id}>
                    <button
                      className="sidebar__item"
                      aria-current={page === id ? "page" : undefined}
                      onClick={() => onNavigate(id)}
                    >
                      <Icon size={16} strokeWidth={1.9} />
                      {label}
                      {badges[id] ? <span className="sidebar__badge">{badges[id]}</span> : null}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </nav>
        <div className="sidebar__foot">
          <span className="mono">{version}</span>
          <button className="icon-button" onClick={onToggleTheme} aria-label="切换深浅色" title="切换深浅色">
            <SunMoon size={16} />
          </button>
        </div>
      </aside>
    </>
  );
}
