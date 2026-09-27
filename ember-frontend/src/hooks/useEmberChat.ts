import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatMessage, ConnectionState, PendingConfirmation, QueryResult, ServerMessage } from "../types";
import { isGroundedTag, parseProviderFamily } from "../lib/providerTag";
import { startMicCapture, VoicePlaybackQueue, type MicCapture } from "../lib/voiceAudio";

const URL_STORAGE_KEY = "ember.serverUrl";
const TOKEN_STORAGE_KEY = "ember.token";
const VOICE_STORAGE_KEY = "ember.voicePreference"; // same key SettingsPanel already writes to

// Mirrors ember_voice.py's own {"type":"voice_state",...} shape. `active`
// is the one thing the button actually cares about — mic capture and
// voice mode are toggled TOGETHER as a single concept here (one button,
// one state), not exposed as two independently-toggleable things.
export type VoiceStatus = {
  active: boolean;
  state: string | null;
  voiceMode: boolean;
  awake: boolean;
  speaking: boolean;
  lastEvent: string | null;
  warning: string | null;
};

const INITIAL_VOICE_STATUS: VoiceStatus = {
  active: false,
  state: null,
  voiceMode: false,
  awake: false,
  speaking: false,
  lastEvent: null,
  warning: null,
};

function makeId(): string {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

export function useEmberChat() {
  const [connectionState, setConnectionState] = useState<ConnectionState>("disconnected");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isGenerating, setIsGenerating] = useState(false);
  const [pendingConfirmation, setPendingConfirmation] = useState<PendingConfirmation | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);
  const [voiceStatus, setVoiceStatus] = useState<VoiceStatus>(INITIAL_VOICE_STATUS);

  const [savedUrl] = useState(() => localStorage.getItem(URL_STORAGE_KEY) ?? "");
  const [savedToken] = useState(() => localStorage.getItem(TOKEN_STORAGE_KEY) ?? "");

  const wsRef = useRef<WebSocket | null>(null);
  const currentAssistantIdRef = useRef<string | null>(null);
  const micRef = useRef<MicCapture | null>(null);
  const playbackRef = useRef<VoicePlaybackQueue | null>(null);
  // Panel data fetches (Systems/Usage/Memory) — keyed by the id we chose
  // when sending, resolved when a "query_result" with the matching id
  // comes back. Separate from the chat message flow entirely; a panel
  // query never touches `messages` or the visible chat.
  const pendingQueriesRef = useRef<Map<string, (result: QueryResult) => void>>(new Map());

  const updateAssistant = useCallback((id: string, patch: Partial<ChatMessage>) => {
    setMessages((prev) => prev.map((m) => (m.id === id ? { ...m, ...patch } : m)));
  }, []);

  const handleServerMessage = useCallback(
    (msg: ServerMessage) => {
      switch (msg.type) {
        case "auth_ok": {
          setConnectionState("connected");
          // A voice picked in Settings previously should actually take
          // effect on this connection, not just sit remembered in
          // localStorage until someone happens to revisit the panel.
          const savedVoice = localStorage.getItem(VOICE_STORAGE_KEY);
          if (savedVoice) {
            wsRef.current?.send(JSON.stringify({ type: "voice", action: "voice", name: savedVoice.toLowerCase() }));
          }
          break;
        }
        case "auth_failed":
          setConnectionState("error");
          setLastError("Authentication failed — check the access token.");
          wsRef.current?.close();
          break;
        case "chunk": {
          const id = currentAssistantIdRef.current;
          if (!id) break;
          setMessages((prev) =>
            prev.map((m) => (m.id === id ? { ...m, content: m.content + msg.text, statusLine: null, pending: false } : m)),
          );
          break;
        }
        case "status": {
          const id = currentAssistantIdRef.current;
          if (!id) break;
          updateAssistant(id, { statusLine: msg.text });
          break;
        }
        case "done": {
          const id = currentAssistantIdRef.current;
          setIsGenerating(false);
          currentAssistantIdRef.current = null;
          if (!id) break;
          const providerFamily = parseProviderFamily(msg.tag);
          updateAssistant(id, {
            content: msg.text || "",
            statusLine: null,
            tag: msg.tag,
            providerFamily,
            grounded: isGroundedTag(msg.tag),
            pending: false,
          });
          break;
        }
        case "confirmation_required":
          setPendingConfirmation({
            requestId: msg.request_id,
            toolName: msg.tool_name,
            args: msg.args,
          });
          break;
        case "query_result": {
          const resolve = pendingQueriesRef.current.get(msg.id);
          if (resolve) {
            pendingQueriesRef.current.delete(msg.id);
            resolve(msg.ok ? { ok: true, data: msg.data } : { ok: false, error: msg.error });
          }
          break;
        }
        case "error": {
          const id = currentAssistantIdRef.current;
          setIsGenerating(false);
          if (id) {
            updateAssistant(id, { error: msg.message, statusLine: null, pending: false });
            currentAssistantIdRef.current = null;
          } else {
            setLastError(msg.message);
          }
          break;
        }

        // ---- Voice: audio in from the server -----------------------------
        case "audio_chunk":
          playbackRef.current?.push(msg.data, msg.gen, msg.sample_rate);
          setVoiceStatus((prev) => (prev.speaking ? prev : { ...prev, speaking: true }));
          break;
        case "audio_stop":
          playbackRef.current?.flush(msg.gen);
          setVoiceStatus((prev) => ({ ...prev, speaking: false }));
          break;
        case "voice_duck":
          playbackRef.current?.setDucked(msg.on);
          break;

        // ---- Voice: status/events ------------------------------------------
        case "voice_state":
          setVoiceStatus((prev) => ({ ...prev, state: msg.state, voiceMode: msg.voice_mode, awake: msg.awake }));
          break;
        case "voice_event":
          setVoiceStatus((prev) => ({
            ...prev,
            lastEvent: msg.event,
            speaking: msg.event === "stopped" ? false : prev.speaking,
          }));
          break;
        case "voice_warning":
          setVoiceStatus((prev) => ({ ...prev, warning: msg.text }));
          break;
        case "voice_unavailable":
          setLastError(`Voice isn't available: ${msg.reason}`);
          setVoiceStatus((prev) => ({ ...prev, active: false }));
          break;

        // ---- Voice: a spoken utterance the server accepted as a command ---
        // Mirrors sendMessage()'s own bubble creation exactly — a voice
        // turn is auto-started server-side the instant it's transcribed,
        // so this client never SENDS a "message" for it, only renders what
        // already happened and gets ready for the chunk/done already coming.
        case "transcript": {
          const userMsg: ChatMessage = {
            id: makeId(),
            role: "user",
            content: msg.text,
            statusLine: null,
            tag: null,
            providerFamily: null,
            grounded: false,
            error: null,
            pending: false,
          };
          const assistantId = makeId();
          const assistantMsg: ChatMessage = {
            id: assistantId,
            role: "assistant",
            content: "",
            statusLine: null,
            tag: null,
            providerFamily: null,
            grounded: false,
            error: null,
            pending: true,
          };
          currentAssistantIdRef.current = assistantId;
          setMessages((prev) => [...prev, userMsg, assistantMsg]);
          setIsGenerating(true);
          break;
        }

        // Debug-only telemetry (see voice_client.py's own --debug output) —
        // not surfaced in the UI, just logged for anyone with devtools open.
        case "voice_heard":
        case "voice_timing":
          // eslint-disable-next-line no-console
          console.debug("[ember voice]", msg);
          break;
      }
    },
    [updateAssistant],
  );

  const connect = useCallback(
    (url: string, token: string) => {
      setLastError(null);
      setConnectionState("connecting");
      localStorage.setItem(URL_STORAGE_KEY, url);
      localStorage.setItem(TOKEN_STORAGE_KEY, token);

      let socket: WebSocket;
      try {
        socket = new WebSocket(url);
      } catch {
        setConnectionState("error");
        setLastError("Couldn't open a connection to that address.");
        return;
      }
      wsRef.current = socket;

      socket.onopen = () => {
        setConnectionState("authenticating");
        socket.send(JSON.stringify({ type: "auth", token }));
      };
      socket.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data) as ServerMessage;
          handleServerMessage(msg);
        } catch {
          // ignore malformed frames rather than crashing the connection
        }
      };
      socket.onerror = () => {
        setConnectionState("error");
        setLastError("Connection error — is the Ember backend running at that address?");
      };
      socket.onclose = () => {
        setConnectionState((prev) => (prev === "connected" ? "disconnected" : prev === "error" ? "error" : "disconnected"));
        // A dead socket can't be sent audio and won't be sending any back —
        // don't leave the mic hot or the UI thinking she might still speak.
        micRef.current?.stop();
        micRef.current = null;
        setVoiceStatus(INITIAL_VOICE_STATUS);
      };
    },
    [handleServerMessage],
  );

  const sendMessage = useCallback((text: string) => {
    const socket = wsRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) return;

    const userMsg: ChatMessage = {
      id: makeId(),
      role: "user",
      content: text,
      statusLine: null,
      tag: null,
      providerFamily: null,
      grounded: false,
      error: null,
      pending: false,
    };
    const assistantId = makeId();
    const assistantMsg: ChatMessage = {
      id: assistantId,
      role: "assistant",
      content: "",
      statusLine: null,
      tag: null,
      providerFamily: null,
      grounded: false,
      error: null,
      pending: true,
    };
    currentAssistantIdRef.current = assistantId;
    setMessages((prev) => [...prev, userMsg, assistantMsg]);
    setIsGenerating(true);
    socket.send(JSON.stringify({ type: "message", text }));
  }, []);

  const cancelGeneration = useCallback(() => {
    const socket = wsRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify({ type: "cancel" }));
  }, []);

  // ---- Voice ---------------------------------------------------------------
  const sendVoiceControl = useCallback((action: string, extra: Record<string, unknown> = {}) => {
    const socket = wsRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify({ type: "voice", action, ...extra }));
  }, []);

  /** The one voice button's entire job: mic capture and voice mode are a
   * single combined state here, not two independent toggles — clicking
   * turns both on together, clicking again turns both off. Call this from
   * a real user click; browsers/Electron require a user gesture to grant
   * mic access and unsuspend an AudioContext. */
  const toggleVoiceMode = useCallback(async () => {
    const socket = wsRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) return;

    if (voiceStatus.active) {
      micRef.current?.stop();
      micRef.current = null;
      sendVoiceControl("listen", { on: false });
      sendVoiceControl("mode", { on: false });
      setVoiceStatus((prev) => ({ ...prev, active: false, voiceMode: false }));
      return;
    }

    if (!playbackRef.current) playbackRef.current = new VoicePlaybackQueue();
    playbackRef.current.resume();

    try {
      micRef.current = await startMicCapture((frame) => {
        if (socket.readyState === WebSocket.OPEN) socket.send(frame);
      });
    } catch (e) {
      setLastError(e instanceof Error ? `Couldn't access the microphone: ${e.message}` : "Couldn't access the microphone.");
      return;
    }

    sendVoiceControl("listen", { on: true });
    sendVoiceControl("mode", { on: true });
    // Real echo cancellation is available here (getUserMedia's
    // echoCancellation constraint, unlike the raw Python voice_client.py)
    // — safe to ask for genuine barge-in instead of the half-duplex/
    // acoustic-stop-keyword workaround the raw client needs.
    sendVoiceControl("barge_in", { on: true });
    setVoiceStatus((prev) => ({ ...prev, active: true, voiceMode: true }));
  }, [voiceStatus.active, sendVoiceControl]);

  const setVoiceName = useCallback((name: string) => sendVoiceControl("voice", { name: name.toLowerCase() }), [sendVoiceControl]);
  const setSpeakerEnabled = useCallback((on: boolean) => sendVoiceControl("speaker", { on }), [sendVoiceControl]);

  /** Stops Ember speaking right now — flushes local playback immediately
   * (feels instant) and tells the server too, so a still-running turn
   * actually cancels rather than just muting this client's audio. */
  const stopSpeaking = useCallback(() => {
    playbackRef.current?.flush(Number.MAX_SAFE_INTEGER);
    setVoiceStatus((prev) => ({ ...prev, speaking: false }));
    sendVoiceControl("stop");
    cancelGeneration();
  }, [sendVoiceControl, cancelGeneration]);

  const QUERY_TIMEOUT_MS = 10000;

  const sendQuery = useCallback(<T = unknown>(name: string, params: Record<string, unknown> = {}): Promise<QueryResult<T>> => {
    const socket = wsRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return Promise.resolve({ ok: false, error: "Not connected." });
    }
    const id = makeId();
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        pendingQueriesRef.current.delete(id);
        resolve({ ok: false, error: `'${name}' timed out.` });
      }, QUERY_TIMEOUT_MS);

      pendingQueriesRef.current.set(id, (result) => {
        clearTimeout(timer);
        resolve(result as QueryResult<T>);
      });
      socket.send(JSON.stringify({ type: "query", id, name, params }));
    });
  }, []);

  const respondConfirmation = useCallback(
    (approved: boolean) => {
      const socket = wsRef.current;
      if (!socket || socket.readyState !== WebSocket.OPEN || !pendingConfirmation) return;
      socket.send(JSON.stringify({ type: "confirm", request_id: pendingConfirmation.requestId, approved }));
      setPendingConfirmation(null);
    },
    [pendingConfirmation],
  );

  const clearConversation = useCallback(() => {
    setMessages([]);
    currentAssistantIdRef.current = null;
    setIsGenerating(false);
    // Server-side history for this connection is cleared by asking Ember
    // directly — CLEAR_CONTEXT is a real recognized intent (ember_intent.py),
    // resolved locally on the backend with no model call. Sent silently
    // (not shown as a user turn) since the local reset above already gives
    // the visual "New Conversation" effect.
    const socket = wsRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "message", text: "clear context" }));
    }
  }, []);

  useEffect(() => {
    return () => {
      wsRef.current?.close();
      micRef.current?.stop();
      playbackRef.current?.close();
    };
  }, []);

  return {
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
    cancelGeneration,
    respondConfirmation,
    clearConversation,
    toggleVoiceMode,
    setVoiceName,
    setSpeakerEnabled,
    stopSpeaking,
  };
}
