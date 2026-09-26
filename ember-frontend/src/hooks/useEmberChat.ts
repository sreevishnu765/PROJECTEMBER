import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatMessage, ConnectionState, PendingConfirmation, QueryResult, ServerMessage } from "../types";
import { isGroundedTag, parseProviderFamily } from "../lib/providerTag";

const URL_STORAGE_KEY = "ember.serverUrl";
const TOKEN_STORAGE_KEY = "ember.token";

function makeId(): string {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

export function useEmberChat() {
  const [connectionState, setConnectionState] = useState<ConnectionState>("disconnected");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isGenerating, setIsGenerating] = useState(false);
  const [pendingConfirmation, setPendingConfirmation] = useState<PendingConfirmation | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);

  const [savedUrl] = useState(() => localStorage.getItem(URL_STORAGE_KEY) ?? "");
  const [savedToken] = useState(() => localStorage.getItem(TOKEN_STORAGE_KEY) ?? "");

  const wsRef = useRef<WebSocket | null>(null);
  const currentAssistantIdRef = useRef<string | null>(null);
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
        case "auth_ok":
          setConnectionState("connected");
          break;
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
    connect,
    sendMessage,
    sendQuery,
    cancelGeneration,
    respondConfirmation,
    clearConversation,
  };
}
