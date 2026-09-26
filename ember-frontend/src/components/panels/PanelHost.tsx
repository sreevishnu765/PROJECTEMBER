import { X } from "lucide-react";
import type { ReactNode } from "react";

interface PanelHostProps {
  title: string;
  onClose: () => void;
  children: ReactNode;
}

// Every sidebar item (other than New Conversation) opens through this —
// one consistent modal shell instead of a different layout per panel, so
// the app doesn't accumulate a pile of one-off screens.
export function PanelHost({ title, onClose, children }: PanelHostProps) {
  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/50 px-4" onClick={onClose}>
      <div
        className="flex max-h-[80vh] w-full max-w-lg flex-col rounded-2xl border border-border bg-surface p-5 animate-riseIn"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-[14.5px] font-medium text-textPrimary">{title}</h2>
          <button
            onClick={onClose}
            className="rounded-md p-1 text-textSecondary hover:bg-surfaceRaised hover:text-textPrimary"
            aria-label="Close"
          >
            <X size={16} />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto text-[13.5px] text-textSecondary">{children}</div>
      </div>
    </div>
  );
}

/** Shared "not built yet" body — honest placeholder, not fake data. */
export function PlaceholderNotice({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-border px-3.5 py-3 text-[13px] leading-relaxed text-textSecondary">
      {children}
    </div>
  );
}
