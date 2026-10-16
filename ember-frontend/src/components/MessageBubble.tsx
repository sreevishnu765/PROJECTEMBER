import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useEffect, useRef, useState } from "react";
import { AlertCircle, Check, Copy, Loader2 } from "lucide-react";
import type { ChatMessage } from "../types";

async function writeClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Older/locked-down contexts: fall back to a hidden textarea + execCommand.
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch {
      return false;
    }
  }
}

/** Small "Copy" control under a message. Copies the raw text (Markdown source for Ember's replies). */
function CopyButton({ text, align = "left" }: { text: string; align?: "left" | "right" }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  const onClick = async () => {
    if (!(await writeClipboard(text))) return;
    setCopied(true);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className={`mt-1 flex ${align === "right" ? "justify-end" : "justify-start"}`}>
      <button
        type="button"
        onClick={onClick}
        title={copied ? "Copied" : "Copy"}
        aria-label={copied ? "Copied" : "Copy message"}
        className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11.5px] text-textMuted transition-colors hover:bg-surfaceRaised hover:text-textPrimary"
      >
        {copied ? <Check size={12} className="text-ember" /> : <Copy size={12} />}
        <span>{copied ? "Copied" : "Copy"}</span>
      </button>
    </div>
  );
}

export function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";

  if (isUser) {
    return (
      <div className="flex flex-col items-end animate-riseIn">
        <div className="max-w-[75%] break-words [overflow-wrap:anywhere] rounded-2xl rounded-tr-sm bg-surfaceRaised px-4 py-2.5 text-[14.5px] leading-relaxed text-textPrimary">
          {message.content}
        </div>
        {message.content && <CopyButton text={message.content} align="right" />}
      </div>
    );
  }

  return (
    <div className="animate-riseIn">
      {message.statusLine && (
        <div className="mb-2 flex items-center gap-2 text-[13px] text-textSecondary">
          <Loader2 size={13} className="animate-spin text-ember" />
          <span>{message.statusLine}</span>
        </div>
      )}

      {message.content && (
        <div className="ember-prose max-w-none">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
        </div>
      )}

      {message.error && (
        <div className="mt-2 flex items-start gap-2 rounded-lg border border-statusOffline/30 bg-statusOffline/10 px-3 py-2 text-[13px] text-textSecondary">
          <AlertCircle size={14} className="mt-0.5 shrink-0 text-statusOffline" />
          <span>{message.error}</span>
        </div>
      )}

      {message.content && !message.pending && <CopyButton text={message.content} />}

      {message.grounded && message.content && (
        <div className="mt-2 inline-flex items-center gap-1.5 rounded border border-border px-1.5 py-0.5 text-[10.5px] text-textMuted">
          grounded with live search
        </div>
      )}
    </div>
  );
}
