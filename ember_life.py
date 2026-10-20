"""Life events: the dated things you tell Ember about -- a trip, a practical, an exam week, a service, a deadline.

Stored in a plain local file (data/life.json); nothing here touches Google Calendar. Ember is told about them in
ordinary speech ("I have a trip in 5 days", "my physics practical is on 3rd November", "sem exams from the 12th
to the 24th of December", "add scooty service on the 31st"), resolves the date right then, and from then on every
reply has an always-current "coming up" block in front of her, so "when am I going?" and "how long until my
practical?" are answered from the file, not from luck.

Capture is deliberately precise rather than clever (a wrong note about your life is worse than a missed one):
  * explicit commands ("add / note / log / put / schedule X on <date>") accept any title;
  * plain statements ("I have ...", "my ... is on ...", "I'm going to ... on ...") are only taken when the thing is
    something worth tracking (trip, exam, practical, deadline, appointment, birthday, service, ...) AND a real date
    expression sits at the end of the sentence. Questions and anything with leftover clauses are ignored.
Every capture is echoed back with the resolved date, so a wrong guess is visible immediately.
"""
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from ember_reminders import _MONTHS, _MONTH_RE, _WEEKDAY_NAMES, _find_calendar_date

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
LIFE_PATH = os.path.join(_PROJECT_ROOT, "data", "life.json")
MAX_EVENTS = 300
PROMPT_WINDOW_DAYS = 60
PROMPT_MAX_LINES = 12

_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
              "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30}
_NUM = r"(?:\d{1,3}|" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")"


def _num(tok: str) -> int:
    return int(tok) if tok.isdigit() else _NUM_WORDS[tok]


# ---------------------------------------------------------------- finding a date in a sentence

@dataclass
class When:
    start: date
    end: Optional[date]
    span: "tuple[int, int]"        # where the date expression sits in the text it was found in
    time: Optional[str] = None     # "HH:MM"
    error: str = ""


_SEP = r"(?:to|till|until|through|-|–)"
_ORD = r"(?:st|nd|rd|th)?"
_R1 = re.compile(rf"(?:\bfrom\s+)?(?:\bthe\s+)?\b(\d{{1,2}}){_ORD}\s*{_SEP}\s*(?:the\s+)?(\d{{1,2}}){_ORD}\s+(?:of\s+)?({_MONTH_RE})\b\.?(?:\s*,?\s*(\d{{4}})\b)?")
_R2 = re.compile(rf"(?:\bfrom\s+)?\b({_MONTH_RE})\b\.?\s+(?:the\s+)?(\d{{1,2}}){_ORD}\s*{_SEP}\s*(?:({_MONTH_RE})\b\.?\s+)?(?:the\s+)?(\d{{1,2}}){_ORD}\b(?:\s*,?\s*(\d{{4}})\b)?")
_R3 = re.compile(rf"\bfrom\s+(.+?)\s+{_SEP}\s+(.+)$")
_REL_DAYS = re.compile(rf"\bin\s+({_NUM})\s+(days?|weeks?)\b")
_WD = "|".join(_WEEKDAY_NAMES)
_NEXT_WD = re.compile(rf"\b(?:next|this\s+coming|coming|this|on)\s+({_WD})\b")
_DURATION = re.compile(rf"\s*,?\s*for\s+({_NUM})\s+(days?|nights?|weeks?)\b")
_TIME = re.compile(r"(?:\bat\s+)?\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\bat\s+(\d{1,2}):(\d{2})\b")


def _month_day(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _range_from_days(d1: int, d2: int, month: int, year: Optional[int], today: date) -> "tuple[Optional[date], Optional[date], str]":
    if not (1 <= d1 <= 31 and 1 <= d2 <= 31 and d1 <= d2):
        return None, None, "That date range doesn't look right, sir."
    if year is not None:
        a, b = _month_day(year, month, d1), _month_day(year, month, d2)
        if not a or not b:
            return None, None, "That date range isn't real, sir."
        if b < today:
            return None, None, f"{b.strftime('%B')} {year} has already passed, sir."
        return a, b, ""
    for y in (today.year, today.year + 1):
        a, b = _month_day(y, month, d1), _month_day(y, month, d2)
        if a and b and b >= today:
            return a, b, ""
    return None, None, "That date range isn't real, sir."


def _time_in(text: str) -> "tuple[Optional[str], Optional[tuple[int, int]]]":
    m = _TIME.search(text)
    if not m:
        return None, None
    if m.group(1):
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if not (1 <= h <= 12 and mi < 60):
            return None, None
        h = h % 12 + (12 if ap == "pm" else 0)
    else:
        h, mi = int(m.group(4)), int(m.group(5))
        if not (h < 24 and mi < 60):
            return None, None
    return f"{h:02d}:{mi:02d}", m.span()


def find_when(text: str, now: Optional[datetime] = None) -> Optional[When]:
    """Finds the date (or date range) a sentence is talking about. `text` should be lowercased. Returns None when
    there is no date expression; a When with .error set when there is one but it can't be used (already passed,
    not a real date). A clock time ("at 5pm") is picked up too but is never part of the span."""
    now = now or datetime.now()
    today = now.date()
    time_str, _ = _time_in(text)

    m = _R1.search(text)
    if m:
        a, b, err = _range_from_days(int(m.group(1)), int(m.group(2)), _MONTHS[m.group(3)],
                                      int(m.group(4)) if m.group(4) else None, today)
        return When(a or today, b, m.span(), time_str, err) if err else _with_duration(When(a, b, m.span(), time_str), text)
    m = _R2.search(text)
    if m:
        month = _MONTHS[m.group(1)]
        if m.group(3) and _MONTHS[m.group(3)] != month:
            m = None                                  # "nov 25 to dec 2" is handled by the from/to form below
        else:
            a, b, err = _range_from_days(int(m.group(2)), int(m.group(4)), month,
                                          int(m.group(5)) if m.group(5) else None, today)
            return When(a or today, b, m.span(), time_str, err) if err else _with_duration(When(a, b, m.span(), time_str), text)
    m = _R3.search(text)
    if m:
        d1, s1, e1 = _find_calendar_date(m.group(1), now)
        d2, s2, e2 = _find_calendar_date(m.group(2), now)
        if (e1 or e2):
            return When(today, None, m.span(), time_str, e1 or e2)
        if d1 and d2:
            if d2 < d1:
                return When(today, None, m.span(), time_str, "That range ends before it starts, sir.")
            return When(d1, d2, (m.start(), m.start(2) + s2[1]), time_str)

    d, span, err = _find_calendar_date(text, now)
    if err:
        return When(today, None, (0, 0), time_str, err)
    if d:
        return _with_duration(When(d, None, span, time_str), text)

    m = _REL_DAYS.search(text)
    if m:
        n = _num(m.group(1)) * (7 if m.group(2).startswith("week") else 1)
        return _with_duration(When(today + timedelta(days=n), None, m.span(), time_str), text)
    for pat, delta in ((r"\bday\s+after\s+tomorrow\b", 2), (r"\btomorrow\b", 1), (r"\btoday\b", 0)):
        m = re.search(pat, text)
        if m:
            return _with_duration(When(today + timedelta(days=delta), None, m.span(), time_str), text)
    m = _NEXT_WD.search(text)
    if m:
        target = _WEEKDAY_NAMES.index(m.group(1))
        ahead = (target - today.weekday()) % 7 or 7
        return _with_duration(When(today + timedelta(days=ahead), None, m.span(), time_str), text)
    return None


def _with_duration(w: When, text: str) -> When:
    """"... on the 3rd for 4 days" / "in 5 days for a week": extends a single day into a stretch."""
    if w.end is not None:
        return w
    m = _DURATION.match(text, w.span[1])
    if not m:
        return w
    n, unit = _num(m.group(1)), m.group(2)
    days = n * 7 if unit.startswith("week") else (n + 1 if unit.startswith("night") else n)
    if days > 1:
        w.end = w.start + timedelta(days=days - 1)
    w.span = (w.span[0], m.end())
    return w


# ---------------------------------------------------------------- capturing from a sentence

@dataclass
class Capture:
    title: str
    start: date
    end: Optional[date]
    time: Optional[str]
    error: str = ""


_FILLER = (r"^(?:(?:ok(?:ay)?|so|well|btw|fyi|also|and|oh|just|hey ember|ember|note that|remember that|remember|"
           r"by the way|for your information|keep in mind that|please)[,\s]+)*")
_KIND = re.compile(
    r"\b(?:trips?|tours?|travel\w*|flights?|journey|vacations?|holidays?|breaks?|exams?|tests?|quiz(?:zes)?|vivas?|"
    r"practicals?|labs?|internals?|midterms?|finals?|sem(?:ester)?s?|submissions?|deadlines?|assignments?|projects?|"
    r"presentations?|seminars?|interviews?|appointments?|check-?ups?|dentist|doctor|birthdays?|anniversar\w+|"
    r"weddings?|functions?|fests?|hackathons?|competitions?|contests?|matches|match|races?|concerts?|events?|"
    r"ceremon\w+|results?|servic(?:e|ing)|maintenance|part change|oil change|camps?|workshops?|conferences?|"
    r"meetups?|admit cards?|orientation|convocation|reunion)\b")
_NOT_PLACE = {"gym", "school", "college", "work", "class", "classes", "office", "bed", "sleep", "bathroom", "toilet",
              "shop", "store", "market", "mall", "library", "canteen", "lab", "sleep", "eat", "lunch", "dinner"}
_QUESTION_START = re.compile(r"^(?:what|when|where|why|how|which|who|do|does|did|can|could|will|would|should|is|are|am)\b")
_ANY_DROP = re.compile(r"\b(?:remind|reminder|calendar|timer|alarm|memory|profile|file|folder)\b")
_IMPERATIVE = re.compile(r"^(?:book|buy|plan|pack|cancel|find|show|tell|check|call|make|get|start|stop|open|send|set|add|remove|delete|move|"
                         r"push|shift|postpone|delay|prepone|change|reschedule|forget|take|list|prepare|study|revise|finish|submit)\b")
_COMMAND_VERBS = r"(?:add|note|log|put|mark|save|track|schedule|set\s+up|make\s+a\s+note\s+of)(?:\s+down)?"
_STATEMENT_RES = [
    ("have", re.compile(_FILLER + r"(?:i|we)(?:\s+|(?='))(?:have|got|'ve\s+got|ve\s+got|have\s+got|will\s+have|am\s+having|'m\s+having|"
                                  r"are\s+having|'re\s+having|am\s+going\s+on|'m\s+going\s+on|are\s+going\s+on|'re\s+going\s+on)\s+(?P<body>.+)$")),
    ("my", re.compile(_FILLER + r"(?:my|our)\s+(?P<body>.+)$")),
    ("bare", re.compile(_FILLER + r"(?P<body>.+)$")),     # "trip in 5 days", "practicals from nov 20 to nov 30"
    ("go", re.compile(_FILLER + r"(?:i|we)(?:\s+|(?='))(?:am\s+|'m\s+|are\s+|'re\s+)?(?:going|travelling|traveling|flying|leaving|heading|off)\s+(?:to|for)\s+(?P<body>.+)$")),
]
_COMMAND_RE = re.compile(_FILLER + _COMMAND_VERBS + r"\s+(?P<body>.+)$")
_TRAIL_OK = re.compile(r"^\s*(?:\d{1,2}\s*,\s*\d{4}\s*)?(?:(?:to|in|into|on)\s+(?:my\s+)?(?:plans|events|dates|countdowns?|life|list|schedule)\s*)?(?:please|sir|ember|thanks|thank you)?[\s.!,]*$")   # the leading digits: a speech-to-text garble of the year, ignored
_LEAD_STRIP = re.compile(r"^(?:that\s+|a\s+|an\s+|the\s+|my\s+|our\s+|some\s+|down\s+)+")
_TAIL_STRIP = re.compile(r"(?:\s+(?:is|are|on|for|in|from|by|at|starts?|begins?|will\s+be|will\s+start|scheduled|due|coming\s+up|falls?|being|to|till|until))+$")


def _clean_title(raw: str) -> str:
    t = raw.strip(" ,.;:!-–")
    t = _LEAD_STRIP.sub("", t)
    t = _TAIL_STRIP.sub("", t).strip(" ,.;:!-–")
    if not t or len(t.split()) > 8:
        return ""
    return t[0].upper() + t[1:]


def _title_and_when(body_orig: str, body_low: str, now: datetime) -> "Optional[tuple[str, When]]":
    """Splits "<title words> <date expression> [at 5pm] [please]" into its title and When; None if the sentence has
    a date expression that isn't at its end (leftover clauses mean it's not a clean statement)."""
    w = find_when(body_low, now)
    if w is None:
        return None
    if w.error:
        return ("", w)
    _t, tspan = _time_in(body_low)
    pre = body_orig[:w.span[0]]
    post = body_low[w.span[1]:]
    if tspan:                                    # a time clause, wherever it sits, is not title and not leftovers
        if tspan[1] <= w.span[0]:
            pre = body_orig[:tspan[0]] + body_orig[tspan[1]:w.span[0]]
        elif tspan[0] >= w.span[1]:
            post = body_low[w.span[1]:tspan[0]] + body_low[tspan[1]:]
    if not _TRAIL_OK.match(post):
        return None
    return pre, w


def capture(text: str, now: Optional[datetime] = None) -> Optional[Capture]:
    """Reads one message and returns the dated event it announces, or None. See the module docstring for the rules."""
    now = now or datetime.now()
    orig = text.strip()
    low = orig.lower()
    if len(low) != len(orig) or "?" in low or len(low.split()) > 40 or _ANY_DROP.search(low):
        return None
    if _QUESTION_START.match(re.sub(_FILLER, "", low)):
        return None

    forms: "list[tuple[str, re.Match]]" = []
    m = _COMMAND_RE.match(low)
    if m:
        forms.append(("command", m))
    for kind, rx in _STATEMENT_RES:
        m = rx.match(low)
        if m:
            forms.append((kind, m))
    for kind, m in forms:
        body_low = m.group("body")
        body_orig = orig[m.start("body"):m.end("body")]
        got = _title_and_when(body_orig, body_low, now)
        if got is None:
            continue
        pre, w = got
        if w.error:
            if kind in ("have", "my", "bare") and not _KIND.search(body_low):
                continue
            return Capture("", w.start, w.end, w.time, w.error)
        title = _clean_title(pre)
        if not title:
            continue
        low_title = title.lower()
        if kind in ("have", "my", "bare") and not _KIND.search(low_title):
            continue
        if kind == "bare" and (len(low_title.split()) > 5 or _IMPERATIVE.match(low_title)):
            continue
        if kind == "go":
            words = low_title.split()
            if not words or words[0] in ("the", "a", "an", "my", "our", "this", "that", "some") or words[0] in _NOT_PLACE:
                continue
            if (w.start - now.date()).days < 2:          # "going to <somewhere> tomorrow" is too often a errand, not a trip
                continue
            title = "Trip to " + title
        return Capture(title, w.start, w.end, w.time)
    return None


# ---------------------------------------------------------------- storage

def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", title.lower()).strip()


class LifeStore:
    """data/life.json: [{"id", "title", "start", "end", "time", "created"}]. Thread-safe, atomic writes."""

    def __init__(self, path: str = LIFE_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._events = self._load()

    def _load(self) -> "list[dict]":
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [e for e in data if isinstance(e, dict) and e.get("title") and e.get("start")] if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            print(f"[ember_life] couldn't read {self.path}: {e} -- keeping a copy as .bad and starting empty")
            try:
                os.replace(self.path, self.path + ".bad")
            except OSError:
                pass
            return []

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._events, f, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            print(f"[ember_life] couldn't save {self.path}: {e}")

    @staticmethod
    def _end(ev: dict) -> date:
        return date.fromisoformat(ev.get("end") or ev["start"])

    @staticmethod
    def _start(ev: dict) -> date:
        return date.fromisoformat(ev["start"])

    def all(self) -> "list[dict]":
        with self._lock:
            return sorted((dict(e) for e in self._events), key=lambda e: (e["start"], e["title"]))

    def upcoming(self, today: date, days: int = PROMPT_WINDOW_DAYS, past_days: int = 0) -> "list[dict]":
        lo, hi = today - timedelta(days=past_days), today + timedelta(days=days)
        return [e for e in self.all() if self._end(e) >= lo and self._start(e) <= hi]

    def add_or_update(self, title: str, start: date, end: Optional[date], time: Optional[str], today: date) -> "tuple[dict, str, Optional[dict]]":
        """Returns (event, "added"|"updated"|"unchanged", previous). A repeat of the same title, still upcoming, on a
        different date is treated as a correction (the trip moved), not as a second trip."""
        key = _norm(title)
        with self._lock:
            same = [e for e in self._events if _norm(e["title"]) == key and self._end(e) >= today]
            for e in same:
                if e["start"] == start.isoformat() and (e.get("end") or None) == (end.isoformat() if end else None) and e.get("time") == time:
                    return dict(e), "unchanged", None
            if len(same) == 1:
                e = same[0]
                before = dict(e)
                e.update(start=start.isoformat(), end=end.isoformat() if end else None, time=time)
                self._save()
                return dict(e), "updated", before
            e = {"id": uuid.uuid4().hex[:8], "title": title, "start": start.isoformat(),
                 "end": end.isoformat() if end else None, "time": time, "created": datetime.now().isoformat(timespec="seconds")}
            self._events.append(e)
            if len(self._events) > MAX_EVENTS:            # drop the oldest already-finished ones first
                self._events.sort(key=lambda x: x["start"])
                finished = [x for x in self._events if self._end(x) < today]
                for x in finished[: len(self._events) - MAX_EVENTS]:
                    self._events.remove(x)
            self._save()
            return dict(e), "added", None

    def find(self, query: str, today: date, include_past: bool = False) -> "list[dict]":
        """Events whose title contains every meaningful word of `query` (looser fallback: most overlap)."""
        from ember_memory import content_tokens
        q = content_tokens(query)
        if not q:
            return []
        pool = [e for e in self.all() if include_past or self._end(e) >= today - timedelta(days=2)]
        tight = [e for e in pool if q <= content_tokens(e["title"])]
        if tight:
            return tight
        scored = [(len(q & content_tokens(e["title"])), e) for e in pool]
        best = max((n for n, _ in scored), default=0)
        return [e for n, e in scored if n and n == best] if best else []

    def remove(self, event_id: str) -> bool:
        with self._lock:
            n = len(self._events)
            self._events = [e for e in self._events if e["id"] != event_id]
            if len(self._events) != n:
                self._save()
                return True
            return False

    def move(self, event_id: str, start: date, end: Optional[date], time: Optional[str]) -> "Optional[dict]":
        with self._lock:
            for e in self._events:
                if e["id"] == event_id:
                    e.update(start=start.isoformat(), end=end.isoformat() if end else None, time=time or e.get("time"))
                    self._save()
                    return dict(e)
        return None


# ---------------------------------------------------------------- saying it

def _d(d: date, today: date) -> str:
    s = f"{d.strftime('%a')}, {d.strftime('%b')} {d.day}"
    return s + (f", {d.year}" if d.year != today.year else "")


def _clock(t: Optional[str]) -> str:
    if not t:
        return ""
    h, m = int(t[:2]), int(t[3:])
    return f" at {(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def relative(ev: dict, today: date) -> str:
    s, e = date.fromisoformat(ev["start"]), date.fromisoformat(ev.get("end") or ev["start"])
    if s > today:
        n = (s - today).days
        return "tomorrow" if n == 1 else f"in {n} days"
    if e >= today:
        if s == e or s == today and e == today:
            return "today"
        left = (e - today).days
        return "starts today" if s == today else f"on now, {'ends today' if left == 0 else f'{left} more day' + ('s' if left != 1 else '')}"
    n = (today - e).days
    return "yesterday" if n == 1 else f"{n} days ago"


def describe(ev: dict, today: date) -> str:
    s, e = date.fromisoformat(ev["start"]), ev.get("end")
    when = _d(s, today) + _clock(ev.get("time"))
    if e and e != ev["start"]:
        when += f" to {_d(date.fromisoformat(e), today)}"
    return f"{ev['title']}: {when} ({relative(ev, today)})"


def prompt_block(store: LifeStore, today: Optional[date] = None) -> str:
    """The always-on "coming up" section of the system prompt; "" when there is nothing to say."""
    today = today or date.today()
    events = store.upcoming(today, PROMPT_WINDOW_DAYS, past_days=2)
    if not events:
        return ""
    lines = [f"- {describe(e, today)}" for e in events[:PROMPT_MAX_LINES]]
    more = len(events) - PROMPT_MAX_LINES
    if more > 0:
        lines.append(f"- ...and {more} more later")
    return (f"Dates and plans the user has told you about, from their local plans file (today is "
            f"{today.strftime('%A')}, {today.strftime('%B')} {today.day}, {today.year}). Treat these as facts and use "
            "them for date arithmetic. Bring one up only when it bears on what they said; don't recite the list. "
            "If they ask about a date or plan that isn't here, say you have nothing on record.\n" + "\n".join(lines))
