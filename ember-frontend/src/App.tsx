import { useEffect, useRef, useState } from "react";
import emberLogo from "./assets/ember-logo.jpeg";
import { Sidebar } from "./components/Sidebar";
import { ChatHeader } from "./components/ChatHeader";
import { MessageList } from "./components/MessageList";
import { Composer } from "./components/Composer";
import { ConnectScreen } from "./components/ConnectScreen";
import { ConfirmationDialog } from "./components/ConfirmationDialog";
import { HistoryPanel } from "./components/panels/HistoryPanel";
import { MemoryPanel } from "./components/panels/MemoryPanel";
import { SystemsPanel } from "./components/panels/SystemsPanel";
import { DevicesPanel } from "./components/panels/DevicesPanel";
import { FilesPanel } from "./components/panels/FilesPanel";
import { UsagePanel } from "./components/panels/UsagePanel";
import { SettingsPanel } from "./components/panels/SettingsPanel";
import { useEmberChat } from "./hooks/useEmberChat";
import type { HudState } from "./types";
import { describeConfirmation } from "./lib/confirmText";

export type PanelId = "history" | "memory" | "systems" | "devices" | "files" | "usage" | "settings" | null;

export default function App() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [activePanel, setActivePanel] = useState<PanelId>(null);
  const {
    connectionState,
    messages,
    isGenerating,
    pendingConfirmation,
    lastError,
    savedUrl,
    savedToken,
    voiceStatus,
    connect,
    sendMessage,
    sendQuery,
    stopSpeaking,
    respondConfirmation,
    clearConversation,
    toggleVoiceMode,
    toggleListening,
    enterHudMode,
    exitHudMode,
    setVoiceName,
  } = useEmberChat();
  const [hudOpen, setHudOpen] = useState(false);

  const lastAssistant = [...messages].reverse().find((m) => m.role === "assistant" && m.tag);

  // Auto-connect to the backend Electron just launched (see
  // electron/main.cjs) — a "single click, one app" experience means the
  // person never sees the connect form at all in the normal case.
  // window.emberBackend only exists inside the Electron shell; a plain
  // browser tab (still useful for frontend-only dev work) falls straight
  // through to the manual ConnectScreen below, unchanged.
  //
  // Retried, not one-shot: the Python process was JUST spawned and can
  // take a few seconds to finish startup_check() and actually bind the
  // WebSocket port, so the very first attempt failing is expected, not an
  // error worth giving up on immediately.
  const MAX_AUTO_ATTEMPTS = 20;
  const AUTO_RETRY_MS = 1500;
  const autoAttemptsRef = useRef(0);
  const autoGaveUpRef = useRef(false);
  const [autoConnecting, setAutoConnecting] = useState(false);

  useEffect(() => {
    if (!window.emberBackend) return;
    if (connectionState !== "disconnected" && connectionState !== "error") return;
    if (autoGaveUpRef.current) return;
    if (autoAttemptsRef.current >= MAX_AUTO_ATTEMPTS) {
      autoGaveUpRef.current = true;
      setAutoConnecting(false);
      return;
    }

    let cancelled = false;
    setAutoConnecting(true);
    const timer = setTimeout(
      () => {
        if (cancelled) return;
        autoAttemptsRef.current += 1;
        window.emberBackend!.getConnection().then(({ url, token }) => connect(url, token));
      },
      autoAttemptsRef.current === 0 ? 0 : AUTO_RETRY_MS,
    );

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectionState]);

  // ---- HUD wiring (Electron only) -----------------------------------
  // Main process tells us when the HUD window opens/closes (however it was
  // closed — X button, Esc, Alt+F4); we switch voice mode / speaker to match.
  useEffect(() => {
    const api = window.emberHud;
    if (!api) return;
    return api.onVisibility(({ open }) => {
      setHudOpen(open);
      if (open) void enterHudMode();
      else exitHudMode();
    });
  }, [enterHudMode, exitHudMode]);

  // The HUD has no connection of its own — it types/stops through us.
  useEffect(() => {
    const api = window.emberHud;
    if (!api) return;
    return api.onCommand((cmd) => {
      if (cmd.type === "send") {
        const text = cmd.text.trim();
        if (text) sendMessage(text);
      } else if (cmd.type === "stop") {
        stopSpeaking();
      } else if (cmd.type === "confirm") {
        respondConfirmation(cmd.approved);
      }
    });
  }, [sendMessage, stopSpeaking, respondConfirmation]);

  // Push a compact snapshot to the HUD whenever something it shows changes.
  useEffect(() => {
    const api = window.emberHud;
    if (!api || !hudOpen) return;
    const snapshot: HudState = {
      connected: connectionState === "connected",
      confirmation: pendingConfirmation
        ? { toolName: pendingConfirmation.toolName, summary: describeConfirmation(pendingConfirmation) }
        : null,
      voiceState: voiceStatus.state,
      awake: voiceStatus.awake,
      voiceMode: voiceStatus.voiceMode,
      speaking: voiceStatus.speaking,
      generating: isGenerating,
      warning: voiceStatus.warning,
      turns: messages.slice(-6).map((m) => ({
        id: m.id,
        role: m.role,
        content: m.content,
        statusLine: m.statusLine,
        error: m.error,
        pending: m.pending,
      })),
    };
    api.publishState(snapshot);
  }, [hudOpen, messages, voiceStatus, isGenerating, connectionState, pendingConfirmation]);

  if (connectionState !== "connected") {
    // Auto-connecting inside the Electron shell: show a quiet loading
    // state instead of the manual form while retries are in flight, and
    // only fall back to the real ConnectScreen once retries are
    // exhausted (backend genuinely didn't start — worth surfacing then).
    if (window.emberBackend && autoConnecting && !autoGaveUpRef.current) {
      return (
        <div className="flex h-full items-center justify-center bg-base">
          <div className="flex flex-col items-center gap-3 text-textSecondary">
            <div className="h-8 w-8 animate-pulse rounded-2xl bg-ember/15" />
            <p className="text-[13px]">Starting Ember…</p>
          </div>
        </div>
      );
    }
    return (
      <ConnectScreen
        connectionState={connectionState}
        lastError={lastError}
        savedUrl={savedUrl}
        savedToken={savedToken}
        onConnect={connect}
      />
    );
  }

  const closePanel = () => setActivePanel(null);

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-base">
      {/* Ember mark — always visible, top-left, independent of sidebar
          width so it's never clipped by the collapse animation. This is
          the one and only way the sidebar opens/closes. */}
      <button
        onClick={() => setSidebarOpen((v) => !v)}
        className="fixed left-3 top-3 z-50 flex h-9 w-9 items-center justify-center overflow-hidden rounded-xl border border-border bg-surface transition-colors hover:border-borderLight"
        aria-label={sidebarOpen ? "Close sidebar" : "Open sidebar"}
      >
        <img src={emberLogo} alt="Ember" className="h-full w-full object-cover" />
      </button>

      <Sidebar
        open={sidebarOpen}
        activePanel={activePanel}
        onNewConversation={() => {
          clearConversation();
          setSidebarOpen(false);
        }}
        onSelectPanel={(panel) => setActivePanel(panel)}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <ChatHeader connectionState={connectionState} lastProviderFamily={lastAssistant?.providerFamily ?? null} />

        <div className="flex min-h-0 flex-1 flex-col">
          <MessageList messages={messages} onSuggestion={sendMessage} />
        </div>

        <Composer
          disabled={connectionState !== "connected"}
          isGenerating={isGenerating}
          onSend={sendMessage}
          onCancel={stopSpeaking}
          voiceStatus={voiceStatus}
          onToggleVoice={toggleVoiceMode}
          onToggleListening={toggleListening}
          onOpenHud={() => void window.emberHud?.open()}
        />
      </div>

      {pendingConfirmation && (
        <ConfirmationDialog confirmation={pendingConfirmation} onRespond={respondConfirmation} />
      )}

      {activePanel === "history" && <HistoryPanel onClose={closePanel} sendQuery={sendQuery} />}
      {activePanel === "memory" && <MemoryPanel onClose={closePanel} sendQuery={sendQuery} />}
      {activePanel === "systems" && <SystemsPanel onClose={closePanel} sendQuery={sendQuery} />}
      {activePanel === "devices" && <DevicesPanel onClose={closePanel} />}
      {activePanel === "files" && <FilesPanel onClose={closePanel} sendQuery={sendQuery} />}
      {activePanel === "usage" && <UsagePanel onClose={closePanel} sendQuery={sendQuery} />}
      {activePanel === "settings" && <SettingsPanel onClose={closePanel} onSelectVoice={setVoiceName} />}
    </div>
  );
}
