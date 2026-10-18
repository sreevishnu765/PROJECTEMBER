import { useEffect, useRef } from "react";
import type { ChatMessage } from "../types";
import { MessageBubble } from "./MessageBubble";

function greeting(): string {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning.";
  if (hour < 18) return "Good afternoon.";
  return "Good evening.";
}

interface MessageListProps {
  messages: ChatMessage[];
}

export function MessageList({ messages }: MessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  if (messages.length === 0) {
    return (
      <div className="flex h-full flex-col items-center justify-center px-6">
        <h1 className="ember-serif text-[34px] leading-tight text-textPrimary">{greeting()}</h1>
        <p className="mt-1.5 text-[14px] text-textSecondary">What can I help you with, sir?</p>
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
