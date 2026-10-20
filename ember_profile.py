"""Who the user is (data/profile.md) and how Ember sounds (skills/persona.md), both plain files you can edit by hand.

profile.md: a short list of lasting facts -- course, semester, subjects, routines, the scooty, how you like to be
spoken to. It is put in front of Ember on EVERY turn (not recalled by similarity like memories are), so she never has
to hope a stored fact happens to resemble the question. Kept small on purpose: it is sent with each message.

persona.md: her voice, with example exchanges. Loaded fresh whenever the file changes, so tuning her is editing a
text file, not code. The grounding rules (never invent a personal fact, never claim an action without a tool result)
stay in BASE_PERSONA in ember_core.py and outrank anything in the file.
"""
import os
import re
import threading
from typing import Optional

_ROOT = os.path.dirname(os.path.abspath(__file__))
PROFILE_PATH = os.path.join(_ROOT, "data", "profile.md")
PERSONA_PATH = os.path.join(_ROOT, "skills", "persona.md")
MAX_PROFILE_CHARS = 3000
MAX_PERSONA_CHARS = 6000


class Profile:
    def __init__(self, path: str = PROFILE_PATH):
        self.path = path
        self._lock = threading.Lock()

    def lines(self) -> "list[str]":
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return [ln.rstrip() for ln in f.read().splitlines() if ln.strip()]
        except OSError:
            return []

    def _write(self, lines: "list[str]") -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
        os.replace(tmp, self.path)

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9 ]+", "", s.lower()).strip()

    def add(self, fact: str) -> "tuple[bool, str]":
        fact = re.sub(r"\s+", " ", fact).strip(" .\t-")
        if not fact:
            return False, "Tell me what to add, sir."
        line = "- " + fact[0].upper() + fact[1:]
        with self._lock:
            lines = self.lines()
            if any(self._norm(l.lstrip("- ")) == self._norm(fact) for l in lines):
                return False, "That's already in your profile, sir."
            if sum(len(l) + 1 for l in lines) + len(line) > MAX_PROFILE_CHARS:
                return False, ("Your profile is full, sir — it's sent with every message, so I keep it short. "
                               "Remove something first, or trim data/profile.md.")
            try:
                self._write(lines + [line])
            except OSError as e:
                return False, f"Couldn't write your profile, sir: {e}"
        return True, line[2:]

    def find(self, query: str) -> "list[str]":
        from ember_memory import content_tokens
        q = content_tokens(query)
        if not q:
            return []
        lines = self.lines()
        tight = [l for l in lines if q <= content_tokens(l)]
        return tight or []

    def remove(self, line: str) -> bool:
        with self._lock:
            lines = self.lines()
            kept = [l for l in lines if l != line]
            if len(kept) == len(lines):
                return False
            try:
                self._write(kept)
            except OSError:
                return False
            return True

    def block(self) -> str:
        lines = self.lines()
        if not lines:
            return ""
        text = "\n".join(lines)
        if len(text) > MAX_PROFILE_CHARS:
            text = text[:MAX_PROFILE_CHARS].rsplit("\n", 1)[0]
        return ("The user's own profile (a file they keep; authoritative, and the first place to look for a personal "
                "fact about them). Use it naturally; never recite it:\n" + text)


_persona_cache: "dict[str, tuple[float, str]]" = {}


def persona_block(path: str = PERSONA_PATH) -> str:
    """The contents of persona.md ("" if absent/empty), re-read only when the file changes."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    cached = _persona_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()[:MAX_PERSONA_CHARS]
    except OSError:
        text = ""
    _persona_cache[path] = (mtime, text)
    return text
