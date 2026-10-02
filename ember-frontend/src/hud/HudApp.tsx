import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ArrowUp, Contrast, Square, X } from "lucide-react";
import type { HudState, HudTurn } from "../types";

// The HUD window. It has no backend connection of its own: the main window
// publishes a HudState snapshot over IPC (see App.tsx) and executes the
// commands sent from here (type a message, stop). Layout: your speech as
// dark-blue tabs on the right, Ember's replies as light-blue tabs on the
// left — no "You said"/"Ember said" labels, the side and colour say it.

const GLASS_KEY = "ember.hudGlass";
// Window background opacity levels (the "see-through" setting). Cards keep
// their own opacity so text stays readable at every level.
const GLASS_LEVELS = [0.18, 0.4, 0.68];

const EMPTY_STATE: HudState = {
  connected: false,
  confirmation: null,
  voiceState: null,
  awake: false,
  voiceMode: false,
  speaking: false,
  generating: false,
  warning: null,
  turns: [],
};

type OrbMode = "idle" | "listening" | "thinking" | "speaking";

function orbMode(s: HudState): OrbMode {
  if (s.speaking || s.voiceState === "speaking") return "speaking";
  if (s.generating) return "thinking";
  if (s.voiceMode || s.awake) return "listening";
  return "idle";
}

function Orb({ mode }: { mode: OrbMode }) {
  return (
    <div className="relative flex h-8 w-8 items-center justify-center" title={mode}>
      {mode === "speaking" && (
        <div className="flex h-5 items-center gap-[3px]">
          {[0, 1, 2, 3, 4].map((i) => (
            <span
              key={i}
              className="h-full w-[3px] origin-center rounded-full bg-ember"
              style={{ animation: `hudBar 0.9s ease-in-out ${i * 0.12}s infinite` }}
            />
          ))}
        </div>
      )}
      {mode === "thinking" && (
        <span
          className="h-5 w-5 rounded-full border-2 border-[#7DD3FC]/25 border-t-ember"
          style={{ animation: "hudSpin 0.9s linear infinite" }}
        />
      )}
      {mode === "listening" && (
        <>
          <span
            className="absolute h-4 w-4 rounded-full border border-[#7DD3FC]/60"
            style={{ animation: "hudRing 1.8s ease-out infinite" }}
          />
          <span className="h-2.5 w-2.5 rounded-full bg-ember shadow-[0_0_10px_rgba(225,113,47,0.7)]" />
        </>
      )}
      {mode === "idle" && <span className="h-2 w-2 rounded-full bg-ember/40" />}
    </div>
  );
}

function Corner({ className }: { className: string }) {
  return <span className={`pointer-events-none absolute h-3 w-3 border-[#7DD3FC]/45 ${className}`} />;
}

function TurnCard({ turn }: { turn: HudTurn }) {
  const isUser = turn.role === "user";
  const empty = !turn.content && !turn.error;

  return (
    <div className={`flex animate-riseIn ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`hud-text relative max-w-[88%] px-3.5 py-2.5 text-[13.5px] leading-relaxed ${
          isUser
            ? "rounded-[14px_4px_14px_14px] border border-[#60A5FA]/45 bg-[#16295F] text-[#DCE9FF]"
            : "rounded-[4px_14px_14px_14px] border border-[#7DD3FC]/50 bg-[#0F3550] text-[#EAF6FF]"
        }`}
      >
        {/* the "tab": a short accent bar sitting on the card's top edge */}
        <span
          className={`absolute -top-px h-[2px] w-7 rounded-full ${isUser ? "right-3 bg-[#3B82F6]" : "left-3 bg-[#7DD3FC]"}`}
        />

        {empty && turn.pending ? (
          turn.statusLine ? (
            <span className="text-[12.5px] text-[#BAE6FD]/80">{turn.statusLine}</span>
          ) : (
            <span className="flex h-5 items-center gap-1">
              {[0, 1, 2].map((i) => (
                <span
                  key={i}
                  className="h-1.5 w-1.5 animate-pulseDot rounded-full bg-[#7DD3FC]"
                  style={{ animationDelay: `${i * 0.2}s` }}
                />
              ))}
            </span>
          )
        ) : isUser ? (
          turn.content
        ) : (
          <div className="ember-prose hud-prose !text-[13.5px] !leading-relaxed !text-inherit">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{turn.content}</ReactMarkdown>
          </div>
        )}

        {turn.error && <div className="mt-1 text-[12px] text-[#FCA5A5]">{turn.error}</div>}
      </div>
    </div>
  );
}

export function HudApp() {
  const [state, setState] = useState<HudState>(EMPTY_STATE);
  const [value, setValue] = useState("");
  const [glassIndex, setGlassIndex] = useState(() => {
    const saved = Number(localStorage.getItem(GLASS_KEY));
    return Number.isInteger(saved) && saved >= 0 && saved < GLASS_LEVELS.length ? saved : 1;
  });
  const inputRef = useRef<HTMLInputElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const api = window.emberHud;
    if (!api) return;
    return api.onState(setState);
  }, []);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [state.turns]);

  // Esc closes the HUD (main process restores the main window).
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") window.emberHud?.sendCommand({ type: "close" });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const mode = orbMode(state);
  const busy = state.generating || state.speaking;
  const canType = state.connected && !state.generating;

  const send = () => {
    const text = value.trim();
    if (!text || !canType) return;
    window.emberHud?.sendCommand({ type: "send", text });
    setValue("");
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      send();
    }
  };

  const cycleGlass = () => {
    const next = (glassIndex + 1) % GLASS_LEVELS.length;
    setGlassIndex(next);
    localStorage.setItem(GLASS_KEY, String(next));
  };

  const panelStyle: CSSProperties = {
    background: `rgba(7, 12, 20, ${GLASS_LEVELS[glassIndex]})`,
    borderColor: "rgba(125, 211, 252, 0.28)",
  };

  const placeholder = !state.connected
    ? "Reconnecting…"
    : state.generating
      ? "Ember is replying…"
      : "Type instead…";

  return (
    <div className="h-screen w-screen p-2">
      <div className="relative flex h-full flex-col overflow-hidden rounded-[18px] border" style={panelStyle}>
        <Corner className="left-2 top-2 border-l border-t" />
        <Corner className="right-2 top-2 border-r border-t" />
        <Corner className="bottom-2 left-2 border-b border-l" />
        <Corner className="bottom-2 right-2 border-b border-r" />

        {/* Drag strip: orb on the left, small controls on the right */}
        <div className="hud-drag flex h-11 shrink-0 items-center justify-between px-4">
          <Orb mode={mode} />
          <div className="hud-nodrag flex items-center gap-1">
            {busy && (
              <button
                onClick={() => window.emberHud?.sendCommand({ type: "stop" })}
                className="flex h-7 w-7 items-center justify-center rounded-lg text-[#BAE6FD] hover:bg-white/10"
                aria-label="Stop"
                title="Stop"
              >
                <Square size={12} fill="currentColor" />
              </button>
            )}
            <button
              onClick={cycleGlass}
              className="flex h-7 w-7 items-center justify-center rounded-lg text-[#BAE6FD]/70 hover:bg-white/10 hover:text-[#BAE6FD]"
              aria-label="Change transparency"
              title="Transparency"
            >
              <Contrast size={14} />
            </button>
            <button
              onClick={() => window.emberHud?.sendCommand({ type: "close" })}
              className="flex h-7 w-7 items-center justify-center rounded-lg text-[#BAE6FD]/70 hover:bg-white/10 hover:text-[#BAE6FD]"
              aria-label="Close HUD"
              title="Close (Esc)"
            >
              <X size={15} />
            </button>
          </div>
        </div>

        {/* Conversation — older turns fade out at the top */}
        <div className="hud-fade min-h-0 flex-1 overflow-y-auto px-4 pb-2">
          <div className="flex min-h-full flex-col justify-end gap-3">
            {state.turns.map((turn) => (
              <TurnCard key={turn.id} turn={turn} />
            ))}
            {state.warning && <div className="hud-text px-1 text-[12px] text-[#FDE68A]/90">{state.warning}</div>}
            <div ref={bottomRef} />
          </div>
        </div>

        {/* Approval card: answer by voice ("yes"/"no") or with these buttons */}
        {state.confirmation && (
          <div className="shrink-0 px-4 pb-2">
            <div className="hud-text rounded-[14px] border border-ember/60 bg-[#2A1A10] px-3.5 py-2.5">
              <div className="text-[13px] text-[#FFE7D2]">{state.confirmation.summary}</div>
              <div className="mt-1 text-[11px] text-[#F2A65A]/80">Say yes or no, sir.</div>
              <div className="hud-nodrag mt-2 flex gap-2">
                <button
                  onClick={() => window.emberHud?.sendCommand({ type: "confirm", approved: false })}
                  className="flex-1 rounded-lg border border-[#7DD3FC]/30 py-1.5 text-[12.5px] text-[#EAF6FF] hover:bg-white/10"
                >
                  Deny
                </button>
                <button
                  onClick={() => window.emberHud?.sendCommand({ type: "confirm", approved: true })}
                  className="flex-1 rounded-lg bg-ember py-1.5 text-[12.5px] font-medium text-base"
                >
                  Approve
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Small text box — replies to typed text are spoken too */}
        <div className="shrink-0 px-4 pb-4 pt-1">
          <div className="flex items-center gap-2 rounded-full border border-[#7DD3FC]/25 bg-[#050A12]/55 py-1 pl-4 pr-1.5">
            <input
              ref={inputRef}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={onKeyDown}
              disabled={!state.connected}
              placeholder={placeholder}
              className="hud-nodrag min-w-0 flex-1 bg-transparent py-1.5 text-[13px] text-[#EAF6FF] placeholder:text-[#7DD3FC]/45 focus:outline-none"
            />
            <button
              onClick={send}
              disabled={!value.trim() || !canType}
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-ember text-base transition-opacity disabled:opacity-25"
              aria-label="Send"
            >
              <ArrowUp size={14} strokeWidth={2.5} />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
