import { Laptop2, Smartphone } from "lucide-react";
import { PanelHost, PlaceholderNotice } from "./PanelHost";

// Dummy data, deliberately labeled as such — real multi-device sync
// (ember_conversation.py's registry already supports more than one
// connection; nothing yet represents "your phone" as a pairable identity)
// doesn't exist server-side. This shows the intended shape without
// pretending it already works.
const DUMMY_DEVICES = [
  { name: "This Desktop", icon: Laptop2, status: "online" as const },
  { name: "Phone", icon: Smartphone, status: "offline" as const },
];

function StatusPill({ status }: { status: "online" | "offline" }) {
  const color = status === "online" ? "bg-statusCloud" : "bg-statusOffline";
  return (
    <span className="flex items-center gap-1.5 text-[12px] text-textSecondary">
      <span className={`h-1.5 w-1.5 rounded-full ${color}`} />
      {status === "online" ? "Online" : "Offline"}
    </span>
  );
}

export function DevicesPanel({ onClose }: { onClose: () => void }) {
  return (
    <PanelHost title="Devices" onClose={onClose}>
      <div className="space-y-2">
        {DUMMY_DEVICES.map(({ name, icon: Icon, status }) => (
          <div
            key={name}
            className="flex items-center justify-between rounded-lg border border-border px-3 py-2.5"
          >
            <div className="flex items-center gap-2.5 text-textPrimary">
              <Icon size={16} className="text-textSecondary" />
              <span className="text-[13px]">{name}</span>
            </div>
            <StatusPill status={status} />
          </div>
        ))}
        <PlaceholderNotice>
          Dummy data for now — real device pairing and connection status
          arrive once multi-device sync is actually built.
        </PlaceholderNotice>
      </div>
    </PanelHost>
  );
}
