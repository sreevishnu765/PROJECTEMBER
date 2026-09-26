import type { ProviderFamily } from "../types";

/**
 * process_turn() returns tags like "gemini-3.5-flash+search" (cloud),
 * "cerebras:gpt-oss-120b" (fallback), "local+search" (local), or a plain
 * command/action name ("get_time", "action:launch_app", "cancelled",
 * "error") that isn't a provider answer at all. This derives the header
 * dot's color straight from that string — never invented separately.
 */
export function parseProviderFamily(tag: string | null): ProviderFamily | null {
  if (!tag) return null;
  const base = tag.split("+search")[0];
  if (base === "local") return "local";
  if (base.includes(":")) return "fallback"; // "cerebras:gpt-oss-120b", "groq:openai/gpt-oss-20b", ...
  if (base.startsWith("gemini")) return "cloud";
  return null; // command/action/error tags — no provider to show
}

export function isGroundedTag(tag: string | null): boolean {
  return !!tag && tag.includes("+search");
}
