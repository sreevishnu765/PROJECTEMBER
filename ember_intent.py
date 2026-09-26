"""
ember_intent.py
=================
Natural-language command/intent layer, per the brief's own central rule:
these are intent ANCHORS, not literal keyword triggers — "Ember, stop."
must interrupt; "Ember, let's stop the AQI forecasting system." must not.
"if 'stop' in text: stop()" is explicitly the failure mode being avoided.

Where this sits relative to what already exists: ember_core.py's
_tool_registry (via ember_tools_registry.py) already handles reminders,
calendar check/create, Spotify, Drive, memory clear/forget, file/script
access, app launching, PDF export, and vision analysis — all with regexes
precise enough that this module doesn't need to re-decide them. This
module exists ONLY for the intents that genuinely aren't covered yet
(update-memory, status, diagnose, clear-context, new-conversation, find,
send, continue, interrupt) and for the one piece of real ambiguity the
brief is centrally worried about (stop/quiet/shut up vs. an ordinary
sentence that happens to contain "stop"). Per the brief's own instruction
— "do not create duplicate systems if Ember already has the relevant
functionality" — nothing here reimplements reminder/calendar logic.

Design: a cheap anchor-word PRE-FILTER (has_intent_anchor) decides whether
it's even worth considering this layer at all — ordinary conversation
("what's the capital of France") never reaches any of the checks below,
same zero-added-cost-for-the-common-case philosophy as needs_search()'s
own gating. Only past that gate do the structural checks below run, each
using sentence STRUCTURE (direct address, verb-vs-noun position, presence
or absence of a trailing object/target) rather than bare substring
matching, and in priority order — first confident match wins. When
structure alone can't resolve it, the correct move per the brief is to
ask a short clarification, NOT to guess — so several checks below return
a clarification-question result rather than executing something
irreversible on a coin flip.

This stays regex/heuristic, deliberately, not an LLM classification call:
every worked example in the brief is actually structurally resolvable
(direct address + bare stop-word = interrupt; stop-word + trailing "the/
my/that <noun>" = refers to something else; "what's my schedule" is
interrogative, "schedule X for Y" is imperative) — reaching for an LLM
call here would spend latency and quota on cases a handful of well-tested
patterns already handle correctly, which is exactly the "small enumerable
vocabulary -> heuristic, not LLM tool-calling" reasoning already
established elsewhere in this project. If real usage surfaces phrasings
these patterns can't resolve, the ambiguous ones fall through to a
clarification question rather than a silent wrong guess — never to a
confident-but-wrong execution.
"""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class IntentResult:
    intent: str
    confidence: str = "high"          # "high" | "ambiguous" — ambiguous means "ask, don't execute"
    target: "str | None" = None
    parameters: dict = field(default_factory=dict)
    clarification: "str | None" = None  # set instead of executing, when confidence == "ambiguous"


# ---- Cheap presence pre-filter --------------------------------------------
# "time"/"date"/"day" added (this pass) for the new local-utility intents
# below (GET_TIME/GET_DATE/GET_DAY/GET_DATETIME). This widens the gate
# slightly, but the gate only decides whether the (cheap, regex-only)
# checkers below even run — it never decides the intent itself — so the
# cost of a wider anchor set is a few extra regex evaluations on messages
# that happen to contain "time"/"date"/"day" and turn out unrelated (e.g.
# "how much time do I have"), not a correctness risk. Those messages
# still correctly fall through every checker and return None.
_ANCHOR_RE = re.compile(
    r"\b(remember|forget|update|remind|cancel|schedule|scheduled|find|where|"
    r"send|continue|move|status|diagnose|clear\s+context|new\s+conversation|"
    r"new\s+chat|start\s+fresh|stop|quiet|shut\s+up|stop\s+listening|"
    r"online|connected|working|time|date|day|"
    # "memor(?:y|ies)"/"search"/"scan" added (this pass) for the new
    # _check_memory_search below -- same "wider gate, checkers still
    # decide" reasoning as time/date/day above. Closes a real gap: a bare
    # "search memory"/"scan memory, ember" contained none of the words
    # this anchor previously recognized, so it never even reached the
    # checker layer at all, regardless of what _check_memory_search might
    # have matched.
    r"memor(?:y|ies)|search|scan)\b",
    re.IGNORECASE,
)


def has_intent_anchor(message: str) -> bool:
    """Cheap gate — if nothing here matches, skip this whole module for
    this turn. Ordinary conversation should never pay for the checks
    below."""
    return bool(_ANCHOR_RE.search(message))


# ---- INTERRUPT (stop / quiet / shut up) -----------------------------------
# The brief's own hardest case. A "bare" interrupt — direct address (or
# none needed) plus an interrupt word and NOTHING ELSE of substance — is
# high confidence. An interrupt word followed by a trailing object
# ("the AQI forecasting system", "the music") means the sentence is ABOUT
# something else; it is never treated as silencing Ember on a guess here.
_BARE_INTERRUPT_RE = re.compile(
    r"^\s*(?:hey\s+)?ember[,!.]?\s*(?:please\s+)?"
    r"(stop|quiet|shut\s+up|be\s+quiet|stop\s+listening)\s*[.!]*\s*$"
    r"|^\s*(?:please\s+)?(stop|quiet|shut\s+up|be\s+quiet|stop\s+listening)\s*[.!]*\s*$",
    re.IGNORECASE,
)

# An interrupt word WITH a trailing object ("stop the AQI system", "quiet
# down the fan") — this is about an external thing, not Ember's own
# speech. Captures the object so a caller can (optionally) try to match
# it against something real (an active reminder/task) rather than assume.
_INTERRUPT_WITH_OBJECT_RE = re.compile(
    r"\b(?:stop|cancel|pause)\s+(?:the|my|that|this)\s+(.+)", re.IGNORECASE
)


def _check_interrupt(message: str) -> "IntentResult | None":
    if _BARE_INTERRUPT_RE.match(message.strip()):
        return IntentResult(intent="INTERRUPT", confidence="high")

    obj_match = _INTERRUPT_WITH_OBJECT_RE.search(message)
    if obj_match:
        # Deliberately NOT flagged as INTERRUPT — the brief is explicit
        # that "stop the AQI forecasting system" refers to something
        # else, not Ember's own output. Returned as STOP_TASK so the
        # caller can try to match the named object against something
        # real (e.g. an active reminder); if nothing matches, the caller
        # should let this fall through to ordinary chat rather than
        # inventing a "task not found" response for what may just be
        # ordinary conversation mentioning a fictional/external system.
        return IntentResult(intent="STOP_TASK", confidence="high", target=obj_match.group(1).strip().strip(".!?"))

    # A bare stop-word exists somewhere (the anchor matched) but neither
    # pattern above fit cleanly — e.g. "why did the system stop?" (past
    # tense, interrogative) or "the music stopped" (narration, not a
    # command). Per the brief: these are normal conversation, not a
    # command at all. Returning None here (not a clarification) is
    # deliberate — asking "did you mean to interrupt me?" after every
    # incidental past-tense mention of the word "stop" would be far more
    # annoying than occasionally missing a genuine-but-oddly-phrased
    # interrupt request.
    return None


# ---- GET_TIME / GET_DATE / GET_DAY / GET_DATETIME (local utility) --------
# The machine's own clock already knows the current time/date/day —
# previously nothing in this file (or anywhere else) recognized that, so
# "what time is it" fell all the way through to ember_core.py's
# needs_search()/_mentions_named_person() heuristics and burned a Tavily
# search AND a metered Gemini grounding call to answer something
# datetime.now() answers instantly, for free, offline, always available.
# These four checkers close that gap, at the same layer (and same
# structural-regex discipline) as STATUS/DIAGNOSE above — not a second,
# competing intent system.
#
# Deliberately full-string anchored (^...$, modulo a small explicit set of
# leading-address/trailing-filler words), the same precision technique
# _BARE_INTERRUPT_RE already uses above — NOT a bare "if 'time' in text"
# substring check, which is explicitly the failure mode being avoided
# here: that would misfire on "how much time do I have before my exam"
# and "what time should I leave for the airport," both of which have a
# real trailing clause after the core phrase that these patterns do not
# consume as filler, so the full-string match simply fails and the
# message correctly falls through to ordinary chat/search handling
# instead of a growing blocklist chasing every new false positive.
_LEADING_ADDRESS = r"(?:hey\s+)?(?:ember[,!.]?\s*)?(?:please\s+)?"
_TRAILING_FILLER = r"(?:\s*(?:right\s+now|currently|exactly|precisely|now|today|please|sir))*\s*[?.!]*\s*"

_TIME_CORE = (
    r"(?:what'?s?\s+(?:is\s+)?the\s+(?:current\s+)?time"
    r"|what\s+time\s+is\s+it"
    r"|do\s+you\s+know\s+(?:what\s+time\s+it\s+is|the\s+time)"
    r"|current\s+time|time\s+check"
    r"|tell\s+me\s+the\s+time"
    r"|(?:have|got)\s+the\s+time)"
)
_GET_TIME_RE = re.compile(
    rf"^\s*{_LEADING_ADDRESS}(?:you\s+)?{_TIME_CORE}{_TRAILING_FILLER}$",
    re.IGNORECASE,
)

_DATE_CORE = (
    r"(?:what'?s?\s+(?:is\s+)?(?:the\s+|today'?s\s+)?date"
    r"|what\s+date\s+is\s+it(?:\s+today)?"
    r"|today'?s\s+date|current\s+date"
    r"|do\s+you\s+know\s+(?:today'?s\s+date|what\s+(?:the\s+)?date\s+is))"
)
_GET_DATE_RE = re.compile(
    rf"^\s*{_LEADING_ADDRESS}{_DATE_CORE}{_TRAILING_FILLER}$",
    re.IGNORECASE,
)

_DAY_CORE = (
    r"(?:what\s+day\s+is\s+it(?:\s+today)?"
    r"|what\s+day\s+is\s+today"
    r"|do\s+you\s+know\s+what\s+day\s+it\s+is)"
)
_GET_DAY_RE = re.compile(
    rf"^\s*{_LEADING_ADDRESS}{_DAY_CORE}{_TRAILING_FILLER}$",
    re.IGNORECASE,
)

_DATETIME_CORE = (
    r"(?:what'?s?\s+(?:is\s+)?the\s+(?:date\s+and\s+time|time\s+and\s+date)"
    r"|(?:give|tell)\s+me\s+the\s+(?:date\s+and\s+time|time\s+and\s+date)"
    r"|(?:date\s+and\s+time|time\s+and\s+date))"
)
_GET_DATETIME_RE = re.compile(
    rf"^\s*{_LEADING_ADDRESS}{_DATETIME_CORE}{_TRAILING_FILLER}$",
    re.IGNORECASE,
)


def _check_get_datetime(message: str) -> "IntentResult | None":
    # Checked before the standalone time/date checkers below. The
    # full-string anchors already make this order-independent in
    # practice (neither _GET_TIME_RE nor _GET_DATE_RE consumes a
    # trailing "...and time"/"...and date" clause as filler, so they
    # can't accidentally steal a combined request), but checking the
    # combined form first keeps the intent explicit rather than relying
    # solely on that as the safeguard.
    if _GET_DATETIME_RE.match(message.strip()):
        return IntentResult(intent="GET_DATETIME", confidence="high")
    return None


def _check_get_time(message: str) -> "IntentResult | None":
    if _GET_TIME_RE.match(message.strip()):
        return IntentResult(intent="GET_TIME", confidence="high")
    return None


def _check_get_date(message: str) -> "IntentResult | None":
    if _GET_DATE_RE.match(message.strip()):
        return IntentResult(intent="GET_DATE", confidence="high")
    return None


def _check_get_day(message: str) -> "IntentResult | None":
    if _GET_DAY_RE.match(message.strip()):
        return IntentResult(intent="GET_DAY", confidence="high")
    return None


# ---- UPDATE_MEMORY ---------------------------------------------------------
_UPDATE_MEMORY_RE = re.compile(
    r"\bupdate\s+(?:my\s+|the\s+)?(.+?)\s+to\s+(.+)"
    r"|\bthe\s+(.+?)\s+(?:has\s+)?changed\s+to\s+(.+)"
    r"|\bremember\s+that\s+the\s+(.+?)\s+(?:is\s+now|changed\s+to)\s+(.+)",
    re.IGNORECASE,
)


def _check_update_memory(message: str) -> "IntentResult | None":
    m = _UPDATE_MEMORY_RE.search(message)
    if not m:
        return None
    groups = [g for g in m.groups() if g]
    if len(groups) < 2:
        return None
    query, new_value = groups[0].strip(), groups[1].strip().strip(".!?")
    if not query or not new_value:
        return None
    # Reconstruct a clean declarative fact ("X is Y") to actually store,
    # rather than the raw imperative sentence ("Update my X to Y") — the
    # update mechanism works either way, but a command phrasing sitting in
    # memory reads oddly on future recall compared to a stated fact.
    clean_statement = f"{query} is {new_value}"
    return IntentResult(
        intent="UPDATE_MEMORY", confidence="high", target=query,
        parameters={"query": query, "full_statement": clean_statement},
    )


# ---- MEMORY INSPECTION ------------------------------------------------------
# "What do you remember about X" is already handled by the existing
# recall pipeline (needs_recall() in ember_core.py feeds memory.recall()
# into every chat turn's context) — this checker exists only to catch the
# explicit, high-confidence phrasing so it can be answered directly from
# memory.recall() rather than waiting on a full generate() call, which
# matters when cloud is degraded/exhausted (a direct memory lookup should
# never depend on LLM availability at all).
# ---- MEMORY INSPECTION ------------------------------------------------------
# Tightened (this pass): the bare "do you remember X" form matched ANY X,
# including live conversational questions like "do you remember when the
# next WEC round is" -- a colloquial way of asking a fresh question, not a
# request to recall something Ember was told to remember. Requiring "my" or
# "that" narrows this to the two shapes that actually signal a personal-
# memory check. "what do you remember about X" is untouched -- its explicit
# "about" already makes that construction unambiguous.
_INSPECT_MEMORY_RE = re.compile(
    r"\bwhat\s+do\s+you\s+remember\s+about\s+(.+)"
    r"|\bdo\s+you\s+remember\s+my\s+(.+)"
    r"|\bdo\s+you\s+remember\s+that\s+(.+)",
    re.IGNORECASE,
)


def _check_inspect_memory(message: str) -> "IntentResult | None":
    m = _INSPECT_MEMORY_RE.search(message)
    if not m:
        return None
    query = next((g for g in m.groups() if g), "").strip().strip("?.!")
    if not query:
        return None
    return IntentResult(intent="INSPECT_MEMORY", confidence="high", target=query, parameters={"query": query})


# ---- MEMORY SEARCH (search/scan/check memory) ------------------------------
# Real, separate gap from _INSPECT_MEMORY_RE above: that regex only
# recognizes "remember"/"recall"-shaped phrasing ("what do you remember
# about X", "do you remember my X"). It has never covered "search/scan/
# check memory" verb-shapes at all -- those messages contained no anchor
# word this module recognized (fixed above) AND matched no checker here,
# so they fell through this entire module silently. Worse, on the
# ember_core.py side, a bare "search memory"/"look up memory" (no
# possessive) was being caught by _is_explicit_search_request's regex and
# sent to a live web search instead -- there is no legitimate reading of
# "search memory" as a request for external search, it always means the
# local EmberMemory store. That regex is fixed too (see ember_core.py's
# _EXPLICIT_SEARCH_RE), but since ember_intent.classify() is checked
# BEFORE ember_core.py's fallback chain, this checker claiming the
# message first is what actually prevents the mis-route end to end.
#
# Target is only captured after an explicit "for"/"about"/"regarding"
# clause. A bare "scan memory, ember" genuinely doesn't name anything to
# look for -- returning an ambiguous result with a clarification question
# here follows the same "ask, don't guess" contract as every other
# ambiguous case in this module, rather than inventing a query out of
# nothing or dumping an arbitrary memory listing.
_MEMORY_SEARCH_RE = re.compile(
    r"\b(?:search|scan|check|look\s*up|go\s+through)\s+(?:my\s+|our\s+|through\s+)?memor(?:y|ies)\b"
    r"(?:\s+(?:for|about|regarding)\s+(.+))?",
    re.IGNORECASE,
)


def _check_memory_search(message: str) -> "IntentResult | None":
    m = _MEMORY_SEARCH_RE.search(message)
    if not m:
        return None
    target = (m.group(1) or "").strip().strip("?.!")
    if not target:
        return IntentResult(
            intent="INSPECT_MEMORY", confidence="ambiguous", target=None,
            clarification="Search memory for what, sir?",
        )
    return IntentResult(intent="INSPECT_MEMORY", confidence="high", target=target, parameters={"query": target})


# ---- STATUS / DIAGNOSE ------------------------------------------------------
_STATUS_RE = re.compile(
    r"^\s*ember,?\s*status\s*[.!?]*\s*$"
    r"|\bwhat'?s\s+your\s+status\b"
    # Widened (this pass) two ways, found via the exact examples given for
    # GET_DEVICE_STATUS/GET_SYSTEM_STATUS: (1) "connected" was the only
    # recognized word — "online" (the far more natural phrasing, e.g. "is
    # my Vivobook online?") never matched at all; (2) the "all systems"
    # phrasing required "all" and "systems" to sit directly adjacent —
    # "are all YOUR systems online" (a completely natural way to ask this)
    # failed because of the possessive word in between. Both were silent
    # failures before this fix — not new behavior, a bug being closed.
    r"|\bare\s+(?:all\s+)?(?:your\s+|the\s+|my\s+)?systems\s+online\b"
    r"|\bis\s+(?:everything|ember)\s+online\b"
    r"|\bis\s+my\s+(\w+)\s+(?:connected|online)\b",
    re.IGNORECASE,
)
# Tightened (this pass, after auditing for the same class of bug the
# "where is"/"find" fixes above address): "why isn't X working" originally
# accepted ANY X at all — "why isn't the WEC race streaming in India" or
# "why isn't the stream working for this race" would both get swallowed as
# a DIAGNOSE command targeting a subject Ember has no way to diagnose,
# instead of being treated as the real-world question they are. Restricted
# to a small, explicit, enumerable set of Ember's own local/system
# subjects — same "small vocabulary -> heuristic list, not open-ended
# capture" reasoning already used for _LAUNCH_APP_STOPWORDS elsewhere in
# this project. Anything outside this list now correctly falls through
# instead of being intercepted.
_DIAGNOSE_KNOWN_SUBJECTS = (
    r"(cloud|gemini|ollama|the\s+app|ember|memory|reminders?|calendar|"
    r"spotify|drive|wifi|wi-fi|internet|connection|network|microphone|mic|"
    r"speaker|notifications?|the\s+system|systems?)"
)
_DIAGNOSE_RE = re.compile(
    r"\bdiagnose\s+yourself\b"
    rf"|\bdiagnose\s+(?:the\s+|my\s+)?{_DIAGNOSE_KNOWN_SUBJECTS}\b"
    rf"|\bwhy\s+isn'?t\s+(?:the\s+|my\s+)?{_DIAGNOSE_KNOWN_SUBJECTS}\s+working\b",
    re.IGNORECASE,
)


def _check_status(message: str) -> "IntentResult | None":
    m = _STATUS_RE.search(message)
    if not m:
        return None
    device = m.group(1) if m.groups() and m.group(1) else None
    return IntentResult(intent="STATUS", confidence="high", target=device)


def _check_diagnose(message: str) -> "IntentResult | None":
    m = _DIAGNOSE_RE.search(message)
    if not m:
        return None
    target = next((g for g in m.groups() if g), None)
    return IntentResult(intent="DIAGNOSE", confidence="high", target=target.strip() if target else None)


# ---- CLEAR CONTEXT / NEW CONVERSATION --------------------------------------
_CLEAR_CONTEXT_RE = re.compile(
    r"^\s*clear\s+context\s*[.!]*\s*$"
    r"|\bforget\s+this\s+conversation\s+context\b"
    r"|\bstart\s+fresh\s+from\s+here\b",
    re.IGNORECASE,
)
_NEW_CONVERSATION_RE = re.compile(
    r"\bstart\s+a\s+new\s+conversation\b|\bnew\s+chat\b|\blet'?s\s+start\s+fresh\b",
    re.IGNORECASE,
)


def _check_clear_context(message: str) -> "IntentResult | None":
    if _CLEAR_CONTEXT_RE.search(message):
        return IntentResult(intent="CLEAR_CONTEXT", confidence="high")
    return None


def _check_new_conversation(message: str) -> "IntentResult | None":
    if _NEW_CONVERSATION_RE.search(message):
        return IntentResult(intent="NEW_CONVERSATION", confidence="high")
    return None


# ---- FIND -------------------------------------------------------------------
# "where is" tightened (this pass) after a real failure: "what time will
# the WEC race start? where is it happening?" was being swallowed whole by
# this checker — "where is" + bare optional "the"/"my" matched ANY
# "where is X" question, including ordinary real-world questions that
# have nothing to do with a personal item Ember might have in memory.
# FIND is meant for "where is my passport" / "where is the spare key" —
# genuinely personal-possession-shaped lookups — not "where is [event]
# happening", which is a normal factual question that belongs in
# search/chat, not a memory.recall() call for a query like "it
# happening" (exactly what shipped: memory.recall("it happening")
# returned unrelated stored notes and Ember confidently presented them as
# the answer). Requiring "my" (not "the", not bare) narrows this to the
# personal-possession shape the intent was actually designed for.
#
# "find" similarly narrowed (this pass, after auditing for the same class
# of bug): "find the WEC race schedule" / "find cheap flights to Austin"
# were both being swallowed as FIND (memory.recall) purely because "the"
# was accepted as a stand-in for "my" — but "the X" doesn't disambiguate
# personal possession from a generic search-shaped request the way "my X"
# does. Also excludes the phrasal verb "find out" (discover/determine) via
# a negative lookahead — "find out what time the race starts" means
# something structurally different from "find my keys" and was at real
# risk of the same hijack.
_FIND_RE = re.compile(
    r"\bfind(?!\s+out\b)\s+my\s+(.+)"
    r"|\bwhere\s+is\s+my\s+(.+)",
    re.IGNORECASE,
)


def _check_find(message: str) -> "IntentResult | None":
    m = _FIND_RE.search(message)
    if not m:
        return None
    target = next((g for g in m.groups() if g), "").strip().strip("?.!")
    if not target:
        return None
    return IntentResult(intent="FIND", confidence="high", target=target, parameters={"query": target})


# ---- SEND / CONTINUE (device sync — not built yet, see ember_core.py) -----
_SEND_RE = re.compile(r"\bsend\s+(?:this|that|it)\s+to\s+(?:my\s+)?(.+)", re.IGNORECASE)
_CONTINUE_RE = re.compile(
    r"\bcontinue\s+(?:this\s+)?(?:on\s+)?(?:my\s+)?(.+)"
    r"|\bmove\s+this\s+conversation\s+to\s+(?:my\s+)?(.+)",
    re.IGNORECASE,
)


def _check_send(message: str) -> "IntentResult | None":
    m = _SEND_RE.search(message)
    if not m:
        return None
    return IntentResult(intent="SEND", confidence="high", target=m.group(1).strip().strip(".!?"))


def _check_continue(message: str) -> "IntentResult | None":
    m = _CONTINUE_RE.search(message)
    if not m:
        return None
    target = next((g for g in m.groups() if g), "").strip().strip(".!?")
    return IntentResult(intent="CONTINUE", confidence="high", target=target or None)


# ---- Router -----------------------------------------------------------------
# Order matters: more specific / less ambiguous checks first. INTERRUPT
# goes first since a bare "stop" must never be caught by anything more
# general below it.
_CHECKERS = [
    _check_interrupt,
    # Local utility (time/date/day) — checked right after immediate
    # control and before every other command below, per the "use the
    # cheapest/local capability before escalating" routing priority:
    # these never touch the LLM or a search backend at all.
    _check_get_datetime,
    _check_get_time,
    _check_get_date,
    _check_get_day,
    _check_update_memory,
    _check_inspect_memory,
    _check_memory_search,
    _check_status,
    _check_diagnose,
    _check_clear_context,
    _check_new_conversation,
    _check_find,
    _check_send,
    _check_continue,
]


def classify(message: str) -> "IntentResult | None":
    """Entry point. Returns None immediately if no anchor word is even
    present (the common case — ordinary conversation never runs the
    checkers below). Returns the first checker's non-None result
    otherwise, or None if an anchor was present but no checker actually
    matched the sentence's structure (e.g. "the music stopped") — in
    which case the caller should fall through to normal chat handling,
    unchanged."""
    if not has_intent_anchor(message):
        return None
    for checker in _CHECKERS:
        result = checker(message)
        if result is not None:
            return result
    return None
