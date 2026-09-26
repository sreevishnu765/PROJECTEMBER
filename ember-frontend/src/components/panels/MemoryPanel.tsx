import { useCallback, useEffect, useState } from "react";
import { Trash2 } from "lucide-react";
import type { MemoryRow, QueryResult } from "../../types";
import { PanelHost, PlaceholderNotice } from "./PanelHost";

interface MemoryPanelProps {
  onClose: () => void;
  sendQuery: <T = unknown>(name: string, params?: Record<string, unknown>) => Promise<QueryResult<T>>;
}

const TYPE_LABEL: Record<string, string> = {
  semantic: "fact",
  episodic: "event",
  preference: "preference",
};

export function MemoryPanel({ onClose, sendQuery }: MemoryPanelProps) {
  const [rows, setRows] = useState<MemoryRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [newFact, setNewFact] = useState("");
  const [adding, setAdding] = useState(false);
  const [forgettingId, setForgettingId] = useState<number | null>(null);

  const refresh = useCallback(async () => {
    const result = await sendQuery<MemoryRow[]>("memory_list", { n: 100 });
    if (result.ok && result.data) {
      setRows(result.data);
      setError(null);
    } else {
      setError(result.error || "Couldn't load memory.");
    }
  }, [sendQuery]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const handleAdd = async () => {
    const text = newFact.trim();
    if (!text) return;
    setAdding(true);
    const result = await sendQuery("memory_remember", { text });
    setAdding(false);
    if (result.ok) {
      setNewFact("");
      refresh();
    } else {
      setError(result.error || "Couldn't save that.");
    }
  };

  const handleForget = async (row: MemoryRow) => {
    // Deletion goes through the normal destructive-tool confirmation
    // flow (the app's shared ConfirmationDialog) — this call simply
    // won't resolve until that's answered, same as any other
    // destructive tool call.
    setForgettingId(row.id);
    // A short, exact substring of this specific row's own text — forget()
    // matches by substring, so this targets just this row rather than
    // anything else that happens to share a word with it.
    const result = await sendQuery("memory_forget", { query: row.text });
    setForgettingId(null);
    if (result.ok) refresh();
    else setError(result.error || "Couldn't delete that.");
  };

  return (
    <PanelHost title="Memory" onClose={onClose}>
      <div className="space-y-3">
        <div className="flex gap-2">
          <input
            value={newFact}
            onChange={(e) => setNewFact(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleAdd()}
            placeholder="Remember something…"
            className="flex-1 rounded-lg border border-border bg-base px-3 py-2 text-[13px] text-textPrimary placeholder:text-textMuted focus:border-borderLight focus:outline-none"
          />
          <button
            onClick={handleAdd}
            disabled={adding || !newFact.trim()}
            className="rounded-lg bg-ember px-3 py-2 text-[13px] font-medium text-base disabled:opacity-40"
          >
            Add
          </button>
        </div>

        {error && <PlaceholderNotice>{error}</PlaceholderNotice>}

        {rows === null && !error && <p className="text-[13px] text-textMuted">Loading, sir…</p>}

        {rows !== null && rows.length === 0 && (
          <p className="text-[13px] text-textMuted">Nothing stored yet.</p>
        )}

        {rows && rows.length > 0 && (
          <div className="space-y-1.5">
            {rows.map((row) => (
              <div key={row.id} className="flex items-start justify-between gap-2 rounded-lg border border-border px-3 py-2">
                <div className="min-w-0">
                  <p className="text-[13px] text-textPrimary">{row.text}</p>
                  <p className="mt-0.5 text-[11px] text-textMuted">
                    {TYPE_LABEL[row.memory_type] || row.memory_type}
                    {row.project ? ` · ${row.project}` : ""}
                    {!row.embedded ? " · keyword-only" : ""}
                  </p>
                </div>
                <button
                  onClick={() => handleForget(row)}
                  disabled={forgettingId === row.id}
                  className="shrink-0 rounded-md p-1.5 text-textMuted hover:bg-surfaceRaised hover:text-statusOffline disabled:opacity-40"
                  aria-label="Forget"
                >
                  <Trash2 size={14} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </PanelHost>
  );
}
