import { useRef, useState, type ClipboardEvent, type DragEvent, type KeyboardEvent } from "react";
import { ArrowUp, Ear, EarOff, FileText, Mic, Paperclip, Square, Waves, X } from "lucide-react";
import type { VoiceStatus } from "../hooks/useEmberChat";
import { checkFiles, formatSize } from "../lib/attachments";

interface ComposerProps {
  disabled: boolean;
  isGenerating: boolean;
  onSend: (text: string, files: File[]) => void;
  onCancel: () => void;
  voiceStatus: VoiceStatus;
  onToggleVoice: () => void;
  onToggleListening: () => void;
  onOpenHud: () => void;
}

export function Composer({ disabled, isGenerating, onSend, onCancel, voiceStatus, onToggleVoice, onToggleListening, onOpenHud }: ComposerProps) {
  const [value, setValue] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const [placeholderNotice, setPlaceholderNotice] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const noticeTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const canSend = !disabled && (value.trim().length > 0 || files.length > 0);

  const handleSend = () => {
    if (!canSend) return;
    onSend(value, files);
    setValue("");
    setFiles([]);
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

  // Short-lived notice above the composer (HUD unavailable, rejected files, ...).
  const showPlaceholderNotice = (text: string, ms = 2200) => {
    if (noticeTimeoutRef.current) clearTimeout(noticeTimeoutRef.current);
    setPlaceholderNotice(text);
    noticeTimeoutRef.current = setTimeout(() => setPlaceholderNotice(null), ms);
  };

  const addFiles = (incoming: File[]) => {
    if (disabled || incoming.length === 0) return;
    const { accepted, rejected } = checkFiles(files, incoming);
    if (accepted.length > 0) setFiles((prev) => [...prev, ...accepted]);
    if (rejected.length > 0) showPlaceholderNotice(rejected[0], 3800);
  };

  const handlePaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const pasted = Array.from(e.clipboardData.files);
    if (pasted.length === 0) return; // plain text paste: leave alone
    e.preventDefault();
    addFiles(pasted);
  };

  const handleDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragging(false);
    addFiles(Array.from(e.dataTransfer.files));
  };

  return (
    <div className="relative shrink-0 px-6 pb-6 pt-2">
      {placeholderNotice && (
        <div className="pointer-events-none absolute bottom-[76px] left-1/2 -translate-x-1/2 animate-riseIn rounded-lg border border-border bg-surfaceRaised px-3 py-1.5 text-[12px] text-textSecondary shadow-lg">
          {placeholderNotice}
        </div>
      )}
      <div
        onDragOver={(e) => {
          e.preventDefault();
          if (!disabled) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        className={`mx-auto w-full max-w-[720px] rounded-2xl border bg-surface focus-within:border-borderLight ${
          dragging ? "border-ember" : "border-border"
        }`}
      >
        {files.length > 0 && (
          <div className="flex flex-wrap gap-1.5 px-3 pt-2.5">
            {files.map((f, i) => (
              <span
                key={`${f.name}-${i}`}
                className="flex max-w-[220px] items-center gap-1.5 rounded-lg border border-border bg-surfaceRaised py-1 pl-2 pr-1 text-[12px] text-textSecondary"
              >
                <FileText size={13} className="shrink-0 text-ember" />
                <span className="truncate">{f.name}</span>
                <span className="shrink-0 text-textMuted">{formatSize(f.size)}</span>
                <button
                  type="button"
                  onClick={() => setFiles((prev) => prev.filter((_, idx) => idx !== i))}
                  className="shrink-0 rounded p-0.5 text-textMuted hover:bg-borderLight hover:text-textPrimary"
                  aria-label={`Remove ${f.name}`}
                >
                  <X size={12} />
                </button>
              </span>
            ))}
          </div>
        )}

        <div className="flex items-end gap-2 px-3 py-2.5">
          <input
            ref={fileInputRef}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              addFiles(Array.from(e.target.files ?? []));
              e.target.value = ""; // allow re-picking the same file
            }}
          />
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={disabled}
            title="Attach files (or drop / paste them)"
            className="mb-0.5 shrink-0 rounded-lg p-1.5 text-textMuted transition-colors hover:bg-surfaceRaised hover:text-textSecondary disabled:opacity-30"
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
            onPaste={handlePaste}
            placeholder={disabled ? "Connect to Ember to start chatting" : files.length ? "Add a message, or just send…" : "Message Ember…"}
            rows={1}
            className="max-h-40 flex-1 resize-none bg-transparent py-1 text-[14.5px] leading-relaxed text-textPrimary placeholder:text-textMuted focus:outline-none disabled:cursor-not-allowed"
          />

          <button
            type="button"
            onClick={() => {
              if (window.emberHud) onOpenHud();
              else showPlaceholderNotice("The HUD needs the desktop app.");
            }}
            disabled={disabled}
            title="Open the voice HUD"
            className="mb-0.5 shrink-0 rounded-lg p-1.5 text-textMuted transition-colors hover:bg-surfaceRaised hover:text-textSecondary disabled:opacity-30"
          >
            <Waves size={17} />
          </button>

          <button
            type="button"
            onClick={onToggleListening}
            disabled={disabled}
            title={voiceStatus.active ? "Listening for \"Hey Ember\" — click to mute" : "Mic muted — click to listen for the wake word"}
            aria-pressed={!voiceStatus.active}
            className="mb-0.5 shrink-0 rounded-lg p-1.5 text-textMuted transition-colors hover:bg-surfaceRaised hover:text-textSecondary disabled:opacity-30"
          >
            {voiceStatus.active ? <Ear size={17} /> : <EarOff size={17} />}
          </button>

          <button
            type="button"
            onClick={onToggleVoice}
            disabled={disabled}
            title={voiceStatus.voiceMode ? "Turn voice mode off" : "Turn voice mode on (no wake word needed)"}
            aria-pressed={voiceStatus.voiceMode}
            className={`relative mb-0.5 shrink-0 rounded-lg p-1.5 transition-colors disabled:opacity-30 ${
              voiceStatus.voiceMode
                ? "bg-ember/15 text-ember hover:bg-ember/20"
                : "text-textMuted hover:bg-surfaceRaised hover:text-textSecondary"
            }`}
          >
            <Mic size={17} />
            {voiceStatus.voiceMode && voiceStatus.awake && (
              <span className="absolute right-0.5 top-0.5 h-1.5 w-1.5 animate-pulseDot rounded-full bg-statusCloud" />
            )}
          </button>

          {isGenerating || voiceStatus.speaking ? (
            <button
              onClick={onCancel}
              className="mb-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-surfaceRaised text-textPrimary hover:bg-borderLight"
              aria-label="Stop"
            >
              <Square size={13} fill="currentColor" />
            </button>
          ) : (
            <button
              onClick={handleSend}
              disabled={!canSend}
              className="mb-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-ember text-base transition-opacity disabled:opacity-30"
              aria-label="Send message"
            >
              <ArrowUp size={16} strokeWidth={2.5} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
