import { Menu, TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";
import type { Level } from "../lib/api";
import "./ui.css";

export function Dot({ level }: { level: Level }) {
  return <span className={`dot dot--${level}`} aria-hidden="true" />;
}

export function StatusPill({ level, children }: { level: Level; children: ReactNode }) {
  return (
    <span className="status-pill">
      <Dot level={level} />
      {children}
    </span>
  );
}

export function PageHead({ title, onMenu, aside }: { title: string; onMenu: () => void; aside?: ReactNode }) {
  return (
    <div className="page-head">
      <button className="icon-button page-head__menu" onClick={onMenu} aria-label="打开导航">
        <Menu size={16} />
      </button>
      <h1>{title}</h1>
      <span className="page-head__spacer" />
      {aside}
    </div>
  );
}

export function Group({ title, meta, footer, children }: { title?: string; meta?: ReactNode; footer?: ReactNode; children: ReactNode }) {
  return (
    <section className="group">
      {(title || meta) && (
        <div className="group__head">
          {title && <h3 className="group__title">{title}</h3>}
          {meta && <span className="group__meta">{meta}</span>}
        </div>
      )}
      {children}
      {footer && <div className="group__foot">{footer}</div>}
    </section>
  );
}

export function Card({ children }: { children: ReactNode }) {
  return <div className="card">{children}</div>;
}

export function Row({ icon, title, desc, children }: { icon?: ReactNode; title: ReactNode; desc?: ReactNode; children?: ReactNode }) {
  return (
    <div className="row">
      {icon && <span className="row__icon">{icon}</span>}
      <div className="row__text">
        <div className="row__title">{title}</div>
        {desc && <div className="row__desc">{desc}</div>}
      </div>
      {children}
    </div>
  );
}

type ButtonProps = { variant?: "primary" | "danger"; icon?: ReactNode; busy?: boolean } & React.ButtonHTMLAttributes<HTMLButtonElement>;

export function Button({ variant, icon, busy, children, className, ...rest }: ButtonProps) {
  return (
    <button className={`button${variant ? ` button--${variant}` : ""}${className ? ` ${className}` : ""}`} disabled={busy || rest.disabled} {...rest}>
      {icon}
      {children}
    </button>
  );
}

export function ErrorNote({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="error-note" role="alert">
      <span>{message}</span>
      {onRetry && (
        <button className="error-note__retry" onClick={onRetry}>
          重试
        </button>
      )}
    </div>
  );
}

export function Skeleton({ height = 52 }: { height?: number }) {
  return <div className="skeleton" style={{ height }} />;
}

// 危险操作的确认条：在那一行下面展开，说清后果，再点一次才执行（docs/design/admin.md「组件」）。
export function ConfirmBar({ text, action, busy, onConfirm, onCancel }: { text: ReactNode; action: string; busy?: boolean; onConfirm: () => void; onCancel: () => void }) {
  return (
    <div className="confirm" role="alertdialog">
      <TriangleAlert size={16} className="confirm__icon" />
      <span className="confirm__text">{text}</span>
      <Button onClick={onCancel} disabled={busy}>
        取消
      </Button>
      <Button variant="danger" busy={busy} onClick={onConfirm} autoFocus>
        {action}
      </Button>
    </div>
  );
}
