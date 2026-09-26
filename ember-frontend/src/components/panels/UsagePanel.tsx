import { useEffect, useState } from "react";
import type { QueryResult, UsageQuota } from "../../types";
import { PanelHost, PlaceholderNotice } from "./PanelHost";

interface UsagePanelProps {
  onClose: () => void;
  sendQuery: <T = unknown>(name: string, params?: Record<string, unknown>) => Promise<QueryResult<T>>;
}

export function UsagePanel({ onClose, sendQuery }: UsagePanelProps) {
  const [usage, setUsage] = useState<UsageQuota | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    sendQuery<UsageQuota>("usage_quota").then((result) => {
      if (cancelled) return;
      if (result.ok && result.data) setUsage(result.data);
      else setError(result.error || "Couldn't load usage data.");
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [sendQuery]);

  return (
    <PanelHost title="API / Usage" onClose={onClose}>
      {loading && <p className="text-[13px] text-textMuted">Checking, sir…</p>}
      {error && <PlaceholderNotice>{error}</PlaceholderNotice>}
      {usage && (
        <div className="space-y-4">
          <div>
            <h3 className="mb-2 text-[11.5px] font-medium uppercase tracking-wide text-textMuted">Cloud (Gemini)</h3>
            <div className="space-y-1.5">
              {usage.cloud_tiers.map((model) => {
                const remaining = usage.quota[model];
                return (
                  <div key={model} className="flex items-center justify-between text-[13px]">
                    <span className="text-textSecondary">{model}</span>
                    <span className="text-textPrimary">{remaining === null || remaining === undefined ? "unlimited/untracked" : `${remaining} left today`}</span>
                  </div>
                );
              })}
            </div>
          </div>

          <div>
            <h3 className="mb-2 text-[11.5px] font-medium uppercase tracking-wide text-textMuted">Fallback tiers</h3>
            {usage.fallback_tiers_configured.length === 0 ? (
              <p className="text-[13px] text-textMuted">None configured — see .env for the *_API_KEY vars.</p>
            ) : (
              <div className="space-y-1.5">
                {usage.all_fallback_tiers
                  .filter((name) => usage.fallback_tiers_configured.includes(name))
                  .map((name) => (
                    <div key={name} className="flex items-center justify-between text-[13px]">
                      <span className="text-textSecondary">{name}</span>
                      <span className="text-textPrimary">configured</span>
                    </div>
                  ))}
              </div>
            )}
          </div>
        </div>
      )}
    </PanelHost>
  );
}
