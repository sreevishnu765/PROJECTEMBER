"""
ember_files.py
================
Lightweight, JSON-backed registry of files Ember has touched — the
missing piece behind a real Files panel. Before this: analyze_document,
analyze_screenshot, generate_pdf, and download_drive_link each did their
job and returned a message, but nothing anywhere kept a list of what
happened. A UI panel asking "what files exist" had nothing to query —
the work was real, it just left no trace anywhere queryable.

Same storage shape as ember_reminders.py's ReminderStore: a small JSON
file, loaded once, rewritten on every change. No embedding, no search,
no pruning by relevance — this is a plain append-mostly log, capped by
count only (FILE_LOG_CAP), same "bounded by design, not a silent
transcript archive" reasoning as ember_session.py's SESSION_TURN_CAP.

Two kinds, matching the Files panel's two sections:
  "upload"    -- something the user handed Ember to look at
                 (analyze_document / analyze_screenshot). category is
                 always "analyzed".
  "generated" -- something Ember produced. category distinguishes
                 "pdf_export" from "drive_download".

path is Optional deliberately: a Drive download's real destination path
has to be scraped out of a plain success message today (see
ember_core.py's _download_drive handler — drive.py itself only returns
text, not a structured path, unlike calendar.py's add_event which
already returns (message, event_id)). When that scrape fails, path is
None and the panel falls back to showing the label text alone rather
than a broken/missing entry — same "degrade honestly, don't drop the
row" contract as everything else in this project.
"""

import json
import os
import threading
import time
import uuid

FILE_LOG_CAP = 500


class FileRegistry:
    """Thread-safe, JSON-backed log of file activity. Never raises to the
    caller — a failed write degrades to 'this entry won't show up in the
    Files panel', never to a broken tool call, same graceful-degradation
    contract as ember_reminders.py's ReminderStore."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._entries: "list[dict]" = self._load()

    def _load(self) -> "list[dict]":
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[ember_files] Couldn't load {self.path} ({e}) — starting with an empty file log rather than crashing.")
            return []

    def _save_locked(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._entries, f, indent=2)
        except OSError as e:
            print(f"[ember_files] Warning: couldn't persist the file log ({e}).")

    def record(self, kind: str, category: str, path: "str | None", label: "str | None" = None) -> dict:
        entry = {
            "id": uuid.uuid4().hex[:8],
            "kind": kind,          # "upload" | "generated"
            "category": category,  # "analyzed" | "pdf_export" | "drive_download"
            "path": path,
            "label": label or (os.path.basename(path) if path else "unknown"),
            "created_at": time.time(),
        }
        with self._lock:
            self._entries.append(entry)
            del self._entries[:-FILE_LOG_CAP]
            self._save_locked()
        return entry

    def update_path(self, old: str, new: str) -> int:
        """A file Ember knows about was moved/renamed: point its entries at the new
        location (and refresh the label) instead of leaving a dead path in the panel."""
        changed = 0
        with self._lock:
            for e in self._entries:
                if e.get("path") and os.path.normcase(e["path"]) == os.path.normcase(old):
                    e["path"] = new
                    e["label"] = os.path.basename(new)
                    changed += 1
            if changed:
                self._save_locked()
        return changed

    def remove_path(self, path: str) -> int:
        """A file Ember knew about was deleted: drop its entries."""
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if not (e.get("path") and os.path.normcase(e["path"]) == os.path.normcase(path))]
            removed = before - len(self._entries)
            if removed:
                self._save_locked()
        return removed

    @staticmethod
    def _norm(p: str) -> str:
        return os.path.normcase(os.path.normpath(p))

    def update_prefix(self, old_dir: str, new_dir: str) -> int:
        """A FOLDER was moved/renamed: every recorded file that lived inside it follows."""
        old_n = self._norm(old_dir) + os.sep
        changed = 0
        with self._lock:
            for e in self._entries:
                p = e.get("path")
                if p and (self._norm(p) + os.sep).startswith(old_n):
                    e["path"] = os.path.join(new_dir, os.path.relpath(self._norm(p), self._norm(old_dir)))
                    changed += 1
            if changed:
                self._save_locked()
        return changed

    def remove_prefix(self, dir_path: str) -> int:
        """A FOLDER was deleted: drop every recorded file that lived inside it."""
        old_n = self._norm(dir_path) + os.sep
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if not (e.get("path") and (self._norm(e["path"]) + os.sep).startswith(old_n))]
            removed = before - len(self._entries)
            if removed:
                self._save_locked()
        return removed

    def get(self, entry_id: str) -> "dict | None":
        with self._lock:
            return next((dict(e) for e in self._entries if e.get("id") == entry_id), None)

    def list_all(self, kind: "str | None" = None) -> "list[dict]":
        with self._lock:
            entries = list(self._entries)
        if kind:
            entries = [e for e in entries if e["kind"] == kind]
        return sorted(entries, key=lambda e: e["created_at"], reverse=True)
