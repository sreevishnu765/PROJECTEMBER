import { useEffect, useState } from "react";
import type { QueryResult, SystemsStatus } from "../../types";
import { PanelHost, PlaceholderNotice } from "./PanelHost";

interface SystemsPanelProps {
  onClose: () => void;
  sendQuery: <T = unknown>(name: string, params?: Record<string, unknown>) => Promise<QueryResult<T>>;
}

function Row({ label, value, good }: { label: string; value: string; good: boolean | null }) {
  const dotColor = good === null ? "bg-textMuted" : good ? "bg-statusCloud" : "bg-statusOffline";
  return (
    <div className="flex items-center justify-between border-b border-border py-2.5 last:border-b-0">
      <span className="text-[13px] text-textSecondary">{label}</span>
      <span className="flex items-center gap-2 text-[13px] text-textPrimary">
        <span className={`h-1.5 w-1.5 rounded-full ${dotColor}`} />
        {value}
      </span>
    </div>
  );
}

export function SystemsPanel({ onClose, sendQuery }: SystemsPanelProps) {
  const [status, setStatus] = useState<SystemsStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    sendQuery<SystemsStatus>("systems_status").then((result) => {
      if (cancelled) return;
      if (result.ok && result.data) setStatus(result.data);
      else setError(result.error || "Couldn't load system status.");
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [sendQuery]);

  return (
    <PanelHost title="Systems" onClose={onClose}>
      {loading && <p className="text-[13px] text-textMuted">Checking, sir…</p>}
      {error && <PlaceholderNotice>{error}</PlaceholderNotice>}
      {status && (
        <div>
          <Row label="Cloud (Gemini)" value={status.cloud_available ? "Available" : "Unavailable"} good={status.cloud_available} />
          <Row
            label="Fallback tiers"
            value={status.fallback_tiers_configured.length ? status.fallback_tiers_configured.join(", ") : "None configured"}
            good={status.fallback_tiers_configured.length > 0}
          />
          <Row label="Local embedding" value={status.local_embedding_available ? "Loaded" : "Not loaded"} good={status.local_embedding_available} />
          <Row label="Local (Ollama)" value={status.ollama_available ? "Reachable" : "Unreachable"} good={status.ollama_available} />
          <Row
            label="Memory"
            value={`${status.memory.total} stored (${status.memory.embedded} embedded)`}
            good={null}
          />
          <Row label="Active reminders" value={String(status.active_reminders)} good={null} />
          <Row label="Background runtime" value={status.background_runtime_running ? "Running" : "Stopped"} good={status.background_runtime_running} />
        </div>
      )}
    </PanelHost>
  );
}
