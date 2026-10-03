import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { AlertCircle, Loader2 } from "lucide-react";
import type { ChatMessage } from "../types";

export function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";

  if (isUser) {
    return (
      <div className="flex justify-end animate-riseIn">
        <div className="max-w-[75%] break-words [overflow-wrap:anywhere] rounded-2xl rounded-tr-sm bg-surfaceRaised px-4 py-2.5 text-[14.5px] leading-relaxed text-textPrimary">
          {message.content}
        </div>
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

      {message.grounded && message.content && (
        <div className="mt-2 inline-flex items-center gap-1.5 rounded border border-border px-1.5 py-0.5 text-[10.5px] text-textMuted">
          grounded with live search
        </div>
      )}
    </div>
  );
}
