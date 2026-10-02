"""
ember_voice_text.py
====================
The pure-text half of Ember's voice pipeline — everything that decides
WHAT gets spoken or WHETHER a heard phrase counts as addressed to Ember,
with no audio, no models, and no threads in it. Split out of
ember_voice.py deliberately: this is the part with the most fiddly edge
cases (markdown in the middle of a stream, "p.m." vs a sentence end,
"amber" vs "ember"), and it's only reliably testable if it doesn't drag
Kokoro/Whisper/ONNX in with it.

Four jobs:

1. clean_for_speech() / SentenceChunker — turn Ember's markdown-ish reply
   text, arriving as a stream of small deltas from process_turn()'s
   stream_callback, into clean, speakable sentence-sized chunks the TTS
   can start on before the reply has finished generating. Reading
   "asterisk asterisk Cloud" or a URL aloud is the failure this exists to
   prevent; so is speaking a code block (replaced once per reply with a
   one-line pointer to the screen).

2. detect_wake() — leading/trailing wake-phrase matching ("Ember", "hey
   Ember", "wake up") on a transcript, returning the command with the
   wake phrase stripped so it reaches ember_intent/ember_core exactly as
   if it had been typed. Explicit alias list, not fuzzy matching:
   difflib-style similarity to "ember" also accepts "embed", which would
   turn every technical conversation in earshot into a wake event.

3. parse_voice_mode_command() / is_stop_command() — "voice mode" / "that's all" style
   commands, full-string anchored (same precision technique as
   ember_intent.py's GET_TIME family) so an ordinary sentence containing
   those words never toggles anything.

4. looks_like_echo() / is_stt_garbage() — backstops for the two
   always-listening failure modes: Ember hearing her OWN speech through
   the mic (which in voice mode would otherwise become a command and
   loop forever), and Whisper's well-known habit of hallucinating
   "Thank you." on near-silent audio.
"""

import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

# ---------------------------------------------------------------------------
# 1. Speech cleaning
# ---------------------------------------------------------------------------

CODE_NOTE = "I've put the code on screen, sir."

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U0001F000-\U0001F2FF\u2600-\u27BF\uFE0F\u200d]+"
)
_FENCE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)\s]+(?:\s+\"[^\"]*\")?\)")
_URL_RE = re.compile(r"https?://[^\s)\]>]+")
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
_QUOTE_RE = re.compile(r"^\s*>\s?")
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+\u2022]|\d+[.)])\s+")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:\-|]+\|?\s*$")
_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1")
_STAR_EM_RE = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")
_UNDER_EM_RE = re.compile(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
_END_PUNCT = ".!?:;,"

# ---- file paths and file names ----------------------------------------------------------
# A reply like "Moved 'a_b.pdf' from C:\\Users\\x\\Downloads to D:\\VISHNU" used to be read out
# letter by letter ("C colon backslash Users backslash ..."). Spoken, a path is only ever
# useful as its LAST part ("Downloads", "VISHNU"); the full path stays on screen. A bare drive
# root becomes "the D drive". Words like "to"/"from" end a folder name that contains spaces, so
# "...\\Downloads to D:\\VISHNU" can't swallow the sentence around it.
_PATH_STOPWORDS = r"(?:to|from|into|and|in|on|at|as|then|or)"
_PATH_WORD = r'[^\\/:*?"<>|\s]+'
_WIN_PATH_RE = re.compile(
    r"[A-Za-z]:\\"
    rf"(?:{_PATH_WORD}(?: (?!{_PATH_STOPWORDS}\b){_PATH_WORD})*\\)*"
    rf"{_PATH_WORD}?"
)
_POSIX_PATH_RE = re.compile(r"(?<![\w/:.])(?:~|\.{1,2})?/(?:[\w.\-~@+]+/)+[\w.\-~@+]*")
_PATH_TRAIL_RE = re.compile(r"[.,;:!?)\]'\"]+$")
_FILE_EXTS = (
    "pdf|docx?|xlsx?|pptx?|txt|md|csv|json|zip|rar|7z|png|jpe?g|gif|webp|bmp|svg|mp3|wav|mp4|mkv|mov|"
    "py|js|ts|tsx|html?|css|log|exe|msi|bat|ps1|lnk|iso"
)
_FILENAME_RE = re.compile(rf"(?<![\w.])([\w\-]+(?:[_\- ][\w\-]+)*)\.({_FILE_EXTS})\b(?![\w@/])", re.IGNORECASE)
# Usage hints that belong on screen, not in the speech: (Say "undo that" to reverse it.)
_HINT_RE = re.compile(r"\(\s*say\b[^)]*\)", re.IGNORECASE)


def _speak_name(name: str) -> str:
    """'drivetrain_comparision.pdf' -> 'drivetrain comparision pdf' (no underscores, no dot)."""
    name = _FILENAME_RE.sub(lambda m: f"{m.group(1)} {m.group(2)}", name)
    return re.sub(r"[_\s]+", " ", name).strip()


def _speak_path(m: "re.Match") -> str:
    raw = m.group(0)
    trail_m = _PATH_TRAIL_RE.search(raw)
    trail = trail_m.group(0) if trail_m else ""
    body = raw[: len(raw) - len(trail)] if trail else raw
    parts = [seg for seg in re.split(r"[\\/]", body) if seg and seg not in ("~", ".", "..")]
    if not parts:
        return raw
    if len(parts) == 1 and re.fullmatch(r"[A-Za-z]:", parts[0]):
        return f"the {parts[0][0].upper()} drive{trail}"
    return _speak_name(parts[-1]) + trail


def speakable_paths(line: str) -> str:
    """Replaces file paths with their last component and tidies bare file names."""
    line = _WIN_PATH_RE.sub(_speak_path, line)
    line = _POSIX_PATH_RE.sub(_speak_path, line)
    return _FILENAME_RE.sub(lambda m: f"{m.group(1)} {m.group(2)}".replace("_", " "), line)


def _clean_line(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return ""
    is_structural = False

    if stripped.startswith("|"):
        if "-" in stripped and _TABLE_SEP_RE.match(stripped):
            return ""
        line = ", ".join(cell.strip() for cell in stripped.strip("|").split("|") if cell.strip())
        is_structural = True

    for regex in (_HEADER_RE, _QUOTE_RE, _LIST_MARKER_RE):
        new = regex.sub("", line, count=1)
        if new != line:
            is_structural = True
            line = new

    line = _IMAGE_RE.sub(r"\1", line)
    line = _LINK_RE.sub(r"\1", line)
    line = _URL_RE.sub("a link", line)
    line = _HINT_RE.sub("", line)
    line = _INLINE_CODE_RE.sub(r"\1", line)
    line = speakable_paths(line)
    line = _BOLD_RE.sub(r"\2", line)
    line = _STAR_EM_RE.sub(r"\1", line)
    line = _UNDER_EM_RE.sub(r"\1", line)
    line = _STRIKE_RE.sub(r"\1", line)
    line = line.replace("*", "").replace("`", "")
    line = _EMOJI_RE.sub("", line)
    line = line.replace("\u2014", ", ").replace(" \u2013 ", ", ").replace("\u2026", "...")
    line = re.sub(r"\s+", " ", line)
    line = re.sub(r"\s+,", ",", line)
    line = re.sub(r",(?:\s*,)+", ",", line).lstrip(" ,").rstrip()   # keep a trailing comma: it's a clause pause the TTS uses

    if line and is_structural and line[-1] not in _END_PUNCT:
        line += "."
    return line


def clean_for_speech(text: str) -> str:
    """Markdown-ish text -> plain speakable text. Multi-line input has each
    line cleaned separately and joined with spaces; list items, headers and
    table rows get a terminal period if they lack punctuation so the TTS
    pauses between them instead of running them together."""
    text = _FENCE_BLOCK_RE.sub(" ", text or "")
    lines = [_clean_line(l) for l in text.split("\n")]
    return " ".join(l for l in lines if l).strip()


def _has_speakable(text: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9]", text))


# ---------------------------------------------------------------------------
# Streaming sentence chunker
# ---------------------------------------------------------------------------

_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "vs", "e.g", "i.e",
    "a.m", "p.m", "approx", "inc", "ltd", "fig",
}
_BOUNDARY_RE = re.compile(r"([.!?]+[\"')\]]*)(?=\s)|(\n+)")
_TRAILING_WORD_RE = re.compile(r"([A-Za-z][A-Za-z.]*)$")

MIN_CHARS = 28          # chunks shorter than this are merged into the next sentence
FIRST_MIN_CHARS = 10    # ...except the first chunk of a reply, which goes out early for latency
MAX_CHARS = 220         # an unpunctuated run this long is force-split at a comma/space

# Latency ramp. Kokoro on CPU synthesizes at roughly 0.5-1.0x realtime, so a
# 160-character one-sentence reply (the persona's normal shape) would mean ~6s
# of silence before the first word. Instead: the FIRST chunk is cut at the first
# clause break (comma/semicolon/colon + space) at least FIRST_CLAUSE_MIN chars in,
# so audio starts after ~1.5-2s; the 2nd and 3rd chunks are capped (RAMP_CAPS) so
# each one finishes synthesizing while the previous one is still playing.
FIRST_CLAUSE_MIN = 30
FIRST_WORD_MIN = 48          # no clause break? then cut at the first space at least this far in
FIRST_CLAUSE_TAIL_MIN = 15   # don't cut if less than this would be left over
RAMP_CAPS = (90, 140)
_CLAUSE_RE = re.compile(r"[,;:](?=\s)")
_SPACE_RE = re.compile(r"\s")


def _first_cut(text: str) -> "int | None":
    """End index (exclusive) of the FIRST chunk of a reply: the earlier of
    (a) the first clause break at least FIRST_CLAUSE_MIN chars in, keeping its
    punctuation, and (b) the first word boundary at least FIRST_WORD_MIN chars
    in. (b) exists because replies often have one long comma-free sentence
    ("...is an LMDh sports prototype racing car designed by Porsche and built
    by Multimatic.") and the first chunk must still be short. Either way at
    least FIRST_CLAUSE_TAIL_MIN chars must remain, and the answer depends only
    on the text seen so far, so it comes out the same however the stream was
    sliced into deltas."""
    cuts = []
    for m in _CLAUSE_RE.finditer(text):
        if m.start() >= FIRST_CLAUSE_MIN:
            cuts.append(m.start() + 1)
            break
    for m in _SPACE_RE.finditer(text):
        if m.start() >= FIRST_WORD_MIN:
            cuts.append(m.start())
            break
    cuts = [c for c in cuts if len(text) - c >= FIRST_CLAUSE_TAIL_MIN]
    return min(cuts) if cuts else None


def _last_break(text: str, cap: int) -> "int | None":
    """Last clause break (else last space) below `cap`, as an exclusive end index."""
    last = None
    for m in _CLAUSE_RE.finditer(text):
        if 25 <= m.start() < cap:
            last = m.start() + 1
    if last is not None:
        return last
    for m in _SPACE_RE.finditer(text):
        if 40 <= m.start() < cap:
            last = m.start()
    return last


def _is_abbreviation(before_punct: str) -> bool:
    m = _TRAILING_WORD_RE.search(before_punct)
    if not m:
        return False
    word = m.group(1).lower()
    if word in _ABBREVIATIONS:
        return True
    # single capital initial: "J. Smith" — but only if it really is a lone letter
    return len(word) == 1 and m.group(1).isupper()


class SentenceChunker:
    """Feed it reply deltas as they stream in; it returns speakable
    chunks (already cleaned) whenever a full sentence — or enough merged
    short ones — is available. Call flush() at end of reply for the tail.

    Code fences are dropped entirely; the first one in a reply yields a
    single CODE_NOTE chunk. A partial fence marker split across two deltas
    (one delta ends "``", the next starts "`") is held back until it can
    be classified — without that, a fence would occasionally leak into
    the speech as literal backticks."""

    def __init__(self, min_chars: int = MIN_CHARS, first_min_chars: int = FIRST_MIN_CHARS, max_chars: int = MAX_CHARS):
        self.min_chars = min_chars
        self.first_min_chars = first_min_chars
        self.max_chars = max_chars
        self._ticks = ""        # 1-2 trailing backticks withheld pending the next delta
        self._buf = ""          # current incomplete sentence (raw text)
        self._hold = ""         # cleaned short sentences waiting to reach min_chars
        self._in_fence = False
        self._code_noted = False
        self._emitted = 0

    # -- public ------------------------------------------------------------
    def feed(self, delta: str) -> "list[str]":
        text = self._ticks + (delta or "")
        self._ticks = ""
        m = re.search(r"(?<!`)`{1,2}$", text)
        if m:
            self._ticks = m.group(0)
            text = text[: m.start()]

        out: "list[str]" = []
        while text:
            if self._in_fence:
                i = text.find("```")
                if i == -1:
                    break  # still inside the fence — discard
                text = text[i + 3:]
                self._in_fence = False
            else:
                i = text.find("```")
                if i == -1:
                    out += self._consume(text)
                    break
                out += self._consume(text[:i])
                out += self._drain(final=True)
                text = text[i + 3:]
                self._in_fence = True
                if not self._code_noted:
                    self._code_noted = True
                    out.append(CODE_NOTE)
                    self._emitted += 1
        return out

    def flush(self) -> "list[str]":
        out: "list[str]" = []
        if self._ticks and not self._in_fence:
            self._buf += self._ticks
        self._ticks = ""
        out += self._drain(final=True)
        return out

    # -- internals ---------------------------------------------------------
    def _threshold(self) -> int:
        return self.first_min_chars if self._emitted == 0 else self.min_chars

    def _accumulate(self, cleaned: str, out: "list[str]") -> None:
        if not cleaned or not _has_speakable(cleaned):
            return
        self._hold = (self._hold + " " + cleaned).strip()
        if len(self._hold) >= self._threshold():
            text, self._hold = self._hold, ""
            self._emit_pieces(text, out)

    def _emit_pieces(self, text: str, out: "list[str]") -> None:
        """Emits `text` as one or more chunks following the latency ramp."""
        while text:
            if self._emitted == 0:
                cut = _first_cut(text)
                if cut is not None:
                    out.append(text[:cut].strip())
                    self._emitted += 1
                    text = text[cut:].strip()
                    continue
            cap = MAX_CHARS if self._emitted - 1 >= len(RAMP_CAPS) else (RAMP_CAPS[self._emitted - 1] if self._emitted > 0 else None)
            if cap is not None and len(text) > cap:
                cut = _last_break(text, cap)
                if cut is not None:
                    out.append(text[:cut].strip())
                    self._emitted += 1
                    text = text[cut:].strip()
                    continue
            out.append(text)
            self._emitted += 1
            return

    def _consume(self, segment: str) -> "list[str]":
        self._buf += segment
        out: "list[str]" = []
        start = 0
        for m in _BOUNDARY_RE.finditer(self._buf):
            if m.group(1) and m.group(1).startswith(".") and _is_abbreviation(self._buf[start:m.start()]):
                continue
            piece = self._buf[start:m.end()]
            start = m.end()
            self._accumulate(clean_for_speech(piece), out)
        self._buf = self._buf[start:]

        if self._emitted == 0 and not self._hold:
            cut = _first_cut(self._buf)
            if cut is not None:
                piece, self._buf = self._buf[:cut], self._buf[cut:]
                cleaned = clean_for_speech(piece)
                if _has_speakable(cleaned):
                    out.append(cleaned)
                    self._emitted += 1

        while len(self._buf) > self.max_chars:
            window = self._buf[: self.max_chars]
            cut = max(window.rfind(", "), window.rfind("; "), window.rfind(" "))
            cut = cut + 1 if cut > 0 else self.max_chars
            piece, self._buf = self._buf[:cut], self._buf[cut:]
            self._accumulate(clean_for_speech(piece), out)
        return out

    def _drain(self, final: bool) -> "list[str]":
        out: "list[str]" = []
        if self._buf.strip():
            self._accumulate(clean_for_speech(self._buf), out)
        self._buf = ""
        if self._hold and final:
            text, self._hold = self._hold, ""
            self._emit_pieces(text, out)
        return out


# ---------------------------------------------------------------------------
# 2. Wake phrase detection
# ---------------------------------------------------------------------------

# Explicit aliases, not fuzzy similarity — see module docstring. "amber" is
# Whisper's most common mishearing of "Ember"; the cost is that an
# utterance genuinely starting with "amber" wakes Ember. Override with
# EMBER_WAKE_ALIASES (comma-separated) if the debug log shows it misfiring.
DEFAULT_NAME_ALIASES = ("ember", "embers", "amber", "umber", "emba", "embar", "embre",
                        "humber", "enbow", "embow", "embo")      # the last four seen in real Whisper output for "Ember"
_FILLERS = {"hey", "hi", "hello", "ok", "okay", "yo", "oh"}
# Fragments Whisper writes for "Ember" on quiet laptop-mic audio ("Hey M. what time...").
# Only accepted right after a filler ("hey"/"okay"), never on their own.
_LOOSE_AFTER_FILLER = {"m", "em", "emb", "emmer"}
# "Remember what is the time?" is very often "Ember, what is the time?" — accepted only when the
# word after it is a question/auxiliary word AND the utterance is a question. "Remember to buy
# milk" and "remember what I said" (no question mark) are left alone.
_REMEMBER_FOLLOWERS = {"what", "when", "who", "how", "where", "which", "can", "could", "is", "are", "do", "does", "will"}
_TOKEN_RE = re.compile(r"[A-Za-z0-9']+")
_STRIP_LEAD = " \t,.:;!?-\u2014\u2013"


# ---- eager ("loose") matching: recall over precision ---------------------------------------------
# On a quiet laptop mic Whisper writes "Ember" as "Humba", "Mbo", "Enbow", "M boy", "Hey, I'm Bo", or "Remember,"
# (when "Ember," starts a command). A fixed alias list can never keep up, so this matches the SOUND PATTERN of
# the name instead: an optional "h", an optional vowel, then m/n + b ("ember", "amber", "umber", "humber", "humba",
# "mbo", "enbow", "embar", ...). It costs some false wakes on real words that share the pattern, which is the trade
# asked for: it is better to wake on the faintest "Ember" than to miss it. EMBER_WAKE_LOOSE=0 turns it off.
_EMBERISH_RE = re.compile(r"^h?[aeiou]?[mn]b[a-z]{0,4}$")
_EMBERISH_STOP = {
    "embed", "embedded", "embark", "embassy", "emblem", "embrace", "ambient", "ambush", "amble", "imbue",
    "amber's", "embody", "embroil", "umbra", "ambit",
}


def loose_wake_enabled() -> bool:
    return os.environ.get("EMBER_WAKE_LOOSE", "1") not in ("0", "false", "off")


def _letters(token: str) -> str:
    return re.sub(r"[^a-z]", "", (token or "").lower())


def ember_like(token: str) -> bool:
    """True if a single word has the sound pattern of "Ember" (see above)."""
    t = _letters(token)
    return 2 <= len(t) <= 7 and bool(_EMBERISH_RE.match(t)) and t not in _EMBERISH_STOP


def name_aliases() -> "tuple[str, ...]":
    raw = os.environ.get("EMBER_WAKE_ALIASES", "").strip()
    if not raw:
        return DEFAULT_NAME_ALIASES
    return tuple(a.strip().lower() for a in raw.split(",") if a.strip())


@dataclass
class WakeResult:
    matched: bool
    command: str = ""            # transcript with the wake phrase removed (original casing/punctuation kept)
    phrase: "str | None" = None  # what actually matched, normalized ("ember", "wake up", ...)
    position: "str | None" = None  # "leading" | "trailing"


def detect_wake(text: str, aliases: "tuple[str, ...] | None" = None) -> WakeResult:
    names = set(aliases or name_aliases())
    toks = [(m.group(0).lower(), m.start(), m.end()) for m in _TOKEN_RE.finditer(text or "")]
    if not toks:
        return WakeResult(False)

    # leading: [filler]{0,2} name   |   "wake up" [name]
    i = 0
    while i < len(toks) - 1 and i < 2 and toks[i][0] in _FILLERS:
        i += 1
    if toks[i][0] in names or (i > 0 and toks[i][0] in _LOOSE_AFTER_FILLER):
        return WakeResult(True, text[toks[i][2]:].lstrip(_STRIP_LEAD).strip(), toks[i][0], "leading")
    if loose_wake_enabled():
        if ember_like(toks[i][0]):                                   # "Humba", "Mbo", "Enbow", "Hey Mbo"
            return WakeResult(True, text[toks[i][2]:].lstrip(_STRIP_LEAD).strip(), toks[i][0], "leading")
        if i + 1 < len(toks):                                        # two words heard as one: "I'm Bo", "M boy"
            a, b = _letters(toks[i][0]), _letters(toks[i + 1][0])
            joinable = (len(a) <= 3) if i > 0 else (a in ("m", "em", "n") and b.startswith("b"))
            if joinable and 1 <= len(b) <= 3 and ember_like(a + b):
                return WakeResult(True, text[toks[i + 1][2]:].lstrip(_STRIP_LEAD).strip(), a + b, "leading")
        if i == 0 and toks[0][0] == "remember" and text[toks[0][2]:toks[0][2] + 1] == ",":
            # "Ember, remind me..." is very often heard as "Remember, remind me..." (note the comma). Without the
            # comma ("Remember to buy milk") it stays an ordinary sentence.
            return WakeResult(True, text[toks[0][2]:].lstrip(_STRIP_LEAD).strip(), "remember,", "leading")
    if (i == 0 and toks[0][0] == "remember" and len(toks) > 2 and toks[1][0] in _REMEMBER_FOLLOWERS
            and text.rstrip().endswith("?")):
        return WakeResult(True, text[toks[1][1]:].strip(), "remember(?)", "leading")
    if toks[0][0] == "wake" and len(toks) > 1 and toks[1][0] == "up":
        end_idx = 1
        if len(toks) > 2 and toks[2][0] in names:
            end_idx = 2
        return WakeResult(True, text[toks[end_idx][2]:].lstrip(_STRIP_LEAD).strip(), "wake up", "leading")

    # trailing: "... ember" (a trailing filler like "hey" before it is dropped too)
    if len(toks) >= 2 and (toks[-1][0] in names or (loose_wake_enabled() and ember_like(toks[-1][0]))):
        cut_idx = len(toks) - 1
        if cut_idx >= 1 and toks[cut_idx - 1][0] in _FILLERS:
            cut_idx -= 1
        return WakeResult(True, text[: toks[cut_idx][1]].rstrip(" \t,.:;-\u2014\u2013").strip(), toks[-1][0], "trailing")

    return WakeResult(False)


# Real words that sound like the start of "Ember" to an acoustic keyword spotter. A soft acoustic hit
# ("Ember"/"wake up" at the start of an utterance) whose first transcribed word is one of these is a false
# alarm: "Remember to buy milk", "December is cold", ... Extend with EMBER_WAKE_SOFT_VETO=word1,word2.
SOFT_VETO_WORDS = {
    "remember", "remembered", "remembering", "remembers", "member", "members", "membership",
    "december", "november", "september", "timber", "embed", "embedded", "embedding", "embers",
}


def soft_wake_vetoed(head_text: str) -> bool:
    """True if the first word of `head_text` (a transcript of just the start of the utterance) is a real
    word the spotter tends to confuse with "Ember"."""
    extra = {w.strip().lower() for w in os.environ.get("EMBER_WAKE_SOFT_VETO", "").split(",") if w.strip()}
    m = _TOKEN_RE.search(head_text or "")
    return bool(m) and m.group(0).lower() in (SOFT_VETO_WORDS | extra)


def strip_wake_residue(text: str, aliases: "tuple[str, ...] | None" = None) -> str:
    """After an ACOUSTIC wake detection the audio was cut a little before the end of "hey ember",
    so Whisper may have transcribed a fragment of it ("Ember, what time...", "M. what time...").
    Drops up to three leading wake-ish tokens, always leaving at least one word."""
    names = set(aliases or name_aliases())
    residue = names | _FILLERS | _LOOSE_AFTER_FILLER | {"um", "uh"}
    toks = list(_TOKEN_RE.finditer(text or ""))
    i = 0
    loose = loose_wake_enabled()
    while i < len(toks) - 1 and i < 3 and (toks[i].group(0).lower() in residue or (loose and ember_like(toks[i].group(0)))):
        i += 1
    return text[toks[i].start():] if i else (text or "").strip()


# ---------------------------------------------------------------------------
# 3. Voice-mode commands
# ---------------------------------------------------------------------------

_POLITE_TAIL_RE = re.compile(r"\s+(?:please|thanks|thank you|sir)$")
_VM_ON_RE = re.compile(
    r"^(?:(?:enter|start|begin|turn on|enable|activate|switch to)\s+)?voice mode(?:\s+on)?$"
    r"|^let'?s\s+(?:talk|chat)$"
)
_VM_OFF_RE = re.compile(
    r"^(?:exit|leave|stop|end|quit|turn off|disable|deactivate)\s+voice mode$"
    r"|^voice mode\s+off$"
    r"|^stop listening$"
    r"|^that'?s all(?:\s+for now)?$"
    r"|^we'?re done(?:\s+here)?$"
    r"|^go (?:to sleep|quiet)$"
)


def _normalize(text: str) -> str:
    t = re.sub(r"[^a-z0-9' ]+", " ", (text or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    while True:
        new = _POLITE_TAIL_RE.sub("", t)
        if new == t:
            return t
        t = new


def parse_voice_mode_command(text: str) -> "str | None":
    """'on' | 'off' | None. Full-string anchored on purpose — 'the voice
    mode setting is confusing' must never toggle anything."""
    t = _normalize(text)
    if _VM_ON_RE.match(t):
        return "on"
    if _VM_OFF_RE.match(t):
        return "off"
    return None


_STOP_RE = re.compile(
    r"^(?:stop|quiet|be quiet|shut up|cancel|enough|that'?s enough|stop talking|never ?mind|hold on|wait)$"
)


def is_stop_command(text: str) -> bool:
    """Bare 'stop' / 'quiet' / 'never mind' — while Ember is speaking or
    working, this is a LOCAL interrupt (silence, cancel the turn), not a
    chat message. Full-string anchored for the same reason as
    ember_intent.py's INTERRUPT: 'stop the AQI forecasting system' is
    about something else and must reach the normal pipeline."""
    return bool(_STOP_RE.match(_normalize(text)))


# ---------------------------------------------------------------------------
# 3b. Spoken confirmation (yes / no) for destructive actions
# ---------------------------------------------------------------------------
# When Ember needs approval ("allow access to Downloads", "delete that file") and the
# person is talking to her rather than looking at the app, she asks out loud and listens
# for the answer. Deny is the safe direction: any clear "no"/"stop"/"cancel" counts, and a
# message that mixes yes and no, or carries a "wait"/"but"/question word, is NOT an
# approval — it returns None so she asks again instead of acting on a guess.

_YES_START = {
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "alright", "affirmative", "absolutely",
    "definitely", "approve", "approved", "confirm", "confirmed", "proceed", "allow", "grant",
}
_YES_PHRASE_STARTS = {("go", "ahead"), ("do", "it"), ("go", "for"), ("that's", "fine"), ("sounds", "good")}
_NO_TOK = {
    "no", "nope", "nah", "don't", "dont", "not", "never", "deny", "denied", "negative",
    "cancel", "stop", "abort", "nevermind", "refuse",
}
_NOT_A_YES = {"wait", "hold", "but", "actually", "hmm", "what", "which", "why", "how", "where", "who", "when"}
_CONFIRM_WAKE_LEAD = {"hey", "ember"}


def parse_confirmation_reply(text: str) -> "str | None":
    """'yes' | 'no' | None for a short spoken answer to "shall I...?". Anchored on a short
    utterance (<= 8 words) so a long sentence that happens to contain "yes" never approves."""
    toks = _normalize(text).split()
    while toks and (toks[0] in _CONFIRM_WAKE_LEAD or ember_like(toks[0])):
        toks = toks[1:]
    if not toks or len(toks) > 8:
        return None
    no_hit = any(t in _NO_TOK for t in toks) or (len(toks) >= 2 and toks[0] == "never" and toks[1] == "mind")
    yes_hit = toks[0] in _YES_START or tuple(toks[:2]) in _YES_PHRASE_STARTS
    if no_hit and yes_hit:
        return None
    if no_hit:
        return "no"
    if yes_hit and not any(t in _NOT_A_YES for t in toks):
        return "yes"
    return None


def _last_part(raw: str) -> str:
    """Spoken form of a path or name: its last component, tidied."""
    cleaned = speakable_paths((raw or "").strip().strip("'\".,!?"))
    return cleaned or "that"


def confirmation_prompt(tool_name: str, args: "dict | None" = None) -> str:
    """One short sentence asking for approval, built from what the gate was asked about.
    Only the last part of a path is ever spoken (the full path is on screen)."""
    args = args or {}
    name = (tool_name or "").lower()
    matched = str(args.get("matched_text") or "")
    if name == "allow_path":
        m = re.search(r"access(?:\s+to)?\s+(.+)$", matched, re.IGNORECASE)
        what = _last_part(m.group(1)) if m else "that folder"
        ask = f"Allow access to {what}"
    elif name == "delete_file":
        ask = f"Move {_last_part(str(args.get('file') or ''))} to the trash"
    elif name == "run_script":
        ask = "Run that script"
    elif name == "clear_memory":
        ask = "Wipe everything I remember"
    elif name == "forget_memory":
        ask = "Forget that"
    elif name.startswith("cancel_calendar"):
        ask = "Cancel that calendar event"
    else:
        ask = "Go ahead with " + name.replace("_", " ") if name else "Go ahead"
    return clean_for_speech(f"{ask}, sir \u2014 yes or no?")


# ---------------------------------------------------------------------------
# 4. Echo / hallucination guards
# ---------------------------------------------------------------------------

_HALLUCINATIONS = {
    "you", "thank you", "thanks for watching", "thank you for watching",
    "bye", "bye bye", "subscribe", "please subscribe",
}


def is_stt_garbage(text: str) -> bool:
    t = _normalize(text)
    if len(re.sub(r"[^a-z0-9]", "", t)) < 2:
        return True
    return t in _HALLUCINATIONS


def looks_like_echo(text: str, recent_spoken: "list[str]") -> bool:
    """True if `text` is probably Ember's own recent speech coming back
    through the microphone. Browser echo cancellation is the first line of
    defense (client side); this is the backstop, and it matters most in
    voice mode where an un-caught echo becomes a command, gets answered,
    is heard again, and so on forever."""
    words = _normalize(text).split()
    if not words or not recent_spoken:
        return False
    if len(words) == 1:
        return words[0] == "sir" and any("sir" in _normalize(s).split() for s in recent_spoken)

    heard = " ".join(words)
    heard_set = set(words)
    for spoken in recent_spoken:
        spoken_norm = _normalize(spoken)
        if not spoken_norm:
            continue
        spoken_set = set(spoken_norm.split())
        if len(heard_set & spoken_set) / len(heard_set) >= 0.8:
            return True
        if SequenceMatcher(None, heard, spoken_norm).ratio() >= 0.75:
            return True
    return False
