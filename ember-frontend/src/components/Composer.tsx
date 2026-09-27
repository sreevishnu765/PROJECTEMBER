import { useRef, useState, type KeyboardEvent } from "react";
import { ArrowUp, Mic, Paperclip, Square, Waves } from "lucide-react";
import type { VoiceStatus } from "../hooks/useEmberChat";

interface ComposerProps {
  disabled: boolean;
  isGenerating: boolean;
  onSend: (text: string) => void;
  onCancel: () => void;
  voiceStatus: VoiceStatus;
  onToggleVoice: () => void;
}

export function Composer({ disabled, isGenerating, onSend, onCancel, voiceStatus, onToggleVoice }: ComposerProps) {
  const [value, setValue] = useState("");
  const [placeholderNotice, setPlaceholderNotice] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const noticeTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleSend = () => {
    if (!value.trim() || disabled) return;
    onSend(value);
    setValue("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const autoGrow = (el: HTMLTextAreaElement) => {
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  };

  // HUD and voice input are both real, planned capabilities — just not
  // wired up yet (HUD is built right after the voice pass, since its whole
  // purpose is spoken interaction). Showing a plain, honest notice instead
  // of a dead button or a fake modal.
  const showPlaceholderNotice = (text: string) => {
    if (noticeTimeoutRef.current) clearTimeout(noticeTimeoutRef.current);
    setPlaceholderNotice(text);
    noticeTimeoutRef.current = setTimeout(() => setPlaceholderNotice(null), 2200);
  };

  return (
    <div className="relative shrink-0 px-6 pb-6 pt-2">
      {placeholderNotice && (
        <div className="pointer-events-none absolute bottom-[76px] left-1/2 -translate-x-1/2 animate-riseIn rounded-lg border border-border bg-surfaceRaised px-3 py-1.5 text-[12px] text-textSecondary shadow-lg">
          {placeholderNotice}
        </div>
      )}
      <div className="mx-auto flex w-full max-w-[720px] items-end gap-2 rounded-2xl border border-border bg-surface px-3 py-2.5 focus-within:border-borderLight">
        <button
          type="button"
          disabled
          title="Attach a file — not wired up yet"
          className="mb-0.5 shrink-0 rounded-lg p-1.5 text-textMuted"
        >
          <Paperclip size={17} />
        </button>

        <textarea
          ref={textareaRef}
          value={value}
          disabled={disabled}
          onChange={(e) => {
            setValue(e.target.value);
            autoGrow(e.target);
          }}
          onKeyDown={handleKeyDown}
          placeholder={disabled ? "Connect to Ember to start chatting" : "Message Ember…"}
          rows={1}
          className="max-h-40 flex-1 resize-none bg-transparent py-1 text-[14.5px] leading-relaxed text-textPrimary placeholder:text-textMuted focus:outline-none disabled:cursor-not-allowed"
        />

        <button
          type="button"
          onClick={() => showPlaceholderNotice("Voice HUD arrives with the voice pass — not wired up yet.")}
          title="Voice HUD — coming soon"
          className="mb-0.5 shrink-0 rounded-lg p-1.5 text-textMuted transition-colors hover:bg-surfaceRaised hover:text-textSecondary"
        >
          <Waves size={17} />
        </button>

        <button
          type="button"
          onClick={onToggleVoice}
          disabled={disabled}
          title={voiceStatus.active ? "Turn voice mode off" : "Turn voice mode on"}
          aria-pressed={voiceStatus.active}
          className={`relative mb-0.5 shrink-0 rounded-lg p-1.5 transition-colors disabled:opacity-30 ${
            voiceStatus.active
              ? "bg-ember/15 text-ember hover:bg-ember/20"
              : "text-textMuted hover:bg-surfaceRaised hover:text-textSecondary"
          }`}
        >
          <Mic size={17} />
          {voiceStatus.active && voiceStatus.awake && (
            <span className="absolute right-0.5 top-0.5 h-1.5 w-1.5 animate-pulseDot rounded-full bg-statusCloud" />
          )}
        </button>

        {isGenerating ? (
          <button
            onClick={onCancel}
            className="mb-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-surfaceRaised text-textPrimary hover:bg-borderLight"
            aria-label="Stop generating"
          >
            <Square size={13} fill="currentColor" />
          </button>
        ) : (
          <button
            onClick={handleSend}
            disabled={disabled || !value.trim()}
            className="mb-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-ember text-base transition-opacity disabled:opacity-30"
            aria-label="Send message"
          >
            <ArrowUp size={16} strokeWidth={2.5} />
          </button>
        )}
      </div>
    </div>
  );
}
