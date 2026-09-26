import { useEffect, useState } from "react";
import { ArrowLeft } from "lucide-react";
import type { HistorySessionSummary, HistoryTurn, QueryResult } from "../../types";
import { PanelHost, PlaceholderNotice } from "./PanelHost";

interface HistoryPanelProps {
  onClose: () => void;
  sendQuery: <T = unknown>(name: string, params?: Record<string, unknown>) => Promise<QueryResult<T>>;
}

function formatWhen(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function HistoryPanel({ onClose, sendQuery }: HistoryPanelProps) {
  const [sessions, setSessions] = useState<HistorySessionSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openSessionId, setOpenSessionId] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<HistoryTurn[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    sendQuery<HistorySessionSummary[]>("history_list_sessions").then((result) => {
      if (cancelled) return;
      if (result.ok && result.data) setSessions(result.data);
      else setError(result.error || "Couldn't load past conversations.");
    });
    return () => {
      cancelled = true;
    };
  }, [sendQuery]);

  const openSession = async (sessionId: string) => {
    setOpenSessionId(sessionId);
    setTranscript(null);
    const result = await sendQuery<HistoryTurn[]>("history_get_session", { session_id: sessionId });
    if (result.ok && result.data) setTranscript(result.data);
    else setError(result.error || "Couldn't load that conversation.");
  };

  // Read-only browsing, deliberately — this views an archived transcript,
  // it doesn't re-attach it as the live conversation. Continuing an old
  // session as the active chat is a real, separate feature, not
  // something to fold in silently here.
  if (openSessionId) {
    return (
      <PanelHost title="Past Conversation" onClose={onClose}>
        <button
          onClick={() => setOpenSessionId(null)}
          className="mb-3 flex items-center gap-1.5 text-[12.5px] text-textSecondary hover:text-textPrimary"
        >
          <ArrowLeft size={13} /> Back to list
        </button>
        {transcript === null && <p className="text-[13px] text-textMuted">Loading, sir…</p>}
        {transcript && (
          <div className="space-y-3">
            {transcript.map((turn, i) => (
              <div key={i} className={turn.role === "user" ? "text-right" : "text-left"}>
                <div
                  className={`inline-block max-w-[85%] rounded-xl px-3 py-1.5 text-[13px] ${
                    turn.role === "user" ? "bg-surfaceRaised text-textPrimary" : "text-textPrimary"
                  }`}
                >
                  {turn.content}
                </div>
              </div>
            ))}
          </div>
        )}
      </PanelHost>
    );
  }

  return (
    <PanelHost title="Past Conversations" onClose={onClose}>
      {error && <PlaceholderNotice>{error}</PlaceholderNotice>}
      {sessions === null && !error && <p className="text-[13px] text-textMuted">Loading, sir…</p>}
      {sessions !== null && sessions.length === 0 && (
        <p className="text-[13px] text-textMuted">No conversations recorded yet.</p>
      )}
      {sessions && sessions.length > 0 && (
        <div className="space-y-1.5">
          {sessions.map((s) => (
            <button
              key={s.session_id}
              onClick={() => openSession(s.session_id)}
              className="w-full rounded-lg border border-border px-3 py-2 text-left hover:border-borderLight hover:bg-surfaceRaised"
            >
              <div className="flex items-center justify-between">
                <span className="truncate text-[13px] text-textPrimary">{s.preview || "(empty)"}</span>
                <span className="ml-2 shrink-0 text-[11px] text-textMuted">{formatWhen(s.last_active_at)}</span>
              </div>
              <p className="mt-0.5 text-[11px] text-textMuted">{s.turn_count} turns</p>
            </button>
          ))}
        </div>
      )}
    </PanelHost>
  );
}
