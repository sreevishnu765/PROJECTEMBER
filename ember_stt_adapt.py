"""
ember_stt_adapt.py
===================
Makes speech recognition adapt to YOU without retraining anything.

Whisper can't be fine-tuned on a laptop, but three cheap things move accuracy a lot for an accent and for
names/complex words, and all three live here:

  1. A vocabulary file you own (data/stt_vocabulary.txt). Every term is fed to Whisper as a hotword and in
     its prompt, so "Verstappen" is a word it has been told to expect rather than one it must guess.
  2. A correction map for mishearings that are consistent for your voice. One line per mishearing:
         max will stop => Verstappen
     Whole-word, case-insensitive, applied to every transcript before anything else sees it.
  3. A conservative fuzzy pass: a heard word that is *nearly* a vocabulary term (same first letter, very high
     spelling similarity, term >= 5 letters) is replaced by the term. "Verstapen" -> "Verstappen".

The file is re-read when it changes, so edits apply without a restart. Say "when you hear X I mean Y" or
"add Y to your vocabulary" to Ember and it writes the same file.

File format (data/stt_vocabulary.txt):
    # comment
    Verstappen            <- a term (hotword + fuzzy target)
    max will stop => Verstappen   <- a fixed correction (the right-hand side is also a term)
"""

import difflib
import os
import re
import threading

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.environ.get("EMBER_STT_VOCAB", os.path.join(_PROJECT_ROOT, "data", "stt_vocabulary.txt"))

SEED = """\
# Ember speech vocabulary — one term per line, or "heard => meant".
# Terms are given to Whisper as hotwords and fuzzy-matched against what it writes.
# Edits apply immediately (no restart). Lines starting with # are ignored.
Ember
Verstappen
"""

MAX_PROMPT_TERMS = 40        # Whisper's prompt window is ~224 tokens; stay well inside it
FUZZY_MIN_TERM_LEN = 5
FUZZY_MIN_RATIO = 0.84
_WORD_RE = re.compile(r"[A-Za-z0-9']+")


class Vocabulary:
    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._mtime = None
        self.terms: "list[str]" = []
        self.corrections: "list[tuple[re.Pattern, str]]" = []
        self._ensure_file()
        self.reload(force=True)

    # ---- file handling -------------------------------------------------
    def _ensure_file(self) -> None:
        try:
            if not os.path.exists(self.path):
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(self.path, "w", encoding="utf-8") as f:
                    f.write(SEED)
        except OSError as e:
            print(f"[ember_stt_adapt] couldn't create {self.path}: {e}")

    def reload(self, force: bool = False) -> None:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if not force and mtime == self._mtime:
            return
        terms, corrections = [], []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError as e:
            print(f"[ember_stt_adapt] couldn't read {self.path}: {e}")
            return
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=>" in line:
                heard, meant = (p.strip() for p in line.split("=>", 1))
                if heard and meant:
                    pat = re.compile(r"(?<![A-Za-z0-9'])" + r"\s+".join(re.escape(w) for w in heard.split()) + r"(?![A-Za-z0-9'])", re.IGNORECASE)
                    corrections.append((pat, meant))
                    line = meant
                else:
                    continue
            if line and line.lower() not in {t.lower() for t in terms}:
                terms.append(line)
        with self._lock:
            self.terms, self.corrections, self._mtime = terms, corrections, mtime

    def add(self, term: str = "", heard: str = "") -> str:
        """Append a term or a correction to the file. Returns a short status string."""
        term, heard = term.strip().strip("\"'"), heard.strip().strip("\"'")
        if not term:
            return "nothing to add"
        line = f"{heard} => {term}" if heard else term
        self.reload()
        if any(t.lower() == line.lower() for t in self.terms) and not heard:
            return "already there"
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(("" if self._ends_with_newline() else "\n") + line + "\n")
        except OSError as e:
            return f"couldn't write the vocabulary file ({e})"
        self.reload(force=True)
        return "added"

    def _ends_with_newline(self) -> bool:
        try:
            with open(self.path, "rb") as f:
                f.seek(0, os.SEEK_END)
                if f.tell() == 0:
                    return True
                f.seek(-1, os.SEEK_END)
                return f.read(1) == b"\n"
        except OSError:
            return True

    # ---- what Whisper is given ------------------------------------------
    def hotwords(self) -> str:
        self.reload()
        return " ".join(self.terms[:MAX_PROMPT_TERMS])

    def prompt(self) -> str:
        self.reload()
        terms = self.terms[:MAX_PROMPT_TERMS]
        return ("Glossary: " + ", ".join(terms) + ".") if terms else ""

    # ---- what we do with what Whisper wrote ------------------------------
    def correct(self, text: str) -> str:
        if not text:
            return text
        self.reload()
        with self._lock:
            corrections, terms = list(self.corrections), list(self.terms)
        for pat, meant in corrections:
            text = pat.sub(meant, text)
        return self._fuzzy(text, terms)

    @staticmethod
    def _fuzzy(text: str, terms: "list[str]") -> str:
        singles = [t for t in terms if " " not in t and len(t) >= FUZZY_MIN_TERM_LEN]
        if not singles:
            return text
        known = {t.lower() for t in terms}
        out, last = [], 0
        for m in _WORD_RE.finditer(text):
            w = m.group(0)
            lw = w.lower()
            if lw in known or len(lw) < FUZZY_MIN_TERM_LEN - 1:
                continue
            best, best_r = None, 0.0
            for t in singles:
                lt = t.lower()
                if lt[0] != lw[0] or abs(len(lt) - len(lw)) > 2:
                    continue
                r = difflib.SequenceMatcher(None, lw, lt).ratio()
                if r > best_r:
                    best, best_r = t, r
            if best and best_r >= FUZZY_MIN_RATIO:
                out.append((m.start(), m.end(), best))
        for s, e, rep in reversed(out):
            text = text[:s] + rep + text[e:]
        return text


_vocab = None
_vocab_lock = threading.Lock()


def get_vocabulary() -> Vocabulary:
    global _vocab
    with _vocab_lock:
        if _vocab is None:
            _vocab = Vocabulary()
        return _vocab
