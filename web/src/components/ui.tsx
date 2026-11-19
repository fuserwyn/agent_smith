/* Small shared building blocks: pane header, empty state, icon button. */
import type { ReactNode } from "react";

export function PaneHeader({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="pane-h">
      <span className="pane-title">{title}</span>
      <span className="pane-actions">{children}</span>
    </div>
  );
}

export function EmptyState({ icon, title, body, note }: { icon: ReactNode; title: string; body?: string; note?: string }) {
  return (
    <div className="empty">
      <div className="empty-ic" aria-hidden="true">{icon}</div>
      <div className="empty-t">{title}</div>
      {body && <p className="empty-b">{body}</p>}
      {note && <span className="empty-note">{note}</span>}
    </div>
  );
}

export function IconButton({ label, onClick, children, pressed }: { label: string; onClick?: () => void; children: ReactNode; pressed?: boolean }) {
  return (
    <button type="button" className="icon-btn" aria-label={label} title={label} onClick={onClick} aria-pressed={pressed}>
      {children}
    </button>
  );
}
