import { useState } from "react";
import { Flame } from "lucide-react";
import type { ConnectionState } from "../types";

interface ConnectScreenProps {
  connectionState: ConnectionState;
  lastError: string | null;
  savedUrl: string;
  savedToken: string;
  onConnect: (url: string, token: string) => void;
}

export function ConnectScreen({ connectionState, lastError, savedUrl, savedToken, onConnect }: ConnectScreenProps) {
  const [url, setUrl] = useState(savedUrl || "ws://localhost:8765");
  const [token, setToken] = useState(savedToken);

  const busy = connectionState === "connecting" || connectionState === "authenticating";

  return (
    <div className="flex h-full items-center justify-center bg-base px-6">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center text-center">
          <div className="mb-3 flex h-11 w-11 items-center justify-center rounded-2xl bg-ember/10 text-ember">
            <Flame size={20} />
          </div>
          <h1 className="text-[16px] font-medium text-textPrimary">Connect to Ember</h1>
          <p className="mt-1 text-[13px] text-textSecondary">
            Ember runs on your machine. Point this at your running <code className="text-textMuted">ember_transport.py</code> server.
          </p>
        </div>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            onConnect(url.trim(), token);
          }}
          className="space-y-3"
        >
          <div>
            <label className="mb-1.5 block text-[12px] text-textSecondary">Server address</label>
            <input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="ws://localhost:8765"
              className="w-full rounded-lg border border-border bg-surface px-3 py-2 text-[13.5px] text-textPrimary placeholder:text-textMuted focus:border-borderLight focus:outline-none"
            />
          </div>
          <div>
            <label className="mb-1.5 block text-[12px] text-textSecondary">Access token</label>
            <input
              value={token}
              onChange={(e) => setToken(e.target.value)}
              type="password"
              placeholder="EMBER_TRANSPORT_TOKEN"
              className="w-full rounded-lg border border-border bg-surface px-3 py-2 text-[13.5px] text-textPrimary placeholder:text-textMuted focus:border-borderLight focus:outline-none"
            />
            <p className="mt-1.5 text-[11px] text-textMuted">
              The same value set in the environment where Ember's backend runs. Never sent anywhere but that server.
            </p>
          </div>

          {lastError && (
            <div className="rounded-lg border border-statusOffline/30 bg-statusOffline/10 px-3 py-2 text-[12.5px] text-textSecondary">
              {lastError}
            </div>
          )}

          <button
            type="submit"
            disabled={busy || !url.trim() || !token.trim()}
            className="w-full rounded-lg bg-ember py-2 text-[13.5px] font-medium text-base transition-opacity disabled:opacity-40"
          >
            {busy ? "Connecting…" : "Connect"}
          </button>
        </form>
      </div>
    </div>
  );
}
