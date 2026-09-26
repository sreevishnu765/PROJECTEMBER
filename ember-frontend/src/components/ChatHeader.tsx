import type { ConnectionState, ProviderFamily } from "../types";

interface ChatHeaderProps {
  connectionState: ConnectionState;
  lastProviderFamily: ProviderFamily | null;
}

// Dot color is the entire indicator — no text label at all. green = Gemini
// (cloud), yellow = a fallback tier (Cerebras/Groq/NVIDIA/Mistral), red =
// local (Ollama), grey = not connected / nothing answered yet this
// conversation. No left-side title either — the sidebar toggle button
// floats fixed over this corner regardless of sidebar state, so any text
// here would sit right underneath it.
function dotFor(connectionState: ConnectionState, family: ProviderFamily | null): { color: string; label: string; pulse: boolean } {
  if (connectionState === "connecting" || connectionState === "authenticating") {
    return { color: "bg-statusOffline", label: "Connecting…", pulse: true };
  }
  if (connectionState !== "connected") {
    return { color: "bg-statusOffline", label: "Offline", pulse: false };
  }
  switch (family) {
    case "cloud":
      return { color: "bg-statusCloud", label: "Cloud", pulse: false };
    case "fallback":
      return { color: "bg-statusFallback", label: "Fallback", pulse: false };
    case "local":
      return { color: "bg-statusLocal", label: "Local", pulse: false };
    default:
      return { color: "bg-statusCloud", label: "Connected", pulse: false };
  }
}

export function ChatHeader({ connectionState, lastProviderFamily }: ChatHeaderProps) {
  const dot = dotFor(connectionState, lastProviderFamily);

  return (
    <header className="flex h-14 shrink-0 items-center justify-end border-b border-border px-6">
      <span
        title={dot.label}
        className={`h-2 w-2 shrink-0 rounded-full ${dot.color} ${dot.pulse ? "animate-pulseDot" : ""}`}
      />
    </header>
  );
}
