import { useEffect, useRef } from "react";
import type { ChatMessage } from "../types";
import { MessageBubble } from "./MessageBubble";
import { Flame, Search, Server } from "lucide-react";

const SUGGESTIONS = [
  { label: "What's on my schedule today?", icon: Server },
  { label: "Search the web", icon: Search },
];

function greeting(): string {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning.";
  if (hour < 18) return "Good afternoon.";
  return "Good evening.";
}

interface MessageListProps {
  messages: ChatMessage[];
  onSuggestion: (text: string) => void;
}

export function MessageList({ messages, onSuggestion }: MessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  if (messages.length === 0) {
    return (
      <div className="flex h-full flex-col items-center justify-center px-6">
        <div className="mb-5 flex h-11 w-11 items-center justify-center rounded-2xl bg-ember/10 text-ember">
          <Flame size={20} />
        </div>
        <h1 className="text-[19px] font-medium text-textPrimary">{greeting()}</h1>
        <p className="mt-1.5 text-[14px] text-textSecondary">What can I help you with, sir?</p>
        <div className="mt-6 flex flex-wrap justify-center gap-2">
          {SUGGESTIONS.map(({ label, icon: Icon }) => (
            <button
              key={label}
              onClick={() => onSuggestion(label)}
              className="flex items-center gap-1.5 rounded-full border border-border px-3 py-1.5 text-[12.5px] text-textSecondary transition-colors hover:border-borderLight hover:text-textPrimary"
            >
              <Icon size={13} />
              {label}
            </button>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-[720px] flex-1 space-y-6 overflow-y-auto px-6 py-8">
      {messages.map((message) => (
        <MessageBubble key={message.id} message={message} />
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
