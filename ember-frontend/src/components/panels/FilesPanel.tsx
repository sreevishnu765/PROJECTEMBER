import { useEffect, useRef, useState } from "react";
import { FileText, FolderDown, FolderOpen, ScanEye } from "lucide-react";
import type { FileEntry, QueryResult } from "../../types";
import { PanelHost, PlaceholderNotice } from "./PanelHost";

interface FilesPanelProps {
  onClose: () => void;
  sendQuery: <T = unknown>(name: string, params?: Record<string, unknown>) => Promise<QueryResult<T>>;
}

const CATEGORY_ICON = {
  analyzed: ScanEye,
  attached: FileText,
  pdf_export: FileText,
  drive_download: FolderDown,
} as const;

function formatWhen(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

interface OpenResult {
  opened: boolean;
  message: string;
}

function FileRow({
  entry,
  onOpen,
  onReveal,
}: {
  entry: FileEntry;
  onOpen: (entry: FileEntry) => void;
  onReveal: (entry: FileEntry) => void;
}) {
  const Icon = CATEGORY_ICON[entry.category] ?? FileText;
  const openable = Boolean(entry.path);
  return (
    <div className="flex items-center gap-1 rounded-lg border border-border pr-1.5 transition-colors hover:border-borderLight">
      <button
        type="button"
        onClick={() => onOpen(entry)}
        disabled={!openable}
        title={openable ? `Open ${entry.path}` : "No file location was recorded for this entry"}
        className="flex min-w-0 flex-1 items-center gap-2.5 rounded-lg px-3 py-2 text-left hover:bg-surfaceRaised disabled:cursor-default disabled:hover:bg-transparent"
      >
        <Icon size={15} className="shrink-0 text-textSecondary" />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13px] text-textPrimary">{entry.label}</p>
          <p className="text-[11px] text-textMuted">{formatWhen(entry.created_at)}</p>
        </div>
      </button>
      {openable && (
        <button
          type="button"
          onClick={() => onReveal(entry)}
          title="Show in File Explorer"
          aria-label={`Show ${entry.label} in File Explorer`}
          className="shrink-0 rounded-md p-1.5 text-textMuted hover:bg-surfaceRaised hover:text-textPrimary"
        >
          <FolderOpen size={14} />
        </button>
      )}
    </div>
  );
}

export function FilesPanel({ onClose, sendQuery }: FilesPanelProps) {
  const [entries, setEntries] = useState<FileEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
  }, []);

  // Opening happens on the machine Ember's backend runs on (your PC), through the
  // `file_open` query. It takes the entry's id, never a path from the browser.
  const openEntry = async (entry: FileEntry, reveal: boolean) => {
    const result = await sendQuery<OpenResult>("file_open", { id: entry.id, reveal });
    const message = result.ok && result.data ? result.data.message : result.error || "Couldn't open that.";
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    setNotice(message);
    noticeTimer.current = setTimeout(() => setNotice(null), 4500);
  };

  useEffect(() => {
    let cancelled = false;
    sendQuery<FileEntry[]>("files_list").then((result) => {
      if (cancelled) return;
      if (result.ok && result.data) setEntries(result.data);
      else setError(result.error || "Couldn't load files.");
    });
    return () => {
      cancelled = true;
    };
  }, [sendQuery]);

  const uploads = entries?.filter((e) => e.kind === "upload") ?? [];
  const generated = entries?.filter((e) => e.kind === "generated") ?? [];

  return (
    <PanelHost title="Files" onClose={onClose}>
      {error && <PlaceholderNotice>{error}</PlaceholderNotice>}
      {notice && (
        <div className="mb-3 rounded-lg border border-border bg-surfaceRaised px-3 py-2 text-[12.5px] text-textSecondary">{notice}</div>
      )}
      {entries === null && !error && <p className="text-[13px] text-textMuted">Loading, sir…</p>}

      {entries !== null && (
        <div className="space-y-4">
          <div>
            <h3 className="mb-1.5 text-[11.5px] font-medium uppercase tracking-wide text-textMuted">Uploads</h3>
            {uploads.length === 0 ? (
              <p className="text-[13px] text-textMuted">Nothing yet — attach a file in the chat (paperclip), or say "analyze this screenshot".</p>
            ) : (
              <div className="space-y-1.5">
                {uploads.map((e) => (
                  <FileRow key={e.id} entry={e} onOpen={(x) => openEntry(x, false)} onReveal={(x) => openEntry(x, true)} />
                ))}
              </div>
            )}
          </div>

          <div>
            <h3 className="mb-1.5 text-[11.5px] font-medium uppercase tracking-wide text-textMuted">Generated</h3>
            {generated.length === 0 ? (
              <p className="text-[13px] text-textMuted">Nothing generated yet — PDF exports and Drive downloads will show up here.</p>
            ) : (
              <div className="space-y-1.5">
                {generated.map((e) => (
                  <FileRow key={e.id} entry={e} onOpen={(x) => openEntry(x, false)} onReveal={(x) => openEntry(x, true)} />
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </PanelHost>
  );
}
