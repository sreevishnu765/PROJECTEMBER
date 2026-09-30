import type { OutgoingAttachment } from "../types";

// Keep these in step with ember_attachments.py — the server re-checks all
// of them, so this is only about failing fast and clearly in the UI.
export const MAX_FILES = 5;
export const MAX_FILE_BYTES = 8 * 1024 * 1024;
export const MAX_TOTAL_BYTES = 15 * 1024 * 1024;
const BLOCKED_EXT = new Set(["exe", "dll", "msi", "scr", "com", "bat", "cmd", "vbs", "lnk"]);

const extOf = (name: string): string => (name.includes(".") ? name.split(".").pop()!.toLowerCase() : "");

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function checkFiles(existing: File[], incoming: File[]): { accepted: File[]; rejected: string[] } {
  const accepted: File[] = [];
  const rejected: string[] = [];
  let count = existing.length;
  let total = existing.reduce((sum, f) => sum + f.size, 0);
  for (const f of incoming) {
    if (BLOCKED_EXT.has(extOf(f.name))) rejected.push(`${f.name}: that file type isn't allowed.`);
    else if (f.size === 0) rejected.push(`${f.name} is empty.`);
    else if (f.size > MAX_FILE_BYTES) rejected.push(`${f.name} is over ${formatSize(MAX_FILE_BYTES)}.`);
    else if (count >= MAX_FILES) rejected.push(`Max ${MAX_FILES} files per message.`);
    else if (total + f.size > MAX_TOTAL_BYTES) rejected.push(`Attachments can total at most ${formatSize(MAX_TOTAL_BYTES)}.`);
    else {
      accepted.push(f);
      count += 1;
      total += f.size;
    }
  }
  return { accepted, rejected };
}

function readAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result);
      resolve(result.slice(result.indexOf(",") + 1)); // strip "data:...;base64,"
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

export async function encodeFiles(files: File[]): Promise<OutgoingAttachment[]> {
  return Promise.all(
    files.map(async (f) => ({
      name: f.name,
      mime: f.type || "application/octet-stream",
      data: await readAsBase64(f),
    })),
  );
}
