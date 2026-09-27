import {
  Cpu,
  Folder,
  Gauge,
  History,
  Laptop2,
  Plus,
  Settings,
  Sparkles,
} from "lucide-react";
import type { PanelId } from "../App";

interface SidebarProps {
  open: boolean;
  activePanel: PanelId;
  onNewConversation: () => void;
  onSelectPanel: (panel: PanelId) => void;
}

// Conceptual nav — everything except New Conversation opens as a panel
// over the chat (see PanelHost in App.tsx) rather than a page navigation,
// so the chat itself never has to unmount. No inline conversation list
// here on purpose — "Past Conversations" is a single entry point, not a
// running list, to keep this sidebar short.
const NAV_ITEMS: { id: Exclude<PanelId, null>; label: string; icon: typeof History }[] = [
  { id: "history", label: "Past Conversations", icon: History },
  { id: "memory", label: "Memory", icon: Sparkles },
  { id: "systems", label: "Systems", icon: Cpu },
  { id: "devices", label: "Devices", icon: Laptop2 },
  { id: "files", label: "Files", icon: Folder },
  { id: "usage", label: "API / Usage", icon: Gauge },
];

export function Sidebar({ open, activePanel, onNewConversation, onSelectPanel }: SidebarProps) {
  return (
    <aside
      className={`flex h-full shrink-0 flex-col overflow-hidden border-r border-border bg-surface transition-[width] duration-200 ease-out ${
        open ? "w-60" : "w-0 border-r-0"
      }`}
    >
      {/* Fixed inner width so content doesn't reflow/wrap while the
          container animates from 0 — it just gets revealed. */}
      <div className="flex h-full w-60 flex-col pt-16">
        <div className="px-2">
          <button
            onClick={onNewConversation}
            className="flex w-full items-center gap-2 rounded-lg border border-border px-2.5 py-2 text-[13px] text-textPrimary hover:border-borderLight hover:bg-surfaceRaised"
          >
            <Plus size={15} />
            <span>New conversation</span>
          </button>
        </div>

        <nav className="mt-4 flex-1 space-y-0.5 px-2">
          {NAV_ITEMS.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              onClick={() => onSelectPanel(id)}
              className={`flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px] transition-colors ${
                activePanel === id
                  ? "bg-surfaceRaised text-textPrimary"
                  : "text-textSecondary hover:bg-surfaceRaised hover:text-textPrimary"
              }`}
            >
              <Icon size={15} className={`shrink-0 ${activePanel === id ? "text-ember" : ""}`} />
              <span>{label}</span>
            </button>
          ))}
        </nav>

        <div className="px-2 pb-3">
          <button
            onClick={() => onSelectPanel("settings")}
            className={`flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13px] transition-colors ${
              activePanel === "settings"
                ? "bg-surfaceRaised text-textPrimary"
                : "text-textSecondary hover:bg-surfaceRaised hover:text-textPrimary"
            }`}
          >
            <Settings size={15} />
            <span>Settings</span>
          </button>
        </div>
      </div>
    </aside>
  );
}
