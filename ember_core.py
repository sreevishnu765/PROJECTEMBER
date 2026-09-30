"""
ember_core.py — Ember's orchestrator.

This is the piece that actually runs Ember: it owns the main loop, checks
that at least one backend is usable before starting, decides per-message
whether a query needs live search grounding, builds the system prompt
(including current date/time, to stop the model from guessing at "today"),
and calls down into llm_client.generate() for the actual model response.

llm_client.py stays a dumb adapter — it doesn't know about personas,
search heuristics, or conversation flow. All of that lives here.
"""

import os
import re
import sys
import time
from datetime import datetime, timedelta

import llm_client
from ember_memory import EmberMemory
from ember_session import EmberSession
from ember_conversation import EmberConversation, get_conversation_registry
import ember_confirmation
from ember_skills import SkillsLoader
from ember_proactive import ProactiveEngine
from ember_tools import app_launcher, pdf_export, vision_ocr
from ember_tools import computer as computer_tools
from ember_tools import spotify as spotify_tool
from ember_tools import calendar as calendar_tool
from ember_tools import drive as drive_tool
from ember_tools import file_ops
from ember_reminders import (
    ReminderStore, parse_natural_time, TIME_PHRASE_RE,
    parse_recurrence, RECURRENCE_PHRASE_RE,
)
from ember_files import FileRegistry
from ember_history import ConversationHistory
from ember_tools_registry import get_registry
from ember_query_registry import get_query_registry
from ember_bus import get_bus
from ember_notifications import NotificationManager
from ember_runtime import EmberRuntime
from ember_tools.google_auth_watchdog import GoogleAuthWatchdog
import ember_intent
import ember_research
import ember_attachments

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_MEMORY_DB_PATH = os.path.join(_PROJECT_ROOT, "data", "ember_memory.db")
memory = EmberMemory(_MEMORY_DB_PATH)

# Files panel's backing store (see ember_files.py) — records uploads-for-
# analysis and generated output (PDF exports, Drive downloads) as they
# happen, so a UI panel has something real to list instead of a
# placeholder. Constructed the same direct-singleton way as `memory`
# above, not through a lazy getter, matching this file's existing
# convention for every other durable store.
_FILES_LOG_PATH = os.path.join(_PROJECT_ROOT, "data", "files.json")
file_registry = FileRegistry(_FILES_LOG_PATH)

# Past Conversations panel's backing store (see ember_history.py) — a
# real, browsable, per-session archive, deliberately separate from
# ember_session.py's bounded crash-resume mirror (see that module's own
# docstring on why "not a transcript archive" was the right call for
# THAT store specifically, and ember_history.py's docstring on why this
# is a second, different one rather than loosening that cap).
_HISTORY_DB_PATH = os.path.join(_PROJECT_ROOT, "data", "ember_history.db")
conversation_history = ConversationHistory(_HISTORY_DB_PATH)

# The shared tool/capability registry (priority #1 of the post-Jarvis-port
# development phase) — see ember_tools_registry.py. Every executable
# capability Ember has is registered into this once, in
# _register_builtin_tools() below, instead of living as an if/elif branch
# inside this file.
_tool_registry = get_registry()
_query_registry = get_query_registry()

# Session-continuity layer (see ember_session.py for the full rationale) —
# a bounded, disk-mirrored copy of a conversation's history, used only to
# survive an unplanned restart within RESUME_WINDOW_MINUTES. Deliberately
# a separate store from `memory` (EmberMemory) — that one holds gated,
# durable FACTS; this one holds every raw turn, verbatim, capped and
# pruned by count, not by relevance. Different access pattern, different
# lifetime, different module — same separation-of-concerns reasoning as
# ember_skills.py's rules/ vs facts/ split.
#
# Ownership moved (this session) from a single global _session/_history
# pair to ember_conversation.EmberConversation — a prerequisite for the
# transport layer and multi-device connectivity, not a cosmetic refactor:
# the OLD design had no way to represent more than one conversation
# existing at once, locks or no locks. _default_conversation below is the
# CLI's own conversation — created the exact same way any future
# transport-connected client's conversation will be, through the same
# registry, so CLI and remote clients share one code path rather than two
# parallel implementations.
_conversation_registry = get_conversation_registry()
_default_conversation: EmberConversation = _conversation_registry.get_or_create(
    "cli-main", session_db_path=os.path.join(_PROJECT_ROOT, "data", "ember_session.db")
)

_skills = SkillsLoader(os.path.join(_PROJECT_ROOT, "skills"))
_confirm_gate = ember_confirmation.get_confirmation_gate()
_reminder_store = ReminderStore(
    path=os.path.join(_PROJECT_ROOT, "data", "reminders.json"),
    # notify_fn is intentionally a no-op now: ember_reminders.py's poll
    # loop already publishes "reminder.due" onto the shared event bus on
    # every fire, and NotificationManager (wired in _build_runtime()
    # below) is what actually prints it via console_channel. Keeping a
    # second direct-print notify_fn here would double the output — this
    # parameter still exists (and ember_reminders.py still calls it) for
    # any consumer that isn't on the bus, which is nobody, currently.
    notify_fn=lambda msg: None,
)

# ---- Short-term conversation history -------------------------------------
# Session-only (resets on restart, not persisted to disk — that's a
# separate, larger feature already on the candidates list). Without this,
# follow-up questions like "what about X?" are structurally unanswerable:
# the model never sees what X is being compared to. Long-term EmberMemory
# is a different thing entirely — durable facts, gated recall, disk-backed.
# This is just "what did we just say," kept small and cheap.
#
# Each entry: {"role": "user"|"assistant", "content": str, "used_search": bool}
# used_search is only meaningful on assistant entries — it's how the
# intent router later infers "was the last topic something we searched
# for," to resolve bare follow-ups like "what about Kevin Estre?".
HISTORY_MAX_TURNS = 6  # 3 exchanges — enough for follow-up context, small enough to stay cheap

# _history/_append_history kept as module-level names (rather than forcing
# every call site to spell out _default_conversation) for backward
# compatibility with existing code in this file — both are now thin
# wrappers over _default_conversation, the CLI's own EmberConversation.
# _history is a live PROPERTY read (always the current snapshot), not a
# stale list captured once at import time — important now that more than
# one EmberConversation can exist and the CLI's own history object is
# just one of them, owned by the registry above, not by this variable.


def _history_snapshot() -> list:
    return _default_conversation.snapshot_history()


def _append_history(role: str, content: str, used_search: bool = False, conversation: "EmberConversation | None" = None):
    """Defaults to the CLI's own conversation when no conversation is
    given — every pre-existing call site in this file keeps working
    unchanged. A future transport handler passes its own conversation
    explicitly instead.

    Also archives the turn into conversation_history (see ember_history.py)
    keyed by that conversation's session_id — this is the ONE place every
    turn from every call site already flows through, so it's the natural
    place to feed the real, browsable Past Conversations store too,
    rather than duplicating this call at every one of process_turn()'s
    several _append_history call sites."""
    target = conversation or _default_conversation
    target.append(role, content, used_search)
    conversation_history.append(target.session_id, role, content, used_search)


BASE_PERSONA = """You are Ember, a personal AI assistant. Address the user as "sir."
Speak in a composed, dry-witted, understated manner — think a capable aide,
not an enthusiastic chatbot. Match your reply's length to the input: a
one-line greeting or remark gets a one-line reply, not a status report on
the user's day and not a follow-up question tacked on by default. Only
ask what's needed if the request is genuinely ambiguous. Avoid stock
assistant phrasing — no "How may I assist you today," no "I trust that...".
Default to 1-2 sentences for casual remarks and simple questions — but
when the request itself signals it wants depth (asking for a "report,"
"summary," "breakdown," "details," to "elaborate," or for multiple
distinct pieces of information), give the full, structured answer
immediately. Don't make the user ask twice for something they already
asked for the first time. Never remind the user that you are an AI or
apologize for being one. Do not use exclamation marks unless something is
genuinely surprising."""


# Appended to the system prompt for turns whose reply will be READ ALOUD (a voice-originated turn, or
# a typed one with the speaker on — ember_transport.py sets conversation.spoken_reply). Without it,
# "what can you tell me about jet engines" comes back as a multi-section markdown report: slow to
# generate, slow to speak, and painful to listen to. It deliberately overrides BASE_PERSONA's
# "give the full structured answer" rule for report-shaped requests.
SPOKEN_ADDENDUM = (
    "SPOKEN REPLY: this answer will be read aloud by a speech synthesizer, so write it the way you "
    "would say it. Plain sentences only: no markdown, no headings, no bullet points, no tables, no "
    "emoji, no URLs. Put the answer in the first sentence. Keep it to one to three sentences unless "
    "the user explicitly asked for detail; even then stay under about six sentences, and offer to go "
    "deeper instead of covering everything. This overrides any earlier instruction to give structured "
    "reports."
)


def build_system_prompt(memory_context: str = "") -> str:
    """Assemble the system prompt fresh each turn so the date/time is current.
    memory_context, if present, is recalled facts relevant to this turn —
    injected as context, not as instructions the model should follow."""
    now = datetime.now().strftime("%A, %B %d, %Y — %I:%M %p")
    prompt = f"{BASE_PERSONA}\n\nCurrent date and time: {now}."

    rules_addendum = _skills.get_system_prompt_addendum()
    if rules_addendum:
        prompt += f"\n\n{rules_addendum}"

    if memory_context:
        prompt += (
            "\n\nRelevant things you know about the user from past conversations "
            f"(use naturally, don't recite this list):\n{memory_context}"
        )
    return prompt


# ---- Search heuristic ------------------------------------------------------
# Decides whether a given message plausibly needs live/current information
# looked up via Google Search grounding, versus something the model can
# answer from its own knowledge. Grounding is opt-in per call (see
# llm_client.generate()'s use_search flag) to conserve the free-tier
# request quota rather than grounding every single message.
#
# This is a heuristic, not a classifier — it errs toward cheap, fast
# pattern-matching. It will have false negatives (missed queries that
# actually needed a search) and false positives (unnecessary grounding).
# Refine as real usage surfaces gaps.

_SEARCH_TRIGGER_PATTERNS = [
    r"\btoday\b", r"\byesterday\b", r"\btonight\b",
    # Consolidated into one alternation covering "this week/weekend/season/
    # year" -- the earlier "\bthis week\b" alone doesn't match "weekend"
    # (\b requires a word boundary right after "week", which "weekend"
    # never has); "season"/"year" were never covered at all, so "what
    # teams are racing this season" got zero search-trigger hits.
    r"\bthis\s+(?:week|weekend|season|year)\b",
    # "next X" had NO coverage at all before this — only "when is the next
    # race" happened to work, via the separate "when is" trigger, not
    # because "next" itself was recognized. "what teams are racing next
    # season" and "next weekend the WEC races" both got zero hits.
    r"\bnext\s+(?:week|weekend|season|year|round|race)\b",
    r"\bcurrent(ly)?\b", r"\blatest\b", r"\brecent(ly)?\b", r"\bnow\b",
    r"\bnews\b", r"\bweather\b", r"\bscore\b", r"\bstock\b", r"\bprice\b",
    r"\bwho is\b", r"\bwho'?s\b", r"\bwhen is\b", r"\bwhen'?s\b",
    # "who won"/"who was the winner" had NO coverage — only "who is/who's"
    # was recognized, so "who won the last WEC race?" fell through to
    # plain ungrounded chat every time. Real, reproduced failure: this
    # combined with the bare-followup gap below to produce a confidently
    # fabricated, detailed "race report" with invented driver names.
    r"\bwho won\b", r"\bwho (?:was|is) the (?:winner|champion|winning\s+\w+)\b",
    r"\bwhat happened\b", r"\breleased?\b", r"\bupcoming\b",
    r"\bschedule\b", r"\bdeadline\b", r"\b20\d{2}\b",  # any 4-digit year 2000-2099
    # Real, reproduced regression: "give me the race report on the f1
    # madrid gp" (no year, no "who won") matched NOTHING here, so it went
    # straight to plain chat — and the persona fix that makes Ember give
    # full detail immediately for "report"-shaped requests turned that
    # ungrounded gap into a confident, detailed, entirely fabricated
    # report on the very first reply, worse than the old short-guess
    # behavior. Broadened per direct feedback: this isn't just a
    # motorsport problem — any "report" on a real-world event (news,
    # an election, a disaster, a launch) has the same failure shape.
    # Two patterns: a small enumerable noun list (same "small vocabulary
    # -> heuristic list" reasoning used everywhere else in this file) for
    # the common NOUN-first phrasing ("race report", "election results"),
    # plus a general "report on/about/of X" pattern for anything else —
    # guarded so it backs off when X sounds personal ("report on my
    # workouts", "summary of what we discussed"), which should go to
    # memory recall instead of the web, not get swept into search here.
    rf"\b(?:race|match|game|election|tournament|summit|trial|hurricane|earthquake|ceasefire|launch)\s+(?:report|recap|summary|results?)\b",
    r"\b(?:detailed\s+|full\s+|brief\s+)?(?:report|recap|breakdown|summary)\s+(?:on|about|of)\s+"
    r"(?!my\b|our\b|your\b)(?!.*?\b(?:remember|we discussed|we talked|earlier|before|last time|previously)\b)",
]

_SEARCH_TRIGGER_RE = re.compile("|".join(_SEARCH_TRIGGER_PATTERNS), re.IGNORECASE)


def needs_search(message: str) -> bool:
    """Return True if `message` looks like it needs current/live information."""
    return bool(_SEARCH_TRIGGER_RE.search(message))


# ---- Intent layer: why regex AND tool-calling, not one or the other ------
# jarvisforember (a friend's comparable assistant, reviewed in full) solves
# "does this need a search" completely differently from the regex approach
# below: it hands the model a `background_search` function via native
# tool-calling and lets the MODEL decide, rather than pre-classifying with
# keywords. That's semantically better — no regex list can ever cover every
# phrasing of "this needs current info" — and as of this change, Ember uses
# that same mechanism for every OpenAI-compatible fallback tier (Cerebras,
# Groq, NVIDIA NIM, Mistral). See llm_client.py's WEB_SEARCH_TOOL_SCHEMA and
# _generate_openai_compatible for the actual tool-calling loop.
#
# This is specifically what fixes the reported Groq hallucination: the root
# cause wasn't Groq being a worse model, it was that `use_search` never
# reached _generate_openai_compatible at all — a search-flagged query that
# fell through to Groq got no grounding, no tool, nothing, and Groq
# confidently answered from stale training data. Groq now gets a real
# search tool it can (and, per the system-prompt directive, is told to)
# call.
#
# The regex layer below is NOT being retired, for two concrete reasons:
#   1. Gemini's native Google Search grounding is quota-metered on a tight
#      20 RPD tier. Letting the model decide "should I ground this" on
#      every single cloud call would burn that budget fast — needs_search()
#      stays as the gate for Gemini specifically, same as before.
#   2. Local Ollama (qwen2.5:7b-instruct) is not a reliable native
#      tool-caller — small local models routinely ignore tool schemas or
#      emit malformed calls. It has no autonomous option, so it still needs
#      *something* deciding whether this message was current-info-shaped,
#      even though today that only produces an honest "answered without
#      grounding" caveat rather than actual evidence (a full local-evidence
#      injection path is a reasonable next step, not implemented here).
#
# So: regex is the fast, free, always-available pre-filter and the only
# option for Gemini/local; tool-calling is the semantic safety net
# underneath it for every fallback tier, catching what the regex misses.
# ---- Intent layer --------------------------------------------------------
# Real usage surfaced a concrete failure: a follow-up ("what about Kevin
# Estre?") got no search because it matched no keyword, and a correction
# ("you're hallucinating, recheck that") got sent back to the same model
# with no new information, which just self-reflected ("I reviewed the
# record") instead of actually checking anything. needs_search() alone
# can't distinguish "chat" from "needs live info" from "my prior answer
# needs checking" — this layer sits on top of it and does that split,
# using recent history to resolve what a bare keyword match can't.
#
# Same governing rule as every other heuristic in this file: cheap
# pattern-matching, not a classifier, will have gaps, refine as usage
# surfaces them. The one qualitative upgrade over "add more keywords"
# (which was tried and rejected) is using conversation history as a
# signal, not just the current message in isolation.

# Action/task language should NOT trigger search even when it contains a
# time word ("remind me to do laundry tonight" has "tonight" but wants a
# reminder, not a web search). "remind me to..." itself is now a real,
# fully-implemented action (see _REMINDER_CREATE_RE / ember_reminders.py
# above, checked earlier in _classify_action) — this override is the
# fallback for the surrounding task-language category that hasn't been
# promoted to a concrete action yet (shopping-list-style phrasing like
# "add milk to my list", generic "my to-dos" mentions not shaped like a
# clean create/list/cancel command). It still just prevents a wasted
# search call for those; it doesn't route them anywhere real.
_ACTION_OVERRIDE_PATTERNS = [
    r"\bremind me\b", r"\bset a reminder\b", r"\bmy (task|to-?do)s?\b",
    r"\bour (task|to-?do)s?\b", r"\badd .* to (my|our|the) list\b",
    r"\bnote to self\b",
]
_ACTION_OVERRIDE_RE = re.compile("|".join(_ACTION_OVERRIDE_PATTERNS), re.IGNORECASE)


def _looks_like_action_request(message: str) -> bool:
    return bool(_ACTION_OVERRIDE_RE.search(message))


# Explicit verification/correction language — "you said X, check it" is a
# fundamentally different request from "tell me about X." This is what was
# completely unhandled before: none of these phrases matched any existing
# trigger, so the model just got asked to reconsider from the same
# knowledge that produced the wrong answer in the first place.
_VERIFY_TRIGGER_PATTERNS = [
    r"\byou'?re (hallucinating|wrong)\b", r"\bhallucinat\w*\b",
    r"\bre-?check\b", r"\bverify\b", r"\bare you sure\b",
    r"\bdouble[- ]check\b", r"\bthat'?s not (right|correct|true)\b",
    r"\bfact[- ]?check\b", r"\bcheck (that|this) again\b",
    r"\bconfirm that\b", r"\bare you certain\b",
]
_VERIFY_TRIGGER_RE = re.compile("|".join(_VERIFY_TRIGGER_PATTERNS), re.IGNORECASE)


def _is_verification_request(message: str) -> bool:
    return bool(_VERIFY_TRIGGER_RE.search(message))


# "Tell me about X" / "who is X" / "what about X" / "what's X" is a
# structural pattern, not a keyword — it catches lookup questions
# regardless of which specific subject is asked about, which a flat
# keyword list fundamentally cannot do. Deliberately case-insensitive:
# an earlier version of this required a capitalized name (proper-noun
# shape), which failed on the actual reported bug — "what can you tell
# me about max verstappen" was typed all lowercase, same as most real
# chat input. Instead, a small stopword filter distinguishes "naming a
# subject" (triggers) from generic filler like "what about you" or "tell
# me about yourself" (doesn't).
_TOPIC_LOOKUP_RE = re.compile(
    r"\b(?:tell me about|who is|who'?s|what about|what'?s)\s+((?:[a-zA-Z'-]+\s*){1,4})",
    re.IGNORECASE,
)
_TOPIC_LOOKUP_STOPWORDS = {
    "me", "you", "it", "that", "this", "yourself", "your", "my", "the",
    "a", "an", "doing", "up", "going", "on", "there", "here",
}


# Real failure, reported directly: "what's the integral of x dx?" and
# "what's the derivative of x^2?" both got misclassified as needing a
# live web search — traced to _TOPIC_LOOKUP_RE above, which structurally
# cannot distinguish "what's X" meaning "tell me about this
# person/current-event topic" from "what's X" meaning "give me this
# static/computed/definitional fact." Confirmed via testing this isn't
# math-specific: "what's the capital of France?" triggers the IDENTICAL
# false positive, since "capital"/"France" aren't stopwords either.
#
# Deliberately a SMALL, CLOSED class of English relational/definitional
# nouns below — not an attempt to enumerate every timeless topic in
# existence (which really would be the unbounded blocklist explicitly
# ruled out; there's no finite list of "facts that don't change"). What
# IS finite and genuinely enumerable is the small set of common nouns
# that structurally signal "the person is asking for a fixed,
# computable, or definitional relationship between two things," not
# information about a real-world entity's current state — the same
# "small enumerable vocabulary -> heuristic list, not open-ended
# capture" reasoning already used for _DIAGNOSE_KNOWN_SUBJECTS and
# _LAUNCH_APP_STOPWORDS elsewhere in this file. "what's the latest on
# X"/"what's the status of X"/"who is the president of X" are all
# untouched by this — none of those head nouns are in the list below,
# so they still correctly fall through to _mentions_named_person's
# normal (search-triggering) path.
_STATIC_LOOKUP_RE = re.compile(
    r"\bwhat(?:'?s|\s+is)\s+the\s+"
    r"(?:square\s+root|cube\s+root|boiling\s+point|freezing\s+point|melting\s+point|"
    r"atomic\s+number|atomic\s+weight|chemical\s+formula|capital|definition|meaning|"
    r"synonym|antonym|opposite|plural|derivative|integral|factorial|sum|product|"
    r"quotient|square|cube|value)\s+(?:of|for)\b",
    re.IGNORECASE,
)


def _is_static_lookup(message: str) -> bool:
    return bool(_STATIC_LOOKUP_RE.search(message))


def _mentions_named_person(message: str) -> bool:
    if _is_static_lookup(message):
        return False
    match = _TOPIC_LOOKUP_RE.search(message)
    if not match:
        return False
    words = match.group(1).strip().split()
    if not words:
        return False
    return not all(w.lower() in _TOPIC_LOOKUP_STOPWORDS for w in words)


# Bare follow-ups ("what about X?", "is he still there?", "and now?") only
# make sense read against the immediately preceding topic. If that topic
# was itself search-flagged, the follow-up almost certainly needs the same
# treatment — this is the one place history actually changes the routing
# decision, not just the model's answer.
_FOLLOWUP_PATTERNS = [
    r"^\s*(and\s+)?what about\b", r"^\s*(and\s+)?what'?s\b",
    r"^\s*(is|does|did|was|were|has|have)\s+(he|she|they|it)\b",
    r"^\s*(and\s+)?(now|still)\b",
    # "which X" added (this pass) -- a real gap found via audit: "which
    # team is fielding the 911s" right after a search-grounded reply
    # didn't inherit search at all, since nothing in this list recognized
    # "which" as a bare-continuation shape the way "what about"/"is he"
    # already were. Still bounded by the same <=8-word cap below, so a
    # longer "which"-led sentence about something unrelated isn't swept
    # in just for starting with the word.
    r"^\s*(and\s+)?which\b",
    # "what <noun> is/was X" had no coverage — only the contracted "what's"
    # was recognized, so "what time is the race?" right after a grounded
    # "next WEC race" answer fell through to plain chat and got a guessed,
    # ungrounded answer. Reproduced twice (a race start time, then a race
    # date) — this is a genuine structural gap, not a one-off phrasing.
    r"^\s*(and\s+)?what\s+\w+\s+(?:is|was|does|did)\b",
    # "give me a report/summary/details on it" — the other reproduced
    # failure: a follow-up asking for MORE detail on the prior (possibly
    # ungrounded) topic, referencing it only as "it"/"that"/"this". This
    # is what let a single missed "who won" trigger snowball into an
    # entire fabricated, detailed race report.
    r"^\s*(?:give|tell)\s+me\s+(?:a\s+)?(?:more\s+)?(?:detailed\s+)?(?:report|summary|breakdown|details?)\b.*\b(?:it|that|this)\b",
]
_FOLLOWUP_RE = re.compile("|".join(_FOLLOWUP_PATTERNS), re.IGNORECASE)

# "What about you?" / "and you?" refers back to Ember/the user themselves,
# not a continuation of whatever external topic was just discussed —
# without this exclusion, that extremely common conversational phrase
# wrongly inherits search from an unrelated prior topic.
_FOLLOWUP_SELF_REFERENCE_RE = re.compile(r"\b(?:about|and)\s+(you|me)\b", re.IGNORECASE)


def _is_bare_followup(message: str) -> bool:
    if _FOLLOWUP_SELF_REFERENCE_RE.search(message):
        return False
    return bool(_FOLLOWUP_RE.search(message)) and len(message.split()) <= 8


# ---- Explicit search request ----------------------------------------------
# Real gap surfaced directly: the person typing "search it" after a
# contested claim got zero search grounding — nothing in needs_search()/
# _mentions_named_person() recognizes an explicit instruction to search at
# all, only implicit keyword/topic shapes. This closes that: a plain "search
# / look up / google (it/that/for X)" is the person deliberately asking for
# live grounding, which should outrank the automatic heuristics below it,
# not be silently ignored by them.
#
# The negative lookahead is the fix for the exact collision flagged before
# building this: "look up my schedule"/"...calendar"/"...reminders" etc.
# name an EXISTING local capability (the calendar tool, the reminder list)
# that should handle the request itself, not get shipped off to Tavily/
# Gemini as if it were an open-ended web query. Since ember_tools_registry's
# action layer is checked before this function ever runs (see
# classify_intent()), a phrase this lookahead excludes still gets a chance
# to be handled by whichever local tool actually recognizes it (see the
# _CALENDAR_CHECK_RE change alongside this) — excluding it here only means
# "don't ALSO treat this as a generic web search," not "do nothing with it."
_EXPLICIT_SEARCH_RE = re.compile(
    r"\b(?:search|google|look\s*up)\b"
    r"(?!\s+(?:my|our|the)\s+(?:schedule|calendar|reminders?|tasks?|to-?dos?|files?|notes?|inbox|emails?)\b)"
    # memory/memories pulled out of the possessive-required list above and
    # given its own lookahead (fixed in the prior session) -- a bare
    # "search memory"/"look up memory" (no "my"/"our"/"the") was still
    # matching the alternation above and getting routed to a live web
    # search, since the original lookahead only ever excluded the
    # possessive form. Unlike schedule/calendar/notes/etc, there's no
    # legitimate reading of bare "search memory" as a request for
    # external search -- it always means the local EmberMemory store.
    # Kept as defense-in-depth alongside ember_intent.py's
    # _check_memory_search, which now claims these messages as a command
    # before this regex is even reached in classify_intent()'s order.
    r"(?!\s+(?:(?:my|our|the)\s+)?memor(?:y|ies)\b)"
    # "find (out) the/what/when/who/where/why/how X" added (this pass) --
    # a real gap this created: FIND (the command intent) now correctly
    # requires "find my X" (see ember_intent.py) to avoid hijacking
    # search-shaped questions, but that left "find the race start time"
    # matching NOTHING at all -- it fell straight to plain chat and Ember
    # confidently invented a specific, unverified time. Deliberately NOT
    # a bare \bfind\b -- that word is far too common in ordinary
    # conversation ("I find this confusing," "you'll find that...") to
    # use as a blanket trigger. Requiring an immediate determiner/WH-word
    # right after "find (out)" narrows this to the imperative "go find
    # out X" shape without catching incidental uses of the word.
    r"|\bfind(?:\s+out)?\s+(?:the|what|when|who|where|why|how)\b",
    re.IGNORECASE,
)


def _is_explicit_search_request(message: str) -> bool:
    return bool(_EXPLICIT_SEARCH_RE.search(message))


def _recent_turn_used_search(history: list) -> bool:
    for turn in reversed(history):
        if turn["role"] == "assistant":
            return turn["used_search"]
    return False


def _find_last_topic(history: list) -> "str | None":
    """Most recent user message that looks like an actual topic, not a
    bare correction/follow-up itself — used to give a verification request
    something concrete to check when the correction message alone
    ('recheck that') doesn't name anything."""
    for turn in reversed(history):
        if turn["role"] == "user" and not _is_verification_request(turn["content"]):
            return turn["content"]
    return None


# ---- Action intent (skill/tool dispatch) ---------------------------------
# The one branch of the original architecture diagram
# (chat / search / memory / action / proactive) that had no implementation
# at all before this pass — classify_intent only ever returned chat/search
# /verify, so "open Notepad" or "export that as a PDF" just got sent to the
# model as a chat message, which can describe how to do it but can't
# actually do it.
#
# Deliberately regex-based, NOT routed through the fallback tiers' new
# native tool-calling loop (see llm_client.py), even though that mechanism
# now exists and works. Two concrete reasons:
#   1. Proportionality: the search decision needed tool-calling because
#      it's genuinely open-ended — "does this message need current info"
#      has effectively unlimited phrasings, and a regex list will always
#      have gaps (that's the actual bug this session fixed). The action
#      vocabulary here is three enumerable, imperative command shapes
#      ("open X", "export as pdf", "analyze image at <path>") — much
#      closer in kind to the existing _ACTION_OVERRIDE_PATTERNS/
#      _VERIFY_TRIGGER_PATTERNS than to the search problem. If/when this
#      vocabulary grows large or loosely-phrased enough that regex starts
#      missing real cases the way needs_search() did, migrate this to the
#      same tool-calling mechanism — not before.
#   2. Untested risk: giving Gemini native custom function-declarations
#      (as opposed to its built-in Search grounding tool) would require
#      restructuring _generate_cloud's flat-string prompt into structured
#      multi-turn Content objects, and there's no way to verify that
#      against the real API from this sandbox. Rather than ship an
#      unverified change to the primary/best-quality tier, action dispatch
#      here never touches an LLM at all — pure deterministic execution, so
#      there's nothing to get subtly wrong at the API layer.
#
# clear_memory is deliberately included as the one destructive example,
# routed through ember_confirmation's gate — proving the confirmation
# mechanism actually fires end-to-end, not sitting unused as
# infrastructure. Future destructive tools should follow the same pattern.
#
# REFACTORED (this pass) to route through ember_tools_registry.py instead
# of a hand-written if/elif chain — see that module's docstring for the
# full rationale (priority #1 of the post-Jarvis-port development phase:
# a scalable capability layer instead of ember_core.py growing a new
# branch per feature). classify_intent()/run() below now just ask the
# registry whether a message matches anything, and dispatch through it —
# they no longer know the individual tool list at all. Adding a new
# capability (calendar, weather, GitHub, ...) means adding one
# registry.register(...) call in _register_builtin_tools() below; nothing
# in classify_intent(), run(), or the intent-routing logic changes.
# "start" deliberately dropped from this trigger (kept only "open"/
# "launch") — found via real testing this session: "start" is far too
# overloaded in ordinary English ("start date", "start a new
# conversation", "start fresh") to safely mean "launch an application."
# "open"/"launch" don't have that ambiguity. This directly fixes a
# collision with two of ember_intent.py's own brief-mandated phrasings
# (NEW_CONVERSATION's "start a new conversation" and an UPDATE_MEMORY
# example containing "start date") that this tool was silently
# intercepting before either could be classified correctly.
_LAUNCH_APP_RE = re.compile(r"\b(?:open|launch)\s+(.+)", re.IGNORECASE)
_PDF_EXPORT_RE = re.compile(
    # Real, reproduced failure: "export previous reply as a pdf" didn't
    # match the old pattern at all, because it only allowed a bare
    # "this/that/it" (or nothing) directly between "export" and "pdf" —
    # any other words in between (like "previous reply") broke the match
    # completely. That silently sent the message to plain chat, where the
    # model then FABRICATED an entire fake destination-path negotiation
    # ("Corrected to... I will save the file to D:\...") despite this
    # tool never taking a destination argument at all — a hallucinated
    # capability, not just a missed trigger. The bounded .{0,40} gap
    # below allows realistic phrasing in between while still requiring
    # both words close together, so it doesn't fire on unrelated
    # messages that happen to mention "export" and "pdf" far apart.
    r"\bexport\b.{0,40}\bpdf\b|\bsave\b.{0,40}\bas\s+a\s+pdf\b"
    r"|\bturn\b.{0,40}\bpdf\b|\bmake\s+(?:this|that|it)\s+a\s+pdf\b",
    re.IGNORECASE | re.DOTALL,
)
# Optional filename override for PDF export ("...under filename X",
# "...as filename X", "...named X") — searched separately against the
# full message in the handler, since _PDF_EXPORT_RE above has no
# capturing group of its own for it.
_PDF_FILENAME_RE = re.compile(r"(?:under|as|named)\s+(?:the\s+)?filename\s+([^\s.,!?]+)", re.IGNORECASE)
_ANALYZE_IMAGE_RE = re.compile(
    r"\b(?:analyz|read)\w*\s+(?:this\s+|that\s+|the\s+)?(?:image|document|screenshot|photo)\s*(?:at\s+)?(.+)?",
    re.IGNORECASE,
)
# Screen capture (brief item #8's missing half — see vision_ocr.py's
# analyze_screenshot): "analyze this screenshot" with NO path given means
# "take one and look at it," not "here's a file." Checked BEFORE the
# generic _ANALYZE_IMAGE_RE above (registration order = match priority)
# so a no-path screen/screenshot mention takes the capture-and-analyze
# path instead of falling into "I need a file path, sir."
_ANALYZE_SCREEN_RE = re.compile(
    r"\b(?:analyz|read|look at|check)\w*\s+(?:my\s+|this\s+|the\s+)?screen(?:shot)?\b(?!\s+at\s)"
    r"|\bwhat'?s?\s+is\s+on\s+(?:my\s+)?screen\b"
    r"|\bwhat'?s\s+on\s+(?:my\s+)?screen\b",
    re.IGNORECASE,
)
_CLEAR_MEMORY_RE = re.compile(r"\bclear\s+(?:all\s+)?(?:my\s+)?memor(?:y|ies)\b|\bforget\s+everything\b", re.IGNORECASE)
_FORGET_SPECIFIC_RE = re.compile(r"\bforget\s+(?:that\s+i\s+said\s+|about\s+)?(.+)", re.IGNORECASE)
_ALLOW_PATH_RE = re.compile(r"\ballow\s+(?:ember\s+(?:to\s+)?)?access(?:\s+to)?\s+(.+)", re.IGNORECASE)
_LIST_DIR_RE = re.compile(r"\blist\s+(?:the\s+)?files?\s+(?:in|at)\s+(.+)|\bwhat'?s\s+in\s+(?:the\s+)?(?:directory|folder)\s+(.+)", re.IGNORECASE)
_READ_FILE_RE = re.compile(r"\bread\s+(?:the\s+)?file\s+(?:at\s+)?(.+)", re.IGNORECASE)
_RUN_SCRIPT_RE = re.compile(r"\b(?:run|execute)\s+(?:the\s+)?script\s+(?:at\s+)?(.+)", re.IGNORECASE)

# ---- Picked up from Nexus VII (this pass) --------------------------------
# Spotify/Calendar/Drive — named directly as example future capabilities in
# the next-phase brief ("calendar, weather, Spotify, email, GitHub,
# computer control, etc."). See ember_tools/{spotify,calendar,drive}.py for
# the real bugs found and fixed in each during the port.
_SPOTIFY_PLAY_RE = re.compile(r"\bplay\s+(.+?)\s+on\s+spotify\b|\bspotify\s+play\s+(.+)", re.IGNORECASE)
_CALENDAR_CHECK_RE = re.compile(
    r"\b(?:what'?s|check|show|look\s*up)\s+(?:on\s+)?(?:my\s+)?(?:calendar|schedule)\s*(?:for\s+)?(.+)?"
    r"|\bmy\s+(?:calendar|schedule)\s+(?:for\s+)?(.+)?",
    re.IGNORECASE,
)
_CALENDAR_ADD_RE = re.compile(
    # Real, reproduced bug: "schedule X at 4pm tomorrow" used to consume
    # the literal "at" as a bare separator, leaving group(2) as just
    # "4pm tomorrow" — and parse_natural_time() only recognizes a bare
    # time when it's preceded by "at " or written as "N:NN"; a lone
    # "4pm" with neither matched nothing, silently defaulting to 9am.
    # The connector is now captured INSIDE the time group so "at" always
    # survives through to parse_natural_time() intact. Confirmed against
    # the real created Google Calendar event, which really was at 9am.
    r"\bschedule\s+(.+?)\s+((?:for|on|at)\s+.+)"
    r"|\badd\s+(.+?)\s+to\s+(?:my\s+)?calendar\s+((?:for|on|at)\s+.+)",
    re.IGNORECASE,
)
# Real, reproduced bug: there was no way to correct a just-created
# event's time at all — no update/edit capability existed anywhere in
# calendar.py. A short correction like "4pm, ember." matched nothing,
# fell through to plain chat, and the model fabricated a fake "Corrected
# to 4:00 PM, sir" reply (even reusing the real event link) while the
# actual event silently stayed wrong. Deliberately narrow: only matches
# a short, mostly-bare time expression, optionally with a correction
# verb or addressed to Ember — broad enough for natural phrasing, narrow
# enough that it won't hijack an unrelated sentence that happens to
# contain a time. The handler above falls back to an honest "nothing to
# correct" reply when there's no event from this session to apply it to.
_CALENDAR_CORRECT_RE = re.compile(
    r"^\s*(?:actually,?\s*)?(?:make (?:it|that)\s+|change (?:it|that\s+)?to\s+|correct(?:ed)?\s+to\s+)?"
    r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*(?:,?\s*ember)?[.!]?\s*$",
    re.IGNORECASE,
)
# ---- File operations by name (ember_tools/file_ops.py) ----------------------
# "move the drivetrain pdf to documents" / "rename it to final" / "delete that
# file" / "undo that". The regexes only capture the tail after the verb; the
# verb-specific parsing (where the file name ends and the destination begins)
# happens in file_ops.py, which tries each " to "/" into " position. Each tool
# is also guarded by file_ops.looks_like_file_request so "move the meeting to
# friday" never reaches a file tool.
_FILE_MOVE_RE = re.compile(r"\b(?:move|relocate)\s+(?P<tail>.+)$", re.IGNORECASE)
_FILE_RENAME_RE = re.compile(r"\brename\s+(?P<tail>.+)$", re.IGNORECASE)
_FILE_DELETE_RE = re.compile(r"\b(?:delete|trash)\s+(?P<tail>.+)$", re.IGNORECASE)
_FILE_OPEN_RE = re.compile(r"\bopen\s+(?P<tail>.+)$", re.IGNORECASE)
_FILE_REVEAL_RE = re.compile(r"\b(?:show|reveal)\s+(?P<tail>.+?)\s+in\s+(?:the\s+)?(?:file\s+)?(?:explorer|folder|finder)\b", re.IGNORECASE)
_FILE_UNDO_RE = re.compile(r"\bundo\s+(?:that|it|the\s+last\s+(?:file\s+)?(?:move|rename|delete|deletion|operation|action))\b", re.IGNORECASE)
_DRIVE_DOWNLOAD_RE = re.compile(r"\bdownload\s+(?:this\s+|that\s+)?(?:from\s+)?(https?://drive\.google\.com/\S+)", re.IGNORECASE)

# Real capability gap, not just a missed trigger: there was NO way to
# cancel or reschedule a calendar event by name anywhere in the code.
# "cancel dentist meeting" / "reschedule the team meeting to wednesday"
# matched nothing at all, fell through to plain chat, and the model was
# free to fabricate an entire fake confirmation-and-execution exchange
# with no real Calendar API call behind it. These are real, destructive
# (cancel) or state-changing (reschedule) actions now — see their
# registration below for confirm-gating.
#
# A trailing calendar-ish noun (meeting/appointment/event/sync/call/
# review) is required so this doesn't collide with the existing reminder
# cancellation path (ember_intent.py's STOP_TASK handles "cancel the
# oven reminder" — no calendar noun there, so it's untouched).
_CALENDAR_NOUN = r"(?:meeting|appointment|event|sync|call|review)"
_CALENDAR_CANCEL_RE = re.compile(
    rf"\bcancel\s+(?:the\s+|my\s+)?(.+?)\s*{_CALENDAR_NOUN}\b",
    re.IGNORECASE,
)
_CALENDAR_RESCHEDULE_RE = re.compile(
    rf"\breschedule\s+(?:the\s+|my\s+)?(.+?)\s*{_CALENDAR_NOUN}?\s*(?:to|for)\s+(.+)",
    re.IGNORECASE,
)
# Real, reproduced bug: "please cancel that" right after a calendar check
# (no calendar noun, just a pronoun referring to the just-shown event)
# matched NEITHER pattern above — it has no "meeting/appointment/..."
# word at all. That fell through to chat and the model fabricated an
# entire fake cancel confirmation, including a fake excuse ("calendar
# sync is slow") when caught not having actually worked. These rely on
# _dispatch_context["last_calendar_event"], populated by check_calendar
# (single-event case) or add_calendar_event — see those handlers.
# Deliberately end-anchored and narrow (bare pronoun, optional "please"):
# broad enough for the natural phrasing, narrow enough not to hijack an
# unrelated sentence that happens to contain the word "that."
_CALENDAR_CANCEL_PRONOUN_RE = re.compile(r"^\s*(?:please\s+)?cancel\s+(?:that|it|this)\b[.!]?\s*$", re.IGNORECASE)
_CALENDAR_RESCHEDULE_PRONOUN_RE = re.compile(r"^\s*(?:please\s+)?reschedule\s+(?:that|it|this)\s+(?:to|for)\s+(.+)", re.IGNORECASE)

# Reminders/tasks — real implementation backed by ember_reminders.py,
# now including recurrence ("remind me every Sunday to update the model")
# per the next-phase brief's explicit task-system requirements.
_REMINDER_CANCEL_RE = re.compile(r"\bcancel\s+reminder\s+(\S+)", re.IGNORECASE)
_REMINDER_CREATE_RE = re.compile(r"\bremind me\s+(?:to\s+)?(.+)", re.IGNORECASE)
_REMINDER_LIST_RE = re.compile(
    r"\b(?:what are|show|list)\s+(?:my|our)\s+(?:reminders?|tasks?|to-?dos?)\b"
    r"|\b(?:my|our)\s+(?:reminders?|tasks?|to-?dos?)\s+(?:for\s+)?(?:today|tonight)?\??$",
    re.IGNORECASE,
)

# launch_app is capped to a short arg and excludes common verb-continuer
# phrasings ("start talking about X", "let's start with...") — "open" and
# "launch" are fairly unambiguous, but "start" is also ordinary
# conversational English, so it's the one most likely to false-positive
# without this guard. Same "heuristic, will have gaps, refine on real
# usage" caveat as every other regex in this file.
_LAUNCH_APP_STOPWORDS = {
    "talking", "telling", "explaining", "going", "thinking", "writing",
    "doing", "working", "being", "with", "over", "again", "by",
}

# Per-turn context that a registered tool handler needs but that isn't
# available at registration time (registration happens once at startup;
# this data changes every turn). Kept as a small explicit dict rather than
# smuggling extra state through the Tool/handler signature itself, so
# every OTHER handler stays a plain, context-free function — only the one
# tool that genuinely needs conversational context (generate_pdf, which
# needs "the last thing Ember said") reads from this, via closure.
_dispatch_context: dict = {}

# Real bug found via a live transcript: "what's on my calendar today,
# ember?" captured "today, ember" as the day phrase — the regex has no
# way to know "ember" isn't part of the day the person meant — which
# then failed to parse as any recognized time phrase at all. This is a
# systemic risk, not calendar-specific: EVERY handler below that
# captures free text via match.group(N) is equally vulnerable to a
# trailing direct address ("...at 5pm, ember", "...C:\file.txt ember")
# polluting the captured argument. Deliberately strips ONLY from the
# END, and only the literal word "ember" plus adjoining punctuation —
# never a LEADING address ("Ember, what's...") since several existing
# intent patterns (STATUS, INTERRUPT in ember_intent.py) deliberately
# anchor on that as a structural signal; stripping it here would only
# ever run on an ALREADY-matched tool's captured argument, well past
# where that anchoring already happened, so there's no overlap risk —
# this only ever cleans up payload text, never intent classification.
_TRAILING_ADDRESS_RE = re.compile(r"[,\s]*\bember\b[.,!?]*\s*$", re.IGNORECASE)


def _strip_trailing_address(text: "str | None") -> str:
    return _TRAILING_ADDRESS_RE.sub("", text or "").strip()


def _register_builtin_tools() -> None:
    """Registers every capability Ember currently has into the shared
    tool registry. Called once at import time (see the bottom of this
    section) — ember_core.py's main loop never enumerates this list
    itself, it only ever calls registry.match()/dispatch()."""
    registry = _tool_registry

    def _clear_memory(match):
        count = memory.clear_all()
        return f"Cleared {count} stored memories, sir."

    registry.register("clear_memory", "Wipes all stored memories", _CLEAR_MEMORY_RE.pattern, _clear_memory, destructive=True)

    def _forget_specific(match):
        query = _strip_trailing_address(match.group(1).strip().strip(".!?"))
        if not query:
            return "Forget what specifically, sir?"
        deleted = memory.forget(query)
        if not deleted:
            return f"Nothing matching '{query}' was in memory, sir."
        lines = "\n".join(f"  - {d['text']}" for d in deleted)
        return f"Forgot {len(deleted)} matching memory(ies), sir:\n{lines}"

    registry.register("forget_memory", "Deletes memories matching a text query", _FORGET_SPECIFIC_RE.pattern, _forget_specific, destructive=True)

    def _cancel_reminder(match):
        reminder_id = match.group(1)
        ok = _reminder_store.cancel(reminder_id)
        return f"Cancelled reminder {reminder_id}, sir." if ok else f"No active reminder with ID {reminder_id}, sir."

    registry.register("cancel_reminder", "Cancels a reminder by ID", _REMINDER_CANCEL_RE.pattern, _cancel_reminder)

    def _create_reminder(match):
        arg = _strip_trailing_address(match.group(1).strip())
        recurrence = parse_recurrence(arg)
        # Strip the recurrence phrase out before hunting for the one-shot
        # time phrase, so "every sunday" doesn't itself get mistaken for
        # (or interfere with matching) the fire-time phrase — they're
        # answering two different questions ("how often" vs "starting
        # when/at what time of day").
        arg_without_recurrence = RECURRENCE_PHRASE_RE.sub("", arg).strip()
        time_match = TIME_PHRASE_RE.search(arg_without_recurrence)
        if not time_match:
            if recurrence:
                # Recurring with no explicit time-of-day given ("remind me
                # every sunday to update the model") — default to a fixed,
                # sensible time-of-day (9am) rather than refusing outright;
                # the user can always say "every sunday at 6pm" for
                # precision.
                fire_at = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
                if fire_at <= datetime.now():
                    fire_at += timedelta(days=1)
                reminder_text = arg_without_recurrence.strip(" ,.to") or "this"
            else:
                return (
                    "I didn't catch when to remind you, sir — try 'remind me to <thing> "
                    "in 2 hours', '...tonight', '...tomorrow morning', or '...every sunday'."
                )
        else:
            time_phrase = time_match.group(0)
            reminder_text = (arg_without_recurrence[: time_match.start()] + arg_without_recurrence[time_match.end():]).strip(" ,.to")
            if not reminder_text:
                reminder_text = "this"
            fire_at, error = parse_natural_time(time_phrase)
            if fire_at is None:
                return error

        reminder = _reminder_store.add(reminder_text, fire_at, recurrence=recurrence)
        when = fire_at.strftime("%I:%M %p on %b %d")
        recur_note = f" (repeats {recurrence})" if recurrence else ""
        return f"Reminder set, sir — I'll remind you to {reminder_text} at {when}{recur_note}. (ID: {reminder.id})"

    registry.register("create_reminder", "Creates a one-shot or recurring reminder", _REMINDER_CREATE_RE.pattern, _create_reminder)

    def _list_reminders(match):
        reminders = _reminder_store.list_active()
        if not reminders:
            return "You've got no active reminders, sir."
        lines = [
            f"  [{r.id}] {r.message} — {r.fire_at.strftime('%I:%M %p on %b %d')}"
            + (f" (repeats {r.recurrence})" if r.recurrence else "")
            for r in reminders
        ]
        return "Active reminders, sir:\n" + "\n".join(lines)

    registry.register("list_reminders", "Lists active reminders", _REMINDER_LIST_RE.pattern, _list_reminders)

    # ---- Opening files/folders by name ---------------------------------
    # Registered BEFORE launch_app: "open that file" used to fall into launch_app and
    # print "Launched that file." without opening anything. "open notepad" (no file-ish
    # signal) still reaches launch_app untouched.
    def _open_item(match):
        res = file_ops.open_item(_strip_trailing_address(match.group("tail").strip()),
                                 [e["path"] for e in file_registry.list_all() if e.get("path")],
                                 _dispatch_context.get("last_file"))
        if res.ok and res.dst:
            _dispatch_context["last_file"] = res.dst
        return res.message

    def _reveal_item(match):
        res = file_ops.open_item(_strip_trailing_address(match.group("tail").strip()),
                                 [e["path"] for e in file_registry.list_all() if e.get("path")],
                                 _dispatch_context.get("last_file"), reveal=True)
        if res.ok and res.dst:
            _dispatch_context["last_file"] = res.dst
        return res.message

    registry.register(
        "open_file", "Opens a file or folder by name in its default app", _FILE_OPEN_RE.pattern, _open_item,
        guard=lambda m: not file_ops.is_app_name(m.group("tail")) and (
            file_ops.looks_like_file_request(m.group("tail"))
            or (bool(_dispatch_context.get("last_file")) and file_ops.is_pronoun(m.group("tail")))),
    )
    registry.register("reveal_file", "Shows a file or folder in File Explorer", _FILE_REVEAL_RE.pattern, _reveal_item)

    def _launch_app(match):
        arg = _strip_trailing_address(match.group(1).strip().strip(".!?"))
        words = arg.lower().split()
        if not (0 < len(words) <= 3 and words[0] not in _LAUNCH_APP_STOPWORDS):
            return None  # signals "not actually a match" — handled by the caller re-checking below
        return app_launcher.launch_app(arg)

    def _launch_app_handler(match):
        result = _launch_app(match)
        if result is None:
            return "That didn't look like an app name I should try to launch, sir."
        return result

    registry.register("launch_app", "Opens/launches a desktop application", _LAUNCH_APP_RE.pattern, _launch_app_handler)

    def _generate_pdf(match):
        last_reply = _dispatch_context.get("last_assistant_reply")
        if not last_reply:
            return "There's nothing in our recent conversation to export yet, sir."
        output_dir = os.path.join(_PROJECT_ROOT, "data", "exports")
        os.makedirs(output_dir, exist_ok=True)
        # New: "export ... under filename X" lets the caller name the file
        # instead of always getting an opaque timestamp — checked against
        # the ORIGINAL full message (match.string), since _PDF_EXPORT_RE
        # itself has no filename-capturing group of its own. Sanitized by
        # replacing anything that isn't a word character or hyphen with
        # "_", which also doubles as path-traversal protection — no "/"
        # or ".." can survive that substitution, so this can't be used to
        # write outside output_dir regardless of what's in the request.
        filename_match = _PDF_FILENAME_RE.search(match.string)
        if filename_match:
            safe_name = re.sub(r"[^\w\-]", "_", filename_match.group(1).strip()) or "ember_export"
            output_path = os.path.join(output_dir, f"{safe_name}.pdf")
        else:
            output_path = os.path.join(output_dir, f"ember_export_{int(datetime.now().timestamp())}.pdf")
        try:
            pdf_export.generate_pdf(last_reply, output_path, theme="claude", is_path=False)
            file_registry.record("generated", "pdf_export", output_path)
            return f"Exported to {output_path}, sir."
        except Exception as e:
            return f"PDF export failed, sir: {e}"

    registry.register("generate_pdf", "Exports the last reply to a themed PDF", _PDF_EXPORT_RE.pattern, _generate_pdf)

    def _gemini_vision_call(image_bytes: bytes, mime_type: str, query: str) -> str:
        response = llm_client._gemini_client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=[{"parts": [
                {"text": query},
                {"inline_data": {"mime_type": mime_type, "data": image_bytes}},
            ]}],
        )
        return response.text or ""

    def _analyze_screen(match):
        # Multimodal perception (item #8) — the missing half:
        # analyze_document below needs an existing file path; this
        # captures the live screen first, then runs it through the same
        # vision call. See vision_ocr.capture_screenshot/analyze_screenshot.
        #
        # Registered BEFORE analyze_document deliberately: registry.match()
        # returns the first pattern that matches, in registration order,
        # and _ANALYZE_IMAGE_RE below also matches "screenshot" as a noun
        # (with an optional, often-empty path group) — found via real
        # testing that "analyze this screenshot" was being swallowed by
        # analyze_document first and returning "I need a file path, sir"
        # instead of ever reaching this handler. Registration order here
        # is the actual priority mechanism, not just declaration order.
        if not llm_client.cloud_available():
            return "Vision analysis needs the cloud tier, sir, and it's currently unavailable."
        # Inlined rather than calling vision_ocr.analyze_screenshot()
        # directly — that convenience function does exactly this same
        # capture-then-analyze sequence internally but only returns the
        # analysis text, not the screenshot's path, and the Files panel
        # needs that path to record the entry. Same default query text
        # analyze_screenshot() itself uses, kept in sync here rather than
        # duplicated silently drifting.
        screenshot_path = vision_ocr.capture_screenshot()
        if screenshot_path is None:
            return "Couldn't capture the screen, sir — see the console for why."
        result = vision_ocr.analyze_document(
            screenshot_path, vision_call_fn=_gemini_vision_call,
            query="Describe what's on screen and point out anything that looks wrong or worth attention.",
        )
        file_registry.record("upload", "analyzed", screenshot_path, label="Screenshot")
        return result

    registry.register("analyze_screen", "Captures and analyzes the current screen", _ANALYZE_SCREEN_RE.pattern, _analyze_screen)

    def _analyze_document(match):
        path = _strip_trailing_address((match.group(1) or "").strip().strip(".!?"))
        if not path:
            return "I need a file path to analyze, sir — try 'analyze image at <path>'."
        if not llm_client.cloud_available():
            return "Vision analysis needs the cloud tier, sir, and it's currently unavailable."
        if os.path.exists(path):
            # Recorded before the vision call, not after — a slow/failed
            # cloud response shouldn't hide the fact that a real,
            # existing file was genuinely handed to Ember for analysis.
            # A path that doesn't exist at all is NOT recorded (nothing
            # was actually analyzed) — vision_ocr.analyze_document's own
            # "File not found" reply already covers that case honestly.
            file_registry.record("upload", "analyzed", path)
        return vision_ocr.analyze_document(path, vision_call_fn=_gemini_vision_call)

    registry.register("analyze_document", "Analyzes/OCRs an image or document via vision", _ANALYZE_IMAGE_RE.pattern, _analyze_document)

    # ---- Computer interaction (item #6) ------------------------------
    # Read-only by default (list/read aren't marked destructive — see
    # ember_tools/computer.py's docstring for why); run_script is always
    # confirmed regardless of what the allowlist says.
    def _allow_path(match):
        return computer_tools.add_allowed_path(_strip_trailing_address(match.group(1).strip().strip(".!?")))

    registry.register("allow_path", "Adds a directory to Ember's file-access allowlist", _ALLOW_PATH_RE.pattern, _allow_path, destructive=True)

    def _list_directory(match):
        path = _strip_trailing_address((match.group(1) or match.group(2) or "").strip().strip(".!?"))
        return computer_tools.list_directory(path)

    registry.register("list_directory", "Lists files in an allowlisted directory", _LIST_DIR_RE.pattern, _list_directory)

    def _read_file(match):
        return computer_tools.read_file(_strip_trailing_address(match.group(1).strip().strip(".!?")))

    registry.register("read_file", "Reads a file from an allowlisted directory", _READ_FILE_RE.pattern, _read_file)

    def _run_script(match):
        return computer_tools.run_script(_strip_trailing_address(match.group(1).strip().strip(".!?")))

    registry.register("run_script", "Executes an allowlisted script — always confirmed", _RUN_SCRIPT_RE.pattern, _run_script, destructive=True)

    # ---- File operations by name ---------------------------------------
    def _known_file_paths() -> "list[str]":
        return [e["path"] for e in file_registry.list_all() if e.get("path")]

    def _finish_file_op(res) -> str:
        """Keeps the Files panel and the "that file" context in step with what
        actually happened on disk."""
        if res.ok and res.op == "open" and res.dst:
            _dispatch_context["last_file"] = res.dst
        elif res.ok and res.src and res.dst:
            if res.op == "delete":
                file_registry.remove_path(res.src)
                if res.is_dir:
                    file_registry.remove_prefix(res.src)
                _dispatch_context["last_file"] = None
            else:  # move / rename / undo
                file_registry.update_path(res.src, res.dst)
                if res.is_dir:
                    file_registry.update_prefix(res.src, res.dst)   # files recorded inside the folder follow it
                _dispatch_context["last_file"] = res.dst
        return res.message

    def _tail(match) -> str:
        return _strip_trailing_address(match.group("tail").strip())

    def _move_file(match):
        return _finish_file_op(file_ops.move_file(_tail(match), _known_file_paths(), _dispatch_context.get("last_file")))

    def _rename_file(match):
        return _finish_file_op(file_ops.rename_file(_tail(match), _known_file_paths(), _dispatch_context.get("last_file")))

    def _delete_file(match):
        def _confirm(path: str, detail: str = "") -> bool:
            # Asked AFTER the name is resolved, so the person approves the exact file or
            # folder (not just their own words); for a folder, `detail` says how much is in
            # it. Fails closed if there's no gate to ask.
            gate = _dispatch_context.get("confirm_gate") or _confirm_gate
            args = {"file": path, "action": "move to Ember's trash (recoverable)"}
            if detail:
                args["contents"] = detail
            return gate.request_confirmation("delete_file", args, _dispatch_context.get("conversation"))
        return _finish_file_op(file_ops.trash_file(_tail(match), _confirm, _known_file_paths(), _dispatch_context.get("last_file")))

    def _undo_file_op(match):
        return _finish_file_op(file_ops.undo_last())

    _pronoun_ok = lambda m: bool(_dispatch_context.get("last_file")) and file_ops.pronoun_request(m.group("tail"))  # noqa: E731
    _file_guard = lambda m: file_ops.looks_like_file_request(m.string) or _pronoun_ok(m)  # noqa: E731
    registry.register("move_file", "Moves a file into a folder, found by name", _FILE_MOVE_RE.pattern, _move_file, guard=_file_guard)
    registry.register("rename_file", "Renames a file, found by name", _FILE_RENAME_RE.pattern, _rename_file, guard=_file_guard)
    registry.register("delete_file", "Moves a file to Ember's trash after confirmation", _FILE_DELETE_RE.pattern, _delete_file,
                      guard=lambda m: bool(file_ops._FILE_WORD.search(m.string) or file_ops._FILENAME_TOKEN.search(m.string)
                                           or file_ops._FILE_TYPE_WORD.search(m.string)))
    registry.register("undo_file_op", "Undoes the last file move/rename/delete", _FILE_UNDO_RE.pattern, _undo_file_op)

    # ---- Picked up from Nexus VII (this pass) ------------------------
    def _play_spotify(match):
        query = _strip_trailing_address((match.group(1) or match.group(2) or "").strip().strip(".!?"))
        return spotify_tool.play_music_on_spotify(query)

    registry.register("play_spotify", "Searches and plays a track on Spotify", _SPOTIFY_PLAY_RE.pattern, _play_spotify)

    def _check_calendar(match):
        phrase = next((g for g in match.groups() if g), "").strip().strip(".!?")
        phrase = _strip_trailing_address(phrase)
        message, events = calendar_tool.check_schedule_natural(phrase or "today")
        # Remember the event if exactly one was shown, so a pronoun
        # follow-up ("cancel that") can act on it without repeating its
        # name — real, reproduced gap: without this, "please cancel that"
        # right after a calendar check matched nothing and the model
        # fabricated an entire fake cancel exchange instead. Ambiguous
        # (0 or 2+ events) intentionally leaves this cleared — guessing
        # which one "that" means would be worse than asking again.
        _dispatch_context["last_calendar_event"] = (
            {"event_id": events[0]["id"], "summary": events[0]["summary"]} if len(events) == 1 else None
        )
        return message

    registry.register("check_calendar", "Checks Google Calendar for a given day", _CALENDAR_CHECK_RE.pattern, _check_calendar)

    def _add_calendar_event(match):
        groups = match.groups()
        # Two alternative phrasings share this handler ("schedule X for Y"
        # / "add X to my calendar for Y") — whichever one matched leaves
        # its two capture groups populated and the other pair as None.
        summary, when = (groups[0], groups[1]) if groups[0] else (groups[2], groups[3])
        summary = _strip_trailing_address(summary.strip().strip(".!?"))
        when = _strip_trailing_address(when.strip().strip(".!?"))
        message, event_id = calendar_tool.add_event(summary, when)
        # Remember what we just created so a short follow-up correction
        # ("4pm, ember.") can actually change the real event instead of
        # falling through to chat and getting a fabricated "corrected"
        # reply — see _correct_calendar_event below.
        _dispatch_context["last_calendar_event"] = {"event_id": event_id, "summary": summary} if event_id else None
        return message

    registry.register("add_calendar_event", "Adds an event to Google Calendar", _CALENDAR_ADD_RE.pattern, _add_calendar_event)

    def _correct_calendar_event(match):
        pending = _dispatch_context.get("last_calendar_event")
        if not pending:
            # No event created this session to correct — be honest rather
            # than silently no-op or guess what "4pm, ember." was about.
            return "I don't have a calendar event I just created to correct, sir — did you mean something else?"
        new_time = match.group(1)
        result = calendar_tool.update_event_time(pending["event_id"], pending["summary"], new_time)
        return result

    registry.register(
        "correct_calendar_event",
        "Corrects the start time of the calendar event just created",
        _CALENDAR_CORRECT_RE.pattern,
        _correct_calendar_event,
    )

    def _cancel_calendar_event_pronoun(match):
        pending = _dispatch_context.get("last_calendar_event")
        if not pending:
            return "I don't have a specific event in mind, sir — which one did you mean?"
        return calendar_tool.cancel_event_by_id(pending["event_id"], pending["summary"])

    registry.register(
        "cancel_calendar_event_pronoun",
        "Cancels the calendar event just shown/created, referenced by pronoun",
        _CALENDAR_CANCEL_PRONOUN_RE.pattern,
        _cancel_calendar_event_pronoun,
        destructive=True,
    )

    def _reschedule_calendar_event_pronoun(match):
        pending = _dispatch_context.get("last_calendar_event")
        if not pending:
            return "I don't have a specific event in mind, sir — which one did you mean?"
        new_time = _strip_trailing_address(match.group(1).strip().strip(".!?"))
        return calendar_tool.update_event_time(pending["event_id"], pending["summary"], new_time)

    registry.register(
        "reschedule_calendar_event_pronoun",
        "Reschedules the calendar event just shown/created, referenced by pronoun",
        _CALENDAR_RESCHEDULE_PRONOUN_RE.pattern,
        _reschedule_calendar_event_pronoun,
    )

    def _cancel_calendar_event(match):
        query = _strip_trailing_address(match.group(1).strip().strip(".!?"))
        return calendar_tool.cancel_event(query)

    registry.register(
        "cancel_calendar_event",
        "Cancels an upcoming calendar event by keyword",
        _CALENDAR_CANCEL_RE.pattern,
        _cancel_calendar_event,
        destructive=True,  # deleting a real Google Calendar event is irreversible through Ember
    )

    def _reschedule_calendar_event(match):
        query = match.group(1).strip()
        new_time = _strip_trailing_address(match.group(2).strip().strip(".!?"))
        return calendar_tool.reschedule_event_by_keyword(query, new_time)

    registry.register(
        "reschedule_calendar_event",
        "Reschedules an upcoming calendar event by keyword to a new time",
        _CALENDAR_RESCHEDULE_RE.pattern,
        _reschedule_calendar_event,
    )

    def _download_drive(match):
        result, path = drive_tool.download_drive_link(match.group(1).strip())
        if path:
            file_registry.record("generated", "drive_download", path)
        return result
        return result

    registry.register("download_drive", "Downloads a file/folder from a Google Drive link", _DRIVE_DOWNLOAD_RE.pattern, _download_drive)


# Registered once at import time — see the module docstring above and
# ember_tools_registry.py for why ember_core.py itself never enumerates
# this list directly.
_register_builtin_tools()


def _register_builtin_queries() -> None:
    """Registers every structured (panel-facing) query — see
    ember_query_registry.py's module docstring for why this is a
    separate layer from the tool registry above rather than more
    branches on it. Each handler reuses the exact same underlying calls
    _build_status_report()/_build_diagnose_report() already use for the
    chat-facing STATUS/DIAGNOSE commands — this is a second INTERFACE
    onto the same data, not a second implementation of it."""
    registry = _query_registry

    def _systems_status(params: dict) -> dict:
        mem_stats = memory.stats()
        return {
            "cloud_available": llm_client.cloud_available(),
            "fallback_tiers_configured": llm_client.configured_fallback_tiers(),
            "local_embedding_available": llm_client.local_embedding_available(),
            "ollama_available": llm_client.check_ollama_available(),
            "memory": mem_stats,
            "active_reminders": len(_reminder_store.list_active()),
            # No module-level handle to the EmberRuntime instance exists
            # here (it's constructed and owned by run()/run_transport.py,
            # not ember_core.py) -- same as _build_status_report()'s
            # existing text version, which already just states this as a
            # given rather than actually checking a runtime object. Not a
            # new gap, just carried over from the text version this
            # mirrors.
            "background_runtime_running": True,
        }

    registry.register("systems_status", "Cloud/fallback/local availability, memory stats, active reminders", _systems_status)

    def _usage_quota(params: dict) -> dict:
        return {
            "quota": llm_client.quota_status(),
            "fallback_tiers_configured": llm_client.configured_fallback_tiers(),
            "cloud_tiers": [t["model"] for t in llm_client.CLOUD_TIERS],
            "all_fallback_tiers": [t["name"] for t in llm_client.FALLBACK_TIERS],
        }

    registry.register("usage_quota", "Per-tier remaining request quota", _usage_quota)

    def _memory_list(params: dict) -> list:
        n = params.get("n", 50)
        project = params.get("project")
        memory_type = params.get("memory_type")
        return memory.list_recent(n=n, project=project, memory_type=memory_type)

    registry.register("memory_list", "Lists recent memories, newest first, no query needed", _memory_list)

    def _memory_forget(params: dict) -> dict:
        query = (params.get("query") or "").strip()
        if not query:
            raise ValueError("memory_forget requires a non-empty 'query' param.")
        deleted = memory.forget(query)
        return {"deleted": deleted, "count": len(deleted)}

    registry.register(
        "memory_forget", "Deletes memories matching a text query", _memory_forget,
        destructive=True, confirm_label="Delete matching memories",
    )

    def _memory_remember(params: dict) -> dict:
        text = (params.get("text") or "").strip()
        if not text:
            raise ValueError("memory_remember requires non-empty 'text'.")
        # Same normalization extract_memory_candidate() applies to a
        # chat-typed fact -- a manually-entered "went for a run today" in
        # the Memory panel is exactly as vulnerable to the stale-"today"
        # bug as one typed in the chat box. One normalization function,
        # both entry points.
        normalized = _normalize_relative_dates(text)
        memory_type = params.get("memory_type", "semantic")
        project = params.get("project")
        return memory.remember_or_update(normalized, llm_client.embed, memory_type=memory_type, project=project)

    registry.register("memory_remember", "Manually adds/updates a memory (Memory panel's 'remember this' field)", _memory_remember)

    def _files_list(params: dict) -> list:
        return file_registry.list_all(kind=params.get("kind"))

    registry.register("files_list", "Lists recorded uploads/analyses and generated files", _files_list)

    def _file_open(params: dict) -> dict:
        """Files panel click -> open (or reveal) a RECORDED file. Takes an entry id, never a
        path, so a client can't ask Ember to open arbitrary locations."""
        entry = file_registry.get(str(params.get("id", "")))
        if not entry:
            return {"opened": False, "message": "That entry isn't in the list any more, sir."}
        if not entry.get("path"):
            return {"opened": False, "message": "No file location was recorded for this entry, sir."}
        res = file_ops.open_path(entry["path"], reveal=bool(params.get("reveal")))
        if res.ok:
            _dispatch_context["last_file"] = entry["path"]
        return {"opened": res.ok, "message": res.message}

    registry.register("file_open", "Opens (or reveals in Explorer) a file from the Files panel", _file_open)

    def _history_list_sessions(params: dict) -> list:
        return conversation_history.list_sessions(limit=params.get("limit", 50))

    registry.register("history_list_sessions", "Lists past conversations, newest-active-first, with a preview", _history_list_sessions)

    def _history_get_session(params: dict) -> list:
        session_id = (params.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("history_get_session requires a non-empty 'session_id' param.")
        return conversation_history.get_session(session_id)

    registry.register("history_get_session", "Fetches the full transcript of one past conversation", _history_get_session)


_register_builtin_queries()


def _classify_action(message: str) -> "tuple | None":
    """Returns (tool, match) if `message` matches something the registry
    knows how to execute, else None. Thin wrapper kept for classify_intent
    below to stay readable — the actual logic now all lives in
    ember_tools_registry.py."""
    return _tool_registry.match(message)


def _dispatch_action(action_payload: "tuple", last_assistant_reply: "str | None", conversation: "EmberConversation | None" = None) -> str:
    """Executes a matched (tool, match) pair via the registry. Never
    raises — registry.dispatch() already catches unexpected handler
    exceptions; this wrapper's other job is resolving which confirmation
    gate a destructive tool should use: the conversation's own gate if it
    has one (set when a transport creates it), else the CLI's default
    singleton — see EmberConversation.confirm_gate and
    ember_tools_registry.py's dispatch() docstring."""
    tool, match = action_payload
    _dispatch_context["last_assistant_reply"] = last_assistant_reply
    gate = (conversation.confirm_gate if conversation and conversation.confirm_gate else None) or _confirm_gate
    _dispatch_context["confirm_gate"] = gate
    _dispatch_context["conversation"] = conversation
    return _tool_registry.dispatch(tool, match, confirm_gate=gate, conversation=conversation)


def _fuzzy_cancel_reminder(target: str) -> "str | None":
    """Backs STOP_TASK: tries to match `target` against active reminders'
    text. Returns a reply string if exactly one reminder matched and was
    cancelled, a clarification if more than one plausibly matched, or
    None if nothing matched at all — the None case is deliberate (see
    ember_intent.py's docstring on _check_interrupt): a stop-word aimed at
    something Ember isn't actually tracking should fall through to
    ordinary chat, not produce an unprompted "I couldn't find that.\""""
    target_lower = target.lower()
    candidates = [r for r in _reminder_store.list_active() if target_lower in r.message.lower()]
    if not candidates:
        return None
    if len(candidates) > 1:
        options = "; ".join(f"[{r.id}] {r.message}" for r in candidates)
        return f"A few reminders match '{target}', sir — which one? {options}"
    reminder = candidates[0]
    _reminder_store.cancel(reminder.id)
    return f"Stopped '{reminder.message}', sir."


def _dispatch_command(result: "ember_intent.IntentResult", conversation: "EmberConversation | None" = None) -> "str | None":
    """Executes an ember_intent.IntentResult. Returning None is a
    deliberate signal, not a failure — it tells run() to treat this turn
    as ordinary chat instead (see _fuzzy_cancel_reminder and the
    STOP_TASK branch below for the one case that actually uses this)."""
    intent = result.intent

    if intent == "INTERRUPT":
        # Honest, not faked: there's no streaming/async generation loop
        # yet for "stop" to actually cancel mid-flight (see this session's
        # note on what voice/interruption still needs). Still worth
        # acknowledging directly-addressed "stop" every time rather than
        # silently treating it as a normal chat message.
        return "Nothing's actively running for me to stop right now, sir."

    if intent == "STOP_TASK":
        return _fuzzy_cancel_reminder(result.target)

    if intent == "UPDATE_MEMORY":
        # Same normalization extract_memory_candidate() applies -- an
        # explicit "update my X to tonight's plan" is just as vulnerable
        # to the stale-"today" bug as an implicit self-fact statement.
        full_statement = _normalize_relative_dates(result.parameters.get("full_statement", result.target))
        outcome = memory.remember_or_update(full_statement, llm_client.embed, memory_type="semantic")
        action = outcome.get("action")
        if action == "updated":
            return f"Updated, sir — '{outcome['old_text']}' is now '{outcome['new_text']}'."
        if action == "created":
            return f"I didn't find an existing memory to update, sir, so I've saved this as new: '{full_statement}'."
        if action == "duplicate":
            return "That's already exactly what I have stored, sir — no change needed."
        return f"Couldn't update that, sir: {outcome.get('reason', 'unknown reason')}."

    if intent == "INSPECT_MEMORY":
        # result.target can be None here — ember_intent.py's
        # _check_memory_search returns an ambiguous result (no target)
        # for a bare "search/scan/check memory" with no "for X" clause.
        # memory.recall(None, ...) would break; asking rather than
        # guessing is the correct move anyway, same "ask, don't guess"
        # contract the rest of ember_intent.py already follows.
        if not result.target:
            return result.clarification or "Search memory for what, sir?"
        recall = memory.recall(result.target, llm_client.embed, n=5)
        if not recall.text:
            return f"I don't have anything stored about '{result.target}', sir."
        caveat = " (keyword match, not semantic — embedding wasn't available)" if recall.degraded else ""
        return f"Here's what I have on '{result.target}', sir{caveat}:\n{recall.text}"

    if intent == "GET_TIME":
        # Local-utility intent (see ember_intent.py) — answered straight
        # from the system clock, never the LLM or a search backend.
        # %I is zero-padded on both platforms; lstrip('0') is used
        # instead of the %-I/%#I strftime directive specifically because
        # that directive's flag character differs between Linux ('-')
        # and Windows ('#') — this project's actual deployment target —
        # so lstrip is the one approach that's portable across both
        # without needing an OS check here.
        now = datetime.now()
        return f"It's {now.strftime('%I:%M %p').lstrip('0')}, sir."

    if intent == "GET_DATE":
        now = datetime.now()
        return f"Today is {now.strftime('%A, %B %d, %Y')}, sir."

    if intent == "GET_DAY":
        now = datetime.now()
        return f"It's {now.strftime('%A')}, sir."

    if intent == "GET_DATETIME":
        now = datetime.now()
        return f"It's {now.strftime('%I:%M %p').lstrip('0')} on {now.strftime('%A, %B %d, %Y')}, sir."

    if intent == "STATUS":
        if result.target:
            return f"I don't have any connected devices to check yet, sir — device sync isn't built."
        return _build_status_report()

    if intent == "DIAGNOSE":
        return _build_diagnose_report(result.target)

    if intent == "CLEAR_CONTEXT":
        (conversation or _default_conversation).clear()
        return "Conversation context cleared, sir — long-term memory is untouched."

    if intent == "NEW_CONVERSATION":
        (conversation or _default_conversation).clear()
        return "Starting fresh, sir — I'll still remember what's in long-term memory."

    if intent == "FIND":
        recall = memory.recall(result.target, llm_client.embed, n=5)
        if not recall.text:
            return f"Nothing turned up for '{result.target}' in memory, sir. I don't have a general file search yet beyond that."
        return f"Found this in memory for '{result.target}', sir:\n{recall.text}"

    if intent in ("SEND", "CONTINUE"):
        return (
            f"I don't have device sync built yet, sir, so I can't {'send that' if intent == 'SEND' else 'continue this'} "
            f"to {result.target or 'another device'} — that's on the roadmap but not implemented."
        )

    return None  # unrecognized intent name — treat as chat rather than error


def _build_status_report() -> str:
    lines = ["Status, sir:"]
    lines.append(f"  Cloud (Gemini): {'available' if llm_client.cloud_available() else 'unavailable'}")
    fallbacks = llm_client.configured_fallback_tiers()
    lines.append(f"  Fallback providers configured: {', '.join(fallbacks) if fallbacks else 'none'}")
    lines.append(f"  Local embedding: {'available' if llm_client.local_embedding_available() else 'not loaded'}")
    mem_stats = memory.stats()
    lines.append(f"  Memory: {mem_stats['total']} stored ({mem_stats['embedded']} embedded)")
    lines.append(f"  Active reminders: {len(_reminder_store.list_active())}")
    lines.append("  Background runtime: running (proactive engine + reminder scheduler)")
    return "\n".join(lines)


def _build_diagnose_report(target: "str | None") -> str:
    if target and "cloud" in target.lower():
        if llm_client.cloud_available():
            quota = llm_client.quota_status()
            return f"Cloud AI is up, sir. Remaining quota today: {quota}."
        return "Cloud AI is unavailable, sir — no Gemini API key is configured, or the client failed to initialize at startup."
    if target:
        return f"I don't have a way to diagnose '{target}' yet, sir — that's outside what I currently monitor."
    # "diagnose yourself" — general self-check
    report = _build_status_report()
    quota = llm_client.quota_status()
    return f"{report}\n  Quota remaining: {quota}"


def classify_intent(message: str, history: list) -> tuple:
    """Return (intent, topic, action_payload).
    intent is 'chat', 'search', 'verify', 'action', or 'command'.
    topic is only populated for 'verify'.
    action_payload is only populated for 'action' (see _classify_action)
    or 'command' (an ember_intent.IntentResult, see _dispatch_command)."""
    action_payload = _classify_action(message)
    if action_payload:
        return "action", None, action_payload

    # New natural-language intent layer (ember_intent.py) — checked after
    # the existing tool registry (reminders/calendar/spotify/drive/memory-
    # clear already have precise regexes and shouldn't be re-decided here)
    # but before the older, broader heuristics below, since an intent like
    # INTERRUPT or STATUS should win over e.g. _looks_like_action_request's
    # much fuzzier "sounds like an action" guess.
    command = ember_intent.classify(message)
    if command:
        return "command", None, command

    return _classify_fallback(message, history)


def _classify_fallback(message: str, history: list) -> tuple:
    """Everything classify_intent() falls through to once neither the
    tool registry (_classify_action) nor the command layer
    (ember_intent.classify) claimed this message.

    Extracted into its own function (this pass) because it needs to be
    reachable from a SECOND place too: process_turn()'s "command"
    branch, when a matched command's dispatch declines to act (returns
    None — e.g. STOP_TASK targeting a reminder that doesn't actually
    exist). That path used to hardcode intent = "chat" directly, which
    was a real bug traced from an actual failure: "cancel the WEC race
    this weekend" matches STOP_TASK, correctly finds no matching
    reminder, and used to fall straight into plain, ungrounded chat from
    there — permanently losing its chance to be classified as "search"
    even though "this weekend" is a real search trigger a message that
    never matched a command in the first place would have gotten. Now a
    declined command gives its message the exact same fallback chain any
    other message gets, rather than a special-cased worse one."""
    if _looks_like_action_request(message):
        return "chat", None, None

    if _is_verification_request(message):
        return "verify", _find_last_topic(history), None

    if _is_explicit_search_request(message):
        return "search", None, None

    if needs_recall(message):
        # Real, reproduced bug: this used to be checked AFTER
        # needs_search() below, so a purely personal message containing
        # any generic search trigger word ("today", "recently") — e.g.
        # "remember that I went for a run today" or "give me a report on
        # my recent activity" — got shipped off to a real web search
        # before this recall check ever ran, wasting a Tavily call and
        # showing a "grounded" tag on what was really just a local
        # memory acknowledgment. Moved ahead of needs_search() so a
        # personal/self-referential message ("my", "remember", "earlier")
        # goes to recall first, even if it also loosely matches a search
        # trigger. Accepted trade-off: a message that's BOTH personal AND
        # genuinely needs current info (e.g. "what's the weather where I
        # live") will now need an explicit "search"/"look up" verb to get
        # grounded, rather than triggering automatically — a safer
        # default for a personal assistant than silently burning search
        # quota on personal-context requests.
        return "chat", None, None

    if needs_search(message):
        return "search", None, None

    if _is_static_lookup(message):
        # Checked before BOTH remaining search paths below, not just
        # _mentions_named_person -- a static/definitional question
        # ("what's the derivative of x^2") also happens to start with
        # "what's", which is one of _FOLLOWUP_RE's own bare-continuation
        # shapes. Right after a search-grounded turn, that combination
        # would otherwise slip through _mentions_named_person's fix above
        # only to get re-caught by the bare-followup branch just below
        # it -- same false positive, different door. One check here
        # closes both.
        return "chat", None, None

    if _mentions_named_person(message):
        return "search", None, None

    if _is_bare_followup(message) and _recent_turn_used_search(history):
        return "search", None, None

    return "chat", None, None


# ---- Memory-worthiness heuristic --------------------------------------
# Same spirit as needs_search: a cheap heuristic, not a classifier, that
# will have false negatives and positives. architecture.md §2 step 7
# leaves "what's worth writing to memory" as an open question — this is
# a first, deliberately narrow answer: store explicit "remember ..."
# commands, and messages that read as the user stating a durable fact
# about themselves, rather than every message (which would flood recall
# with noise and burn embedding calls on things like "what's 2+2").
# Refine as real usage surfaces gaps, same as needs_search.

_REMEMBER_COMMAND_RE = re.compile(r"^\s*remember(?:\s+that)?\s+(.+)", re.IGNORECASE)

# Real usage caught this gap directly: "embedded modular brain..., remember
# that" was typed fact-first, not command-first, and silently stored
# nothing because the pattern above only matches "remember that <fact>".
# People phrase this either way in natural speech — this catches the
# trailing form.
_REMEMBER_TRAILING_RE = re.compile(r"^(.+?),?\s*remember(?:\s+that)?\s*[.!]?\s*$", re.IGNORECASE)

_SELF_FACT_PATTERNS = [
    r"\bmy name is\b", r"\bi am\b", r"\bi'?m\b", r"\bi work\b", r"\bi live\b",
    r"\bi like\b", r"\bi prefer\b", r"\bi hate\b", r"\bi dislike\b",
    r"\bi own\b", r"\bmy birthday\b", r"\bi'?ve got\b", r"\bfyi\b",
]
_SELF_FACT_RE = re.compile("|".join(_SELF_FACT_PATTERNS), re.IGNORECASE)

# Real, reproduced bug: "when's my birthday?" matched \bmy birthday\b and
# got stored VERBATIM as if it were a stated fact — a question, not a
# statement, ended up polluting memory. A question almost never IS a
# self-fact regardless of which trigger word it happens to contain, so
# this excludes anything question-shaped before the self-fact check ever
# runs. Deliberately scoped to just this: it does not catch every
# conversational aside that isn't phrased as a question (e.g. "im not
# sure that's the right time ember" doesn't start with a WH-word or end
# in "?"), which remains a known, harder heuristic gap for a later pass.
_QUESTION_START_RE = re.compile(
    r"^\s*(?:when|what|why|how|who|where|which|is|are|do|does|did|can|could|will|would|should)\b",
    re.IGNORECASE,
)


def _looks_like_question(message: str) -> bool:
    stripped = message.strip()
    return stripped.endswith("?") or bool(_QUESTION_START_RE.match(stripped))


# ---- Relative-day normalization (fixes a real reported bug) --------------
# A memory stored verbatim as "...went for a run today" reads identically
# whether it's recalled five minutes later or five weeks later -- "today"
# silently drifts to mean whatever day the RECALL happens to land on,
# which is correct on exactly one day (the day it was written) and wrong
# every day after that. EmberMemory's created_at timestamp already
# records when a fact was stored, but recall() only ever hands back the
# raw stored TEXT (see its own comment on the Markdown-divider fix) --
# nothing re-derives "today" from created_at after the fact, and doing
# that reconstruction at every future recall would be far more fragile
# than just writing the real date down once, now, while "today" still
# unambiguously means something.
_RELATIVE_DAY_TERMS = {
    "this morning": lambda now: f"the morning of {now.strftime('%B %d, %Y')}",
    "this afternoon": lambda now: f"the afternoon of {now.strftime('%B %d, %Y')}",
    "this evening": lambda now: f"the evening of {now.strftime('%B %d, %Y')}",
    "tonight": lambda now: f"the night of {now.strftime('%B %d, %Y')}",
    "last night": lambda now: f"the night of {(now - timedelta(days=1)).strftime('%B %d, %Y')}",
    "yesterday": lambda now: (now - timedelta(days=1)).strftime("%B %d, %Y"),
    "tomorrow": lambda now: (now + timedelta(days=1)).strftime("%B %d, %Y"),
    # Checked last -- it's a substring of every phrase above ("this
    # TODAY-morning" isn't a real risk since none of them contain the bare
    # word "today", but ordering longest-first is still the same safe
    # habit ember_reminders.py's own _TIME_OF_DAY_KEYS_ORDERED already
    # uses for exactly this kind of phrase list).
    "today": lambda now: now.strftime("%B %d, %Y"),
}
_RELATIVE_DAY_TERMS_ORDERED = sorted(_RELATIVE_DAY_TERMS.keys(), key=len, reverse=True)
_RELATIVE_DAY_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _RELATIVE_DAY_TERMS_ORDERED) + r")\b",
    re.IGNORECASE,
)


def _normalize_relative_dates(text: str, now: "datetime | None" = None) -> str:
    """Rewrites relative day words into absolute dates, e.g. 'went for a
    run today' -> 'went for a run on September 15, 2026'. now defaults to
    the real current time; only overridden in tests. Deliberately a plain
    word-substitution, not an LLM call -- same 'small enumerable
    vocabulary -> heuristic, not a model call' reasoning already used
    throughout this file (needs_search, extract_memory_candidate itself,
    etc.). A day-of-week word ('remembered it was Tuesday') is out of
    scope for this pass -- 'today'/'yesterday'/'tonight' are what the
    reported bug actually used, and covering every possible temporal
    phrase a person might type is a much larger, open-ended problem."""
    now = now or datetime.now()

    def _replace(match: "re.Match") -> str:
        return _RELATIVE_DAY_TERMS[match.group(1).lower()](now)

    return _RELATIVE_DAY_RE.sub(_replace, text)


def extract_memory_candidate(message: str) -> "str | None":
    """Return text worth storing to memory, or None if this message doesn't
    look worth remembering. Explicit 'remember X' or 'X, remember that'
    commands always win and store just the fact; otherwise, falls back to
    a self-fact heuristic that stores the message verbatim. Every return
    path is passed through _normalize_relative_dates() before being
    handed to the caller, so nothing reaches EmberMemory with a
    day-relative word still in it -- see that function's docstring for
    the exact bug this closes."""
    leading = _REMEMBER_COMMAND_RE.match(message)
    if leading:
        return _normalize_relative_dates(leading.group(1).strip())

    trailing = _REMEMBER_TRAILING_RE.match(message)
    if trailing:
        candidate = trailing.group(1).strip()
        if candidate and candidate.lower() not in {"remember", "remember that"}:
            return _normalize_relative_dates(candidate)

    if _SELF_FACT_RE.search(message) and not _looks_like_question(message):
        return _normalize_relative_dates(message.strip())

    return None


# ---- Recall-worthiness heuristic ---------------------------------------
# Calling embed() on every single turn (including "good afternoon") burns
# an API call for no benefit — real usage confirmed this immediately (both
# cloud tiers hit 429 within three turns of a live session). This is the
# same unconditional-recall anti-pattern flagged in the JARVIS review;
# gating it here corrects it. Same "heuristic, not classifier" caveat as
# needs_search and extract_memory_candidate.

_RECALL_TRIGGER_PATTERNS = [
    r"\bmy\b", r"\bmine\b", r"\bdo you know\b", r"\bremember\b", r"\brecall\b",
    r"\bwe (talked|discussed)\b", r"\bearlier\b", r"\bbefore\b", r"\blast time\b",
    r"\bpreviously\b", r"\bagain\b", r"\bam i\b", r"\bwhat do i\b", r"\bwhat am i\b",
]
_RECALL_TRIGGER_RE = re.compile("|".join(_RECALL_TRIGGER_PATTERNS), re.IGNORECASE)


def needs_recall(message: str) -> bool:
    """Return True if `message` plausibly references something worth
    looking up in memory, rather than being a generic/stateless message."""
    return bool(_RECALL_TRIGGER_RE.search(message))


# ---- Startup checks -----------------------------------------------------

def startup_check() -> None:
    """Verify at least one backend works before entering the main loop.
    Prints a clear diagnosis instead of letting a bad first call surface
    a raw stack trace to the user."""
    print("Ember starting up...")

    if llm_client.cloud_available():
        status = llm_client.quota_status()
        parts = ", ".join(f"{m} ({r} left)" for m, r in status.items() if r is not None)
        print(f"  Cloud (Gemini): ready — {parts}")
    else:
        print("  Cloud (Gemini): unavailable (GEMINI_API_KEY not set or invalid).")

    configured = llm_client.configured_fallback_tiers()
    all_names = [t["name"] for t in llm_client.FALLBACK_TIERS]
    if configured:
        print(f"  Fallback tiers: {', '.join(configured)} ({len(configured)}/{len(all_names)} configured)")
    else:
        print(f"  Fallback tiers: none configured ({len(all_names)} available — see .env for the *_API_KEY vars)")

    ollama_ok = llm_client.check_ollama_available()
    print(f"  Local (Ollama): {'ready' if ollama_ok else 'unreachable'}")

    if not llm_client.cloud_available() and not configured and not ollama_ok:
        print(
            "\nNo backend is usable at all — Ember can't run. Fix at least one:\n"
            "  - Cloud: set the GEMINI_API_KEY environment variable.\n"
            "  - Fallback: set at least one of CEREBRAS_API_KEY, GROQ_API_KEY, "
            "NVIDIA_API_KEY, MISTRAL_API_KEY.\n"
            "  - Local: start Ollama (`ollama serve`) and confirm "
            f"'{llm_client.OLLAMA_MODEL}' is pulled.\n"
        )
        sys.exit(1)

    print()


# ---- Main loop --------------------------------------------------------
# _SEARCH_CAVEAT (a small "you might want to double-check this" note
# appended to an otherwise-shown ungrounded reply) used to live here.
# Removed, not just unused — the caveat-and-show approach was the actual
# bug reported: search-required requests must never present an ungrounded
# answer at all, caveated or not. See the wants_search branch below, which
# now replaces the reply outright instead of appending a note to it.


# ---- Proactive engine wiring ---------------------------------------------
# Fills the last unimplemented branch of the original architecture diagram
# ("proactive event -> proactive engine"). Adapted (not copied) from
# jarvisforember's core/proactive_engine.py — see EMBER_PORT_AUDIT.md for
# the real bug found and fixed in the original (it silently ignores its own
# NVIDIA-key check and always calls Mistral with BACKUP_4 creds regardless).
# This wrapper hands the engine a plain call_fn that routes through Ember's
# own generate() — the SAME tiered fallback chain every other message goes
# through — rather than hardcoding a provider, per the "LLM adapter stays
# responsible for model communication" rule.
#
# notify_fn just prints for now, with a clearly distinct prefix so a
# proactive message is never confused with a response to something the
# user asked. This is a CLI-appropriate placeholder — the moment there's a
# real frontend, swap this for whatever delivery channel that provides,
# same swap-out contract as ember_confirmation.py's CLI gate.
#
# Silently does nothing useful if data/goals.json doesn't exist yet — the
# engine's own _run_cycle checks for that and just sleeps an hour per
# cycle, which is a deliberate, already-reasoned no-op, not a bug.

def _proactive_call_fn(prompt: str) -> str:
    result = llm_client.generate(prompt, system_prompt="", use_search=False, history=[])
    return result.text


def _proactive_notify_fn(message: str) -> None:
    """Publishes onto the shared bus rather than printing directly — the
    actual console output now happens once, in
    ember_notifications.NotificationManager's console_channel, reached via
    the "proactive.message" event this publishes. Before the event bus
    existed, this function printed directly; that would now double-print
    alongside the notification manager's own delivery, so this is the one
    place that changed shape rather than just gaining a second path."""
    get_bus().publish("proactive.message", {"message": message})


def _build_runtime() -> EmberRuntime:
    """Assembles the background runtime (item #9 of the next-phase brief):
    proactive engine + reminder scheduler + notification manager + the
    Google-auth expiry watchdog, wired through the shared event bus, with
    ONE start()/stop() for ember_core.py's run() to call — it no longer
    needs to know the construction order or wiring details of each
    subsystem."""
    engine = ProactiveEngine(
        call_fn=_proactive_call_fn,
        notify_fn=_proactive_notify_fn,
        goals_path=os.path.join(_PROJECT_ROOT, "data", "goals.json"),
    )
    notification_manager = NotificationManager()
    bus = get_bus()
    # Always constructed, not conditional on Google auth already being
    # configured — the watchdog's own _check_once() already no-ops
    # gracefully (returns immediately) until last_consent_timestamp()
    # has anything to read, so there's nothing to gate here; the same
    # "never crashes, only degrades" contract every other subsystem in
    # this file already follows.
    google_auth_watchdog = GoogleAuthWatchdog(bus=bus)
    return EmberRuntime(
        proactive_engine=engine,
        reminder_store=_reminder_store,
        notification_manager=notification_manager,
        google_auth_watchdog=google_auth_watchdog,
        bus=bus,
    )


def _vision_for_attachments(image_bytes: bytes, mime_type: str, query: str) -> str:
    """Same Gemini vision call the analyze_document tool uses, at module
    level so the attachment path (ember_attachments.py) can reach it."""
    response = llm_client._gemini_client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=[{"parts": [
            {"text": query},
            {"inline_data": {"mime_type": mime_type, "data": image_bytes}},
        ]}],
    )
    return response.text or ""


def _on_attachment_saved(att: dict) -> None:
    file_registry.record("upload", "attached", att["path"], label=att["name"])
    _dispatch_context["last_file"] = att["path"]   # so "move that to documents" works right after an upload


def _augment_with_attachments(prompt: str, conversation, stream_callback) -> str:
    """Folds chat-attached files (or a still-warm block from a recent turn)
    into this turn's final prompt. See ember_attachments.py for the safety
    and bounding rules. Never raises: a failure here degrades to answering
    without the file, plainly noted, never to a crashed turn."""
    try:
        return ember_attachments.augment_prompt(
            prompt, conversation,
            vision_call=_vision_for_attachments if llm_client.cloud_available() else None,
            status_fn=lambda t: _emit_stream_status(stream_callback, t),
            on_saved=_on_attachment_saved,
        )
    except Exception as e:
        print(f"[ember_core] attachment handling failed: {e}")
        return prompt + "\n\n(The user attached file(s), but reading them failed; say so rather than guessing at their contents.)"


def _emit_stream_status(stream_callback, text: str) -> None:
    """Best-effort transient progress indicator for a slow, otherwise
    silent step — a Tavily research() round (up to 2 per turn, several
    seconds each), or falling all the way back to a fully blocking
    generate() call for forced native/tool-based grounding when no
    evidence could be pre-fetched. Closes the actual "silent latency" gap:
    even on turns that eventually stream their real answer fine, the
    research() call that precedes generation was — and, for evidence
    lookups themselves, still is — fully synchronous with zero feedback;
    a client (CLI or transport) previously saw nothing at all for
    however long that round-trip took, then either a stream of real
    tokens or, in the worst case, one giant reply appearing all at once
    several seconds later with no indication anything was happening in
    between.

    Deliberately a SEPARATE call shape from the real answer, not text
    spliced into it — passes kind="status" so a caller can distinguish
    "Ember is still working on this" from actual reply content and
    render/handle them differently (ember_transport.py sends these as a
    distinct {"type": "status"} message; the CLI prints them on their own
    line). Never appended to reply_text, never persisted to history —
    these are transient, not part of the answer.

    Never raises — a broken or disconnected callback (a transport client
    that vanished mid-turn, or any other unexpected failure in a
    caller-supplied function this module doesn't control) must not take
    down the turn itself, same graceful-degradation contract as
    everything else in this file. No-ops entirely when stream_callback is
    None, which remains the default and was the CLI's only behavior
    before this session."""
    if stream_callback is None:
        return
    try:
        stream_callback(text, "status")
    except Exception as e:
        print(f"[ember_core] stream status callback failed (non-fatal): {e}")


def process_turn(user_input: str, conversation: "EmberConversation | None" = None, stream_callback=None) -> "tuple[str, str]":
    """Processes exactly one turn for `conversation` (defaults to the
    CLI's own) and returns (tag, reply_text) — extracted out of run()'s
    while-loop so a future transport handler can call the SAME function
    for a WebSocket client's turn, rather than a second, parallel
    implementation of this logic. run() below now uses this same
    streaming contract too (see this session's change) — it is no longer
    the transport-only path it originally was.

    stream_callback: optional callable(text: str, kind: str = "text").
    kind="text" (the default, and the only kind that ever existed before
    this session) is an incremental delta of the actual reply — invoked
    as output becomes available via llm_client.generate_stream(). kind=
    "status" is a NEW, separate channel (see _emit_stream_status above):
    a transient progress note sent during a slow-but-silent step (a
    Tavily research() round, or an unavoidable fall-back to a fully
    blocking generate() call), never part of the reply itself and never
    persisted to history. Existing callers that only ever handled a
    single positional text argument (e.g. an older transport handler
    written before this change) will still work for kind="text" calls
    made positionally-compatible ways, but should be updated to accept
    the second `kind` argument to actually see status updates rather
    than erroring on an unexpected argument — see ember_transport.py's
    _stream_to_client for the reference implementation.

    Streaming still only takes effect for the final ordinary-chat/
    already-evidenced generation step (not action/command intents, which
    are already fast and return a complete string). When stream_callback
    is None (still a valid, fully-supported mode — nothing requires a
    caller to use streaming), this function behaves exactly as it always
    has: non-streaming, one complete reply, no status callbacks either
    (see _emit_stream_status's no-op-on-None behavior).

    Cancellation: reset at the start of every turn (a flag left set by a
    turn that already finished must never spuriously cancel this new
    one). Checked before starting expensive steps throughout, AND — when
    stream_callback is given — genuinely mid-generation, via
    generate_stream()'s own cancel_check, since that's the one call in
    this whole function with an actual loop to check it inside of. The
    non-streaming generate() path can only honor cancellation BETWEEN
    calls, never during one already in flight (see
    ember_conversation.py's request_cancel() docstring)."""
    conversation = conversation or _default_conversation
    conversation.reset_cancel()
    _t_turn = time.perf_counter()
    research_s = 0.0

    recall_text = ""
    if needs_recall(user_input):
        recall = memory.recall(user_input, llm_client.embed, n=8)
        recall_text = recall.text
    system_prompt = build_system_prompt(memory_context=recall_text)
    if getattr(conversation, "spoken_reply", False):
        system_prompt += "\n\n" + SPOKEN_ADDENDUM

    history_snapshot = conversation.snapshot_history()
    intent, verify_topic, action_payload = classify_intent(user_input, history_snapshot)
    if getattr(conversation, "attachments", None):
        # A message with files attached is a question ABOUT those files. Without
        # this, "analyze this image" would hit _ANALYZE_IMAGE_RE and answer
        # "I need a file path", and typed words could trigger tools/commands.
        intent, verify_topic, action_payload = "chat", None, None
    wants_search = intent == "search"
    verified_via_evidence = False
    search_evidence_used = False

    if intent == "action":
        last_reply = next(
            (t["content"] for t in reversed(history_snapshot) if t["role"] == "assistant"),
            None,
        )
        reply_text = _dispatch_action(action_payload, last_reply, conversation=conversation)
        tool_name = action_payload[0].name
        _append_history("user", user_input, conversation=conversation)
        _append_history("assistant", reply_text, conversation=conversation)
        return f"action:{tool_name}", reply_text

    if intent == "command":
        reply_text = _dispatch_command(action_payload, conversation=conversation)
        if reply_text is not None:
            # Every ember_intent.py command (INTERRUPT, STATUS, the new
            # GET_TIME/GET_DATE/GET_DAY/GET_DATETIME, etc.) is resolved
            # entirely locally, above — this never reaches llm_client at
            # all. Logging it explicitly, in place of the old implicit
            # silence here, is what makes "used the cheapest local
            # capability instead of the LLM/web search" visible in the
            # console during development, per the same visibility
            # standard the search-intent branch below already has.
            print(f"[ember_core] Local intent: {action_payload.intent} -> no API call")
            _append_history("user", user_input, conversation=conversation)
            _append_history("assistant", reply_text, conversation=conversation)
            return action_payload.intent.lower(), reply_text
        # _dispatch_command returned None on purpose (e.g. a STOP_TASK
        # whose target didn't match anything Ember is actually tracking)
        # -- give this message the SAME fallback chain a message that
        # never matched a command at all gets (search/verify/recall/etc.),
        # instead of hardcoding it straight to plain chat. See
        # _classify_fallback's docstring for the exact bug this fixes:
        # that hardcode was silently discarding a message's real chance
        # to be classified as "search" whenever a command matched then
        # declined.
        intent, verify_topic, action_payload = _classify_fallback(user_input, history_snapshot)
        wants_search = intent == "search"

    if conversation.is_cancelled():
        return "cancelled", "Cancelled before I started on that, sir."

    if intent == "verify":
        last_claim = next(
            (t["content"] for t in reversed(history_snapshot) if t["role"] == "assistant"),
            None,
        )
        search_query = verify_topic or user_input
        _emit_stream_status(stream_callback, "Checking sources, sir — one moment...")
        _t_r = time.perf_counter()
        verify_research = ember_research.research(search_query, llm_client.web_search, cancel_check=conversation.is_cancelled)
        research_s = time.perf_counter() - _t_r
        print(f"[ember_core] verification research: {len(verify_research.evidence)} source(s), {verify_research.rounds_used} round(s), {research_s:.1f}s")
        evidence = verify_research.evidence
        if evidence and not verify_research.relevant:
            # Exactly the failure this session traced: 5 results came
            # back, from several domains, "sufficient" by count/diversity
            # alone -- and were entirely about something else (vintage
            # apparel, in the reported case, for a motorsport question).
            # Handing that to the model as "evidence to evaluate" just let
            # it reassert its original, unverified claim while technically
            # being honest that the results didn't address the question --
            # a worse outcome than admitting no usable evidence exists.
            # Treating it as no evidence here routes to the same honest
            # "can't verify" branch below instead.
            print(f"[ember_core] Verification retrieved {len(evidence)} source(s) across {verify_research.rounds_used} round(s) but none appear topically relevant to {search_query!r} -- treating as no usable evidence.")
            evidence = []

        if evidence:
            evidence_block = "\n".join(
                f"- {r['title']}: {r['content'][:300]} ({r['url']})" for r in evidence
            )
            effective_prompt = (
                f"The user is challenging something you said. "
                f"{'The topic was: ' + verify_topic if verify_topic else ''} "
                f"Your previous claim was: {last_claim!r}. "
                f"They just said: {user_input!r}. "
                f"Here is live search evidence, retrieved independently just now:\n{evidence_block}\n\n"
                "Evaluate this evidence against your previous claim. If your "
                "previous claim was wrong or outdated, say so plainly and give "
                "the corrected, current information based on the evidence "
                "above. If the evidence confirms your original claim, say "
                "that plainly too — don't hedge either way."
            )
            verified_via_evidence = True
            print(f"[ember_core] Verification used {len(evidence)} independently retrieved source(s).")
        else:
            # No independent evidence available (no TAVILY_API_KEY,
            # or the call failed) — fall back to forcing Gemini's
            # own grounding, same as before this existed.
            effective_prompt = (
                f"The user is challenging something you said. "
                f"{'The topic was: ' + verify_topic if verify_topic else ''} "
                f"Your previous claim was: {last_claim!r}. "
                f"They just said: {user_input!r}. "
                "You have live search grounding for this response — use it to "
                "actually check the current facts before answering. If your "
                "previous claim was wrong or outdated, say so plainly and give "
                "the corrected, current information. Do not simply reassert "
                "your previous claim without new evidence, and do not just say "
                "you 'reviewed' something without actually having searched."
            )
            wants_search = True
    elif intent == "search":
        # Closes the exact gap flagged after the last session: local
        # Ollama has no tool-calling and no native grounding, so a
        # search-flagged query that fell all the way to local used
        # to get nothing but an honest "you should double-check
        # this" caveat — true, but not actually useful. Applying
        # the same evidence-first pattern "verify" already uses
        # closes it for every backend, not just the ones smart
        # enough to call a tool.
        #
        # Bonus effect, not just a fix: when Tavily evidence comes
        # back, wants_search is set to False below — Gemini's own
        # metered Google Search grounding (20 RPD tier) is skipped
        # entirely rather than double-spending quota on the same
        # freshness the injected evidence already provides, and
        # fallback tiers aren't told to force another tool call for
        # the same reason (tool_choice="auto" still lets a fallback
        # tier call web_search again if it decides the injected
        # evidence wasn't enough — nothing here prevents that,
        # it just removes the forced directive).
        # Multi-step research (brief item #7): a single thin/single-
        # domain result set now triggers one bounded, reformulated
        # follow-up search before giving up — see ember_research.py.
        # Still at most 2 Tavily calls per turn, never an open-ended loop.
        _emit_stream_status(stream_callback, "Searching for that, sir — one moment...")
        _t_r = time.perf_counter()
        research_result = ember_research.research(user_input, llm_client.web_search, cancel_check=conversation.is_cancelled)
        research_s = time.perf_counter() - _t_r
        print(f"[ember_core] research: {len(research_result.evidence)} source(s), {research_result.rounds_used} round(s), {research_s:.1f}s")
        evidence = research_result.evidence
        if evidence and not research_result.relevant:
            # Same off-topic-evidence guard as the verify branch above --
            # don't embed evidence into the prompt as if it addressed the
            # question when the keyword-overlap check says it almost
            # certainly doesn't. Falls through to the "no evidence" else
            # branch below, same as a genuinely empty result set.
            print(f"[ember_core] Search intent retrieved {len(evidence)} source(s) across {research_result.rounds_used} round(s) but none appear topically relevant to the query -- treating as no usable evidence.")
            evidence = []
        if evidence:
            evidence_block = "\n".join(
                f"- {r['title']}: {r['content'][:300]} ({r['url']})" for r in evidence
            )
            effective_prompt = (
                f"{user_input}\n\n"
                f"(Live search evidence, retrieved independently just now — use it "
                f"to answer accurately rather than relying on your own training "
                f"data, which may be outdated:\n{evidence_block})"
            )
            search_evidence_used = True
            wants_search = False  # evidence already embedded — no need to also spend Gemini's metered grounding or force a fallback-tier tool call
            print(f"[ember_core] Search intent used {len(evidence)} independently retrieved source(s) across {research_result.rounds_used} round(s).")
        else:
            # No TAVILY_API_KEY configured, or the call failed —
            # fall back to the pre-existing behavior: Gemini's own
            # native grounding, or a forced tool-call directive on
            # fallback tiers, or (on local) the honest caveat.
            effective_prompt = user_input
            search_evidence_used = False
    else:
        effective_prompt = user_input

    effective_prompt = _augment_with_attachments(effective_prompt, conversation, stream_callback)

    if wants_search and stream_callback is not None:
        # Reached only when no independent evidence was available/usable
        # (Tavily unconfigured, the call failed, or came back off-topic) —
        # this is the one remaining branch that falls all the way through
        # to a fully blocking generate() call below (Gemini's own native
        # grounding, or a forced multi-iteration tool-calling loop on a
        # fallback tier), which can genuinely take several seconds with
        # nothing else shown in the meantime. One more honest heads-up
        # here, distinct from the "Searching..."/"Checking sources..."
        # notes above, since this specifically flags the SLOWER path.
        _emit_stream_status(
            stream_callback,
            "No independent evidence turned up, sir — falling back to a "
            "live-grounded generation directly; this may take a little longer.",
        )

    if conversation.is_cancelled():
        return "cancelled", "Cancelled before generating a reply, sir."

    if stream_callback is not None and not wants_search:
        # Streaming path — only for ordinary chat/already-evidenced turns
        # (wants_search is False here whenever real evidence was already
        # fetched above and embedded into effective_prompt, OR the intent
        # never needed search at all; the remaining case where
        # wants_search is True means no evidence could be fetched and
        # we're forcing Gemini's own native grounding, which is the exact
        # case generate_stream() itself declines to stream — see its
        # module-level note — so falling through to the non-streaming
        # branch below for that case is correct, not a gap).
        assembled = []
        final_chunk = None
        try:
            for chunk in llm_client.generate_stream(
                effective_prompt,
                system_prompt=system_prompt,
                use_search=False,
                history=history_snapshot,
                cancel_check=conversation.is_cancelled,
            ):
                if chunk.text_delta:
                    if not assembled:
                        print(f"[ember_core] first token {time.perf_counter() - _t_turn:.1f}s after turn start (research {research_s:.1f}s)")
                    assembled.append(chunk.text_delta)
                    stream_callback(chunk.text_delta)
                if chunk.done:
                    final_chunk = chunk
        except RuntimeError as e:
            return "error", f"Both engines are down, sir. ({e})"

        reply_text = "".join(assembled)
        if final_chunk and final_chunk.cancelled:
            reply_text = reply_text or "Cancelled, sir."
            _append_history("user", user_input, conversation=conversation)
            _append_history("assistant", reply_text, conversation=conversation)
            return "cancelled", reply_text
        # What goes into HISTORY is only what the model actually wrote. The failure
        # note below is for the person reading the chat; it must never be fed back
        # to the model (it used to be, so Ember then talked about "a server timeout"
        # and re-offered the answer instead of simply finishing it).
        history_text = reply_text
        if final_chunk and final_chunk.recovered_from:
            print(f"[ember_core] reply was cut off ({final_chunk.recovered_from}) and continued on {final_chunk.model}.")
        if final_chunk and final_chunk.error:
            print(f"[ember_core] reply cut off and could not be recovered: {final_chunk.error}")
            note = "[The reply was cut off by a provider error, sir. Say \"continue\" and I'll pick it up.]"
            reply_text = f"{reply_text}\n\n{note}" if reply_text else note

        actually_grounded = search_evidence_used or verified_via_evidence
        source = final_chunk.source if final_chunk else "local"
        model = final_chunk.model if final_chunk else "unknown"
        tag = model + ("+search" if actually_grounded else "") if source in ("cloud", "fallback") else "local" + ("+search" if actually_grounded else "")

        memory_candidate = extract_memory_candidate(user_input)
        if memory_candidate:
            store_result = memory.remember(memory_candidate, llm_client.embed)
            if store_result.get("warning"):
                print(f"[ember_core] {store_result['warning']}")

        _append_history("user", user_input, conversation=conversation)
        _append_history("assistant", history_text or reply_text, used_search=actually_grounded, conversation=conversation)
        print(f"[ember_core] turn total {time.perf_counter() - _t_turn:.1f}s (research {research_s:.1f}s, streamed)")
        return tag, reply_text

    try:
        result = llm_client.generate(
            effective_prompt,
            system_prompt=system_prompt,
            use_search=wants_search,
            history=history_snapshot,
        )
    except RuntimeError as e:
        return "error", f"Both engines are down, sir. ({e})"

    reply_text = result.text
    if intent == "verify" and not verified_via_evidence and not result.grounded:
        # No independent evidence AND no grounding happened at all —
        # letting a model confidently restate or "reconsider" the
        # same claim is exactly the failure mode this intent exists
        # to prevent. Say so plainly.
        reply_text = (
            "I can't actually verify that right now, sir — no search "
            "evidence was available on any path, and re-answering from the "
            "same knowledge that produced the original claim wouldn't tell "
            "us anything new. Worth checking manually until search access "
            "is back."
        )
    elif wants_search and not result.grounded:
        # Point 1 fix (reported directly): this used to just append
        # a small caveat note to whatever reply_text already
        # contained — which meant a request classified as needing current
        # information could still show an ungrounded guess (very
        # often from local Ollama, which has no search capability
        # at all) as if it were a normal answer, with only a small
        # note tacked on. That's exactly the failure mode reported:
        # "search required -> all search-capable providers fail ->
        # Ollama answers from its static knowledge" dressed up to
        # look like a real response.
        #
        # The fix is the binary outcome originally specified:
        # search succeeded -> grounded answer (handled above,
        # unaffected), or search could not be performed -> say so
        # plainly and show NOTHING ELSE. reply_text is replaced
        # entirely here, not appended to — whatever text
        # result.text contains (Ollama's static-knowledge guess, or
        # a fallback tier answering despite being told to search)
        # is discarded rather than shown.
        #
        # Checking result.grounded, not result.source != "cloud" —
        # a fallback tier (Cerebras/Groq/NVIDIA/Mistral) can now
        # actually ground its answer too, by choosing to call the
        # web_search tool (see llm_client.py's tool-calling loop),
        # and Gemini's own grounded flag is now read from real
        # grounding metadata, not assumed from the request flag.
        # So this branch is only reachable when NO backend anywhere
        # actually retrieved real evidence — which is precisely the
        # "search cannot be performed" state.
        reply_text = (
            "I can't get you a grounded answer on that right now, sir — search "
            "access is unavailable across everything I tried (cloud quota, fallback "
            "providers, and local has no search capability at all). I'd rather tell "
            "you that plainly than hand you an unverified guess."
        )
        # Note: this branch is unreachable when search_evidence_used
        # was True (evidence pre-fetched above) — wants_search is
        # already False in that case, so a reply that genuinely had
        # real evidence behind it never gets discarded here even
        # though result.grounded itself may read False (that
        # grounding happened via injected text, not via Gemini's
        # own tool or a fallback tier's tool call).

    memory_candidate = extract_memory_candidate(user_input)
    if memory_candidate:
        store_result = memory.remember(memory_candidate, llm_client.embed)
        if store_result.get("warning"):
            print(f"[ember_core] {store_result['warning']}")

    # Bug found in an earlier pass, same class as the originally-reported
    # one: search_evidence_used only ever gets set True in the "search"
    # intent branch above. A "verify" turn that got real, independently-
    # retrieved Tavily evidence (verified_via_evidence) was genuinely
    # grounded — the model was handed real current data — but generate()
    # is called with use_search=False for that path (the evidence is
    # already embedded in the prompt text), so result.grounded reads
    # False too. Without including verified_via_evidence here, a verify
    # turn that was actually checked against real evidence got
    # used_search=False recorded in history — meaning a bare follow-up
    # right after it (_recent_turn_used_search) would wrongly conclude
    # the previous turn was NOT search-grounded and fail to inherit
    # search intent.
    actually_grounded = result.grounded or search_evidence_used or verified_via_evidence
    if result.source in ("cloud", "fallback"):
        tag = result.model + ("+search" if actually_grounded else "")
    else:
        tag = "local" + ("+search" if actually_grounded else "")

    _append_history("user", user_input, conversation=conversation)
    _append_history("assistant", reply_text, used_search=actually_grounded, conversation=conversation)

    print(f"[ember_core] turn total {time.perf_counter() - _t_turn:.1f}s (research {research_s:.1f}s, not streamed)")
    return tag, reply_text


def warm_up_models() -> None:
    """Starts a background thread that pays one-time startup costs (local embedding model load,
    Gemini connection) so the FIRST real turn isn't slower than every later one. Call once after
    startup_check(); returns immediately."""
    import threading
    threading.Thread(target=llm_client.warm_up, name="ember-warmup", daemon=True).start()


def run() -> None:
    startup_check()
    runtime = _build_runtime()
    runtime.start()
    warm_up_models()

    # Resume short-term context if we're restarting shortly after the last
    # turn (crash, planned restart, laptop sleep/wake) — see
    # ember_session.py for the exact window/bounds. Outside that window
    # this returns [] and Ember starts a clean slate, same as it always
    # has; nothing here silently drags an old, unrelated conversation into
    # a new one.
    resumed = _default_conversation.load_resumable_history(HISTORY_MAX_TURNS)
    if resumed:
        _default_conversation.seed_history(resumed)
        print(f"Ember: Welcome back, sir — picking up where we left off ({len(resumed)} recent turn(s) restored).\n")

    print("Type 'exit' or 'quit' to shut down.\n")

    while True:
        try:
            user_input = input("You: ").strip()

            if not user_input:
                continue

            if user_input.lower() in {"exit", "quit"}:
                print("Ember: Goodbye, sir.")
                break

            # CLI now streams too (this session) — previously the ONLY
            # caller of process_turn() that never passed a
            # stream_callback at all, which meant every single CLI reply
            # was fully blocking regardless of intent, and a
            # search/verify turn that fell back to forced native/tool
            # grounding (see _emit_stream_status in ember_core.py) sat
            # sir looking at a bare "You: " prompt for however long that
            # took with zero indication anything was happening. This is
            # the actual "silent latency" gap for the interface sir
            # currently uses daily — the transport layer (no client yet)
            # was never where this bit in practice.
            #
            # prefix_shown/streamed_any_text are closed over by
            # _cli_stream below via `nonlocal` — freshly rebound each
            # loop iteration since _cli_stream itself is redefined every
            # time through this loop.
            prefix_shown = False
            streamed_any_text = False

            def _cli_stream(text, kind="text"):
                nonlocal prefix_shown, streamed_any_text
                if kind == "status":
                    # Transient progress note — always starts on its own
                    # line, distinguished with brackets so it's never
                    # mistaken for actual reply content, and never
                    # persisted (process_turn already keeps these out of
                    # reply_text/history entirely).
                    if prefix_shown:
                        print()
                    print(f"Ember: [{text}]")
                    prefix_shown = False  # next content re-opens its own "Ember: " line
                else:
                    if not prefix_shown:
                        print("Ember: ", end="", flush=True)
                        prefix_shown = True
                    print(text, end="", flush=True)
                    streamed_any_text = True

            tag, reply_text = process_turn(user_input, _default_conversation, stream_callback=_cli_stream)

            if not streamed_any_text:
                # Action/command intents, cancellations, and the
                # forced-blocking-grounding path never invoke the
                # streaming callback with real text (see process_turn's
                # docstring) — only status notes may have printed above,
                # if any. Print the complete reply now instead of
                # leaving nothing after those.
                if prefix_shown:
                    print()
                print(f"Ember: {reply_text}", end="")
            print(f"  [{tag}]\n")

        except KeyboardInterrupt:
            print("\nEmber: Shutting down.")
            break

        except Exception as e:
            print(f"[Ember error] {e}\n")


if __name__ == "__main__":
    run()