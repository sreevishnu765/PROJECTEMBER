import type { PendingConfirmation } from "../types";

/** Last component of a Windows/POSIX path ("C:\Users\x\Downloads" -> "Downloads"). */
function lastPart(raw: string): string {
  const parts = raw.trim().replace(/^['"]|['".,!?]+$/g, "").split(/[\\/]+/).filter(Boolean);
  const last = parts[parts.length - 1] ?? "";
  if (/^[A-Za-z]:$/.test(last)) return `${last[0].toUpperCase()} drive`;
  return last || "that";
}

/** One short line for the HUD approval card. Full paths stay in the main-window dialog. */
export function describeConfirmation(c: PendingConfirmation): string {
  const name = c.toolName.toLowerCase();
  const args = c.args ?? {};
  const matched = typeof args.matched_text === "string" ? args.matched_text : "";
  if (name === "allow_path") {
    const m = /access(?:\s+to)?\s+(.+)$/i.exec(matched);
    return `Allow access to ${m ? lastPart(m[1]) : "that folder"}?`;
  }
  if (name === "delete_file") {
    return `Move ${lastPart(String(args.file ?? ""))} to the trash?`;
  }
  if (name === "run_script") return "Run that script?";
  if (name === "clear_memory") return "Wipe all stored memories?";
  if (name === "forget_memory") return "Forget that?";
  if (name.startsWith("cancel_calendar")) return "Cancel that calendar event?";
  return `Go ahead with ${name.replace(/_/g, " ")}?`;
}
