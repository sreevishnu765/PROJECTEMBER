import { useEffect, useState } from "react";
import { FileText, FolderDown, ScanEye } from "lucide-react";
import type { FileEntry, QueryResult } from "../../types";
import { PanelHost, PlaceholderNotice } from "./PanelHost";

interface FilesPanelProps {
  onClose: () => void;
  sendQuery: <T = unknown>(name: string, params?: Record<string, unknown>) => Promise<QueryResult<T>>;
}

const CATEGORY_ICON = {
  analyzed: ScanEye,
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

function FileRow({ entry }: { entry: FileEntry }) {
  const Icon = CATEGORY_ICON[entry.category] ?? FileText;
  return (
    <div className="flex items-center gap-2.5 rounded-lg border border-border px-3 py-2">
      <Icon size={15} className="shrink-0 text-textSecondary" />
      <div className="min-w-0 flex-1">
        <p className="truncate text-[13px] text-textPrimary" title={entry.path ?? undefined}>
          {entry.label}
        </p>
        <p className="text-[11px] text-textMuted">{formatWhen(entry.created_at)}</p>
      </div>
    </div>
  );
}

export function FilesPanel({ onClose, sendQuery }: FilesPanelProps) {
  const [entries, setEntries] = useState<FileEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

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
      {entries === null && !error && <p className="text-[13px] text-textMuted">Loading, sir…</p>}

      {entries !== null && (
        <div className="space-y-4">
          <div>
            <h3 className="mb-1.5 text-[11.5px] font-medium uppercase tracking-wide text-textMuted">Uploads</h3>
            {uploads.length === 0 ? (
              <p className="text-[13px] text-textMuted">Nothing analyzed yet — try "analyze this screenshot" or "analyze image at &lt;path&gt;".</p>
            ) : (
              <div className="space-y-1.5">
                {uploads.map((e) => (
                  <FileRow key={e.id} entry={e} />
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
                  <FileRow key={e.id} entry={e} />
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </PanelHost>
  );
}
