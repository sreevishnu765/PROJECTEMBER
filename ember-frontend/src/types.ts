export type ConnectionState = "disconnected" | "connecting" | "authenticating" | "connected" | "error";

// Present only inside the Electron shell (see electron/preload.cjs) —
// undefined in a plain browser tab, which is how the app tells the two
// apart and falls back to the manual ConnectScreen there.
declare global {
  interface Window {
    emberBackend?: {
      getConnection: () => Promise<{ url: string; token: string }>;
    };
  }
}

// Matches llm_client.GenerateResult.source — "cloud" (Gemini), "fallback"
// (Cerebras/Groq/NVIDIA/Mistral), or "local" (Ollama). The header dot's
// color comes straight from this, nothing invented on the frontend side.
export type ProviderFamily = "cloud" | "fallback" | "local";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  /** Transient "Searching…"/"Checking sources…" note from a "status" message — cleared once real content arrives. */
  statusLine: string | null;
  /** Raw tag process_turn() returned (e.g. "gemini-3.5-flash+search", "local+search") — parsed to derive providerFamily. */
  tag: string | null;
  providerFamily: ProviderFamily | null;
  grounded: boolean;
  error: string | null;
  pending: boolean;
}

export interface PendingConfirmation {
  requestId: string;
  toolName: string;
  args: Record<string, unknown>;
}

// Server -> client message shapes, per ember_transport.py's protocol.
export type ServerMessage =
  | { type: "auth_ok" }
  | { type: "auth_failed" }
  | { type: "chunk"; text: string }
  | { type: "status"; text: string }
  | { type: "done"; tag: string; text: string }
  | { type: "confirmation_required"; request_id: string; tool_name: string; args: Record<string, unknown> }
  | { type: "query_result"; id: string; ok: true; data: unknown }
  | { type: "query_result"; id: string; ok: false; error: string }
  | { type: "error"; message: string };

export interface QueryResult<T = unknown> {
  ok: boolean;
  data?: T;
  error?: string;
}

// ---- Query payload shapes (ember_query_registry.py handlers in ember_core.py) ----
export interface SystemsStatus {
  cloud_available: boolean;
  fallback_tiers_configured: string[];
  local_embedding_available: boolean;
  ollama_available: boolean;
  memory: { total: number; embedded: number; without_embedding: number; partial_embedding: number };
  active_reminders: number;
  background_runtime_running: boolean;
}

export interface UsageQuota {
  quota: Record<string, number | null>;
  fallback_tiers_configured: string[];
  cloud_tiers: string[];
  all_fallback_tiers: string[];
}

export interface MemoryRow {
  id: number;
  text: string;
  embedded: boolean;
  memory_type: string;
  project: string | null;
  importance: number;
  confidence: number;
  created_at: number;
  last_accessed_at: number | null;
  access_count: number;
}

export interface FileEntry {
  id: string;
  kind: "upload" | "generated";
  category: "analyzed" | "pdf_export" | "drive_download";
  path: string | null;
  label: string;
  created_at: number;
}

export interface HistorySessionSummary {
  session_id: string;
  turn_count: number;
  started_at: number;
  last_active_at: number;
  preview: string;
}

export interface HistoryTurn {
  role: "user" | "assistant";
  content: string;
  used_search: boolean;
  created_at: number;
}
