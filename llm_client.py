"""
llm_client.py — Ember's LLM adapter layer.

This is the ONLY piece of Ember that knows how to talk to a language model.
Everything else in Ember (orchestrator, memory, tools) calls generate() and
gets a GenerateResult back — it never needs to know whether that text came
from the cloud or from the local fallback model.

Behavior:
  1. Try Gemini cloud models, best-quality first, skipping any tier whose
     tracked daily quota is already exhausted (no point paying round-trip
     latency for a call we know will 429).
  2. If a tier fails anyway (network error, unexpected 429, empty response),
     fall through to the next cloud tier, then finally to local Ollama.
  3. Report which source AND which model actually answered.

Quota tracking (Aug 2026 free tier, see conversation history for full table):
    gemini-3.5-flash      -> 20 RPD
    gemini-3.5-flash-lite -> 500 RPD
Both share 5-15 RPM / 250K TPM, which we don't track locally (RPD is the
one that actually bites a long-running assistant). Counts are persisted to
quota_state.json next to this file and reset at midnight Pacific time,
matching Google's reset schedule. This is a local estimate, not a live
readout from Google — if quota is also consumed by another process/testing
outside Ember, our count can be stale and we'll still occasionally hit a
real 429, which is caught and handled the same as any other cloud failure.
"""

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google import genai
from google.genai import types
import requests

# Load variables from a .env file at the project root into the process
# environment, if one exists. This has to happen before anything below
# reads os.environ — GEMINI_API_KEY was previously working because it's
# set as a permanent Windows environment variable, not because anything
# here read a file. That doesn't scale to five optional provider keys,
# so from here on .env is the single place all of them live.
load_dotenv()

# ---- Configuration ----------------------------------------------------

# Ordered best-quality-first. Ember tries each in order, skipping any tier
# whose tracked daily quota is already spent, before falling back to local.
_CLOUD_TIERS_QUALITY_FIRST = [
    {"model": "gemini-3.5-flash", "rpd": 20},
    {"model": "gemini-3.5-flash-lite", "rpd": 500},
]
# LATENCY (reported: 10-15+ s for a simple reply): the quality-first order above sends the first
# 20 requests EVERY DAY to gemini-3.5-flash, whose default "thinking" phase measured 16 s for a
# one-line greeting. Interactive use (chat and especially voice) wants the fast model first, with
# the bigger one as the fallback. EMBER_CLOUD_ORDER=quality restores the old order.
CLOUD_TIERS = (
    list(_CLOUD_TIERS_QUALITY_FIRST)
    if os.environ.get("EMBER_CLOUD_ORDER", "fast").lower() == "quality"
    else list(reversed(_CLOUD_TIERS_QUALITY_FIRST))
)

# How much Gemini "thinks" before answering. Thinking is what turns a 1 s answer into a 10-20 s
# one for a chatty assistant. "minimal" -> "low" -> budget 0 -> API default is tried in that order
# per model until the API accepts one (models differ in which knob they support), and the
# accepted choice is remembered. EMBER_GEMINI_THINKING = minimal | low | medium | high | default.
GEMINI_THINKING = os.environ.get("EMBER_GEMINI_THINKING", "minimal").lower()

OLLAMA_MODEL = "qwen2.5:7b-instruct-q4_K_M"
OLLAMA_HOST = "http://localhost:11434"
OLLAMA_GENERATE_URL = f"{OLLAMA_HOST}/api/generate"

# Embeddings use a separate model from chat generation. As of this writing
# we have not confirmed whether this model's free-tier quota is tracked
# separately from the CLOUD_TIERS chat quota above or shares a pool with
# it — treat that as an open question, not an assumption. Because of that
# uncertainty, the cloud embedding path does NOT go through QuotaTracker at
# all right now; it just tries the call and reports failure honestly if it
# 429s. If usage shows this needs its own tracked budget, add it as a
# genuine follow-up, not a guess baked in now.
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_CHAR_LIMIT = 2048

# ---- Local embedding (fastembed / ONNX runtime) --------------------------
# Closes the gap flagged directly in ember_memory.py's own docstring: no
# local embedding fallback existed, so cloud-unavailable/quota-exhausted/
# offline all degraded semantic memory to keyword matching even though
# embeddings are cheap enough to run locally on this hardware (unlike chat
# generation, which is why generate() stays cloud-first while embed() is
# local-first).
#
# fastembed (Qdrant) chosen over sentence-transformers deliberately: it
# runs on onnxruntime, not torch — a meaningfully smaller dependency and
# faster cold-start on a CPU-only always-on-server box (the Vivobook), and
# there's no other reason to want torch installed here. BAAI/bge-small-en-v1.5
# is ~130MB quantized, 384-dim, and is a solid general-purpose embedding
# model for the personal-fact-recall volume this project actually has.
#
# Model files are downloaded from Hugging Face on first use and cached
# under LOCAL_EMBEDDING_CACHE_DIR — this download itself requires internet
# access once; after that, embedding is fully offline. That's a one-time
# exception to "local-first," not a contradiction of it.
LOCAL_EMBEDDING_MODEL_NAME = "BAAI/bge-small-en-v1.5"
LOCAL_EMBEDDING_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "embedding_models")
LOCAL_EMBEDDING_SOURCE = f"local:{LOCAL_EMBEDDING_MODEL_NAME}"
CLOUD_EMBEDDING_SOURCE = f"cloud:{EMBEDDING_MODEL}"

_local_embed_model = None
_local_embed_init_attempted = False


def _get_local_embed_model():
    """Lazily loads the local embedding model on first use (not at import
    time — a slow or failed model load shouldn't delay or crash module
    import, same startup-safety contract as the Gemini client above).
    Returns None (and stays None thereafter — no per-call retry storm) if
    fastembed isn't installed or the model can't be loaded/downloaded."""
    global _local_embed_model, _local_embed_init_attempted
    if _local_embed_init_attempted:
        return _local_embed_model
    _local_embed_init_attempted = True
    try:
        from fastembed import TextEmbedding
        os.makedirs(LOCAL_EMBEDDING_CACHE_DIR, exist_ok=True)
        _local_embed_model = TextEmbedding(
            model_name=LOCAL_EMBEDDING_MODEL_NAME,
            cache_dir=LOCAL_EMBEDDING_CACHE_DIR,
        )
        print(f"[llm_client] Local embedding model ready ({LOCAL_EMBEDDING_MODEL_NAME}) — memory recall no longer depends on cloud availability.")
    except Exception as e:
        print(f"[llm_client] Local embedding unavailable ({e}); embedding will use cloud, if available, instead.")
        _local_embed_model = None
    return _local_embed_model


def local_embedding_available() -> bool:
    """Cheap check for startup_check() — does not force model load if it
    hasn't been attempted yet, so this alone won't trigger the (slower)
    first-use download; it just reports whether that's already happened."""
    return _local_embed_model is not None


def _embed_local(text: str) -> "list[float] | None":
    model = _get_local_embed_model()
    if model is None:
        return None
    try:
        vector = next(model.embed([text]))
        return vector.tolist()
    except Exception as e:
        print(f"[llm_client] Local embedding call failed ({e}); falling back to cloud for this call.")
        return None

# ---- OpenAI-compatible fallback tiers -----------------------------------
# Tried in this order after both Gemini tiers are exhausted/unavailable,
# before dropping to local Ollama. All four expose an OpenAI-compatible
# /chat/completions endpoint, so one generic caller (_generate_openai_compatible)
# handles all of them — no new SDK dependency, just `requests`.
#
# None of these support Google Search grounding — that's a Gemini-only
# capability. generate() reports this honestly via GenerateResult.source
# so ember_core.py's search-caveat logic covers these tiers too, not just
# local Ollama.
#
# Ordered by published daily-request generosity, since that's the thing
# that actually bit us in practice (see conversation history: two Gemini
# tiers exhausted within a handful of turns). "Published" is the key word:
# NVIDIA NIM and Mistral don't publish a daily cap (NVIDIA rate-limits by
# RPM instead, ~40 RPM per NVIDIA's own developer forum staff; Mistral's
# free "Experiment" tier is described only as "rate-limited for evaluation"
# with no public numbers as of Aug 2026) — so those two can't be proactively
# skipped the way Cerebras/Groq/Gemini can. They're tried reactively:
# attempt the call, and if it fails, move on, same as any other failure.
#
# Each api_key_env being unset means that tier is silently skipped — you
# don't need every one of these accounts for Ember to work, only the ones
# you've actually signed up for.
FALLBACK_TIERS = [
    {
        "name": "cerebras",
        "base_url": "https://api.cerebras.ai/v1",
        "model": "gpt-oss-120b",  # llama-3.3-70b was deprecated Feb 2026 — this replaces it
        "api_key_env": "CEREBRAS_API_KEY",
        "rpd": 14400,  # published, confirmed current Aug 2026
    },
    {
        "name": "groq",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-20b",  # llama-3.1-8b-instant deprecated June 2026 — this replaces it
        "api_key_env": "GROQ_API_KEY",
        "rpd": None,  # only a daily TOKEN cap (200,000 TPD) is published for this model, not a request cap — can't proactively track a request-count limit we don't have, so this is now reactive-only like NVIDIA/Mistral, same honest treatment
    },
    {
        "name": "nvidia_nim",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "model": "meta/llama-3.1-70b-instruct",
        "api_key_env": "NVIDIA_API_KEY",
        "rpd": None,  # no published daily cap — reactive only
    },
    {
        "name": "mistral",
        "base_url": "https://api.mistral.ai/v1",
        "model": "mistral-small-latest",
        "api_key_env": "MISTRAL_API_KEY",
        "rpd": None,  # no published numbers as of Aug 2026 — reactive only
    },
]

_QUOTA_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quota_state.json")


# ---- Result type --------------------------------------------------------

@dataclass
class GenerateResult:
    text: str
    source: str       # "cloud" (Gemini), "fallback" (OpenAI-compatible tier), or "local" (Ollama)
    model: str        # actual model name that answered
    grounded: bool    # whether this reply actually used live search evidence,
                       # verified from the response itself, not merely
                       # requested. On "cloud" this is read from Gemini's
                       # own grounding_metadata (web_search_queries /
                       # grounding_chunks) — Gemini can be handed the
                       # search tool and still choose not to use it, and
                       # this now reflects that honestly instead of
                       # assuming True whenever search was requested. On
                       # "fallback" this is True only when a tool call
                       # returned real, non-empty evidence (not merely
                       # attempted — see _generate_openai_compatible).
                       # Always False on "local" (Ollama gets no
                       # tool-calling; ember_core.py instead pre-injects
                       # Tavily evidence into the prompt text for
                       # search-intent messages before ever calling
                       # generate(), so local still benefits, just via a
                       # different path).


# ---- Quota tracking -----------------------------------------------------

class QuotaTracker:
    """Tracks requests-per-day against each cloud tier, persisted to disk,
    resetting at midnight Pacific time to match Google's RPD reset schedule."""

    def __init__(self, path: str, tiers: list[dict]):
        self.path = path
        self.limits = {t["model"]: t["rpd"] for t in tiers}
        self._date = None
        self._counts: dict[str, int] = {}
        self._load()

    @staticmethod
    def _today_pt() -> str:
        return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()

    def _load(self):
        data = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                data = {}
        today = self._today_pt()
        if data.get("date") != today:
            data = {"date": today, "counts": {}}
        self._date = data["date"]
        self._counts = data.get("counts", {})

    def _save(self):
        try:
            with open(self.path, "w") as f:
                json.dump({"date": self._date, "counts": self._counts}, f)
        except OSError as e:
            print(f"[llm_client] Warning: couldn't persist quota state ({e}).")

    def _roll_if_new_day(self):
        today = self._today_pt()
        if today != self._date:
            self._date = today
            self._counts = {}
            self._save()

    def remaining(self, model: str) -> "int | None":
        """Returns None for untracked models (treated as unlimited/unknown)."""
        self._roll_if_new_day()
        limit = self.limits.get(model)
        if limit is None:
            return None
        return max(0, limit - self._counts.get(model, 0))

    def can_use(self, model: str) -> bool:
        r = self.remaining(model)
        return r is None or r > 0

    def record(self, model: str):
        self._roll_if_new_day()
        self._counts[model] = self._counts.get(model, 0) + 1
        self._save()

    def status(self) -> dict:
        return {m: self.remaining(m) for m in self.limits}


_quota = QuotaTracker(
    _QUOTA_STATE_PATH,
    CLOUD_TIERS + [t for t in FALLBACK_TIERS if t["rpd"] is not None],
)


# ---- Cloud client setup (startup-safe) -----------------------------------
# We do NOT let a missing/bad API key crash the whole module at import time.
# If Gemini can't be initialized, Ember should still run in local-only mode
# rather than dying on startup.
#
# GEMINI_REQUEST_TIMEOUT_MS: real, reproduced bug (voice transcript, "it just
# doesn't respond suddenly... says please say that again... doesn't work till
# I restart it"). Every OTHER network call in this file has an explicit
# timeout (Tavily 15s, OpenAI-compatible tiers 30s, Ollama 120s) — the
# Gemini SDK calls (generate_content, generate_content_stream,
# embed_content, and models.get/list used by warm_up() above) had NONE. A
# single stalled connection (dead WiFi, a Google-side hiccup) let one of
# those calls hang forever, not just slowly — and since the non-streaming
# generate() path is only cancellable BETWEEN calls (see its own
# docstring), a hang INSIDE one meant process_turn() itself never returned.
# That leaves the transport's turn_active() stuck True permanently, so every
# later utterance hits ember_voice.py's busy-wait path, times out, and gets
# told "Still finishing the last one, sir. Say that again in a moment."
# forever — not because anything is actually still finishing, but because
# nothing ever will, since the stuck call has no way to fail on its own. A
# full process restart was the only fix, because nothing else could ever
# un-stick a truly unbounded wait. http_options bounds every request this
# client makes (chat, streaming, embeddings, warm-up, and the vision calls
# in ember_core.py, which share this same client instance) to
# GEMINI_REQUEST_TIMEOUT_MS — generous enough for the slowest grounded
# replies actually observed in practice (30+ seconds in the same transcript
# that reproduced this bug), but finite, so a genuinely dead connection now
# surfaces as an ordinary caught exception (falls through to the next tier,
# same as any other cloud failure) instead of a permanent wedge.
GEMINI_REQUEST_TIMEOUT_MS = int(os.environ.get("EMBER_GEMINI_TIMEOUT_MS", "45000"))

_gemini_client = None
_gemini_init_error = None

try:
    if not os.environ.get("GEMINI_API_KEY"):
        raise RuntimeError("GEMINI_API_KEY environment variable is not set.")
    try:
        _gemini_client = genai.Client(http_options=types.HttpOptions(timeout=GEMINI_REQUEST_TIMEOUT_MS))
    except Exception as http_opts_error:
        # Defensive, not expected in practice: an older google-genai build
        # that doesn't accept http_options here would otherwise take down
        # the whole client (and therefore all of Ember's cloud tier) over a
        # constructor-signature mismatch, for a feature that's explicitly
        # optional protection, not core functionality. Degrade to no
        # client-side timeout — the original (buggy) behavior — rather than
        # failing startup, same fail-open contract as everything else here.
        print(f"[llm_client] Couldn't set a Gemini request timeout ({http_opts_error}) — "
              f"falling back to no client-side timeout; a network stall could hang a turn indefinitely.")
        _gemini_client = genai.Client()
except Exception as e:
    _gemini_init_error = str(e)


_UNSET = object()
_thinking_choice: dict = {}   # model -> the ThinkingConfig (or None) the API accepted


def _thinking_candidates() -> list:
    """Configs to try, best-for-latency first. None means 'send no thinking config'."""
    if GEMINI_THINKING == "default":
        return [None]
    level_enum = getattr(types, "ThinkingLevel", None)
    names = ["minimal", "low"] if GEMINI_THINKING == "minimal" else [GEMINI_THINKING]
    out = []
    for name in names:
        member = getattr(level_enum, name.upper(), None) if level_enum is not None else None
        if member is not None:
            out.append(types.ThinkingConfig(thinking_level=member))
    if GEMINI_THINKING in ("minimal", "low"):
        out.append(types.ThinkingConfig(thinking_budget=0))   # 2.5-family style knob
    out.append(None)
    return out


def _looks_like_thinking_rejection(e: Exception) -> bool:
    msg = str(e).lower()
    return any(k in msg for k in ("thinking", "thinkinglevel", "thinking_level", "budget")) and (
        "400" in msg or "invalid" in msg or "not supported" in msg or "unsupported" in msg
    )


def _describe_thinking(candidate) -> str:
    if candidate is None:
        return "API default"
    if getattr(candidate, "thinking_level", None) is not None:
        return f"level={candidate.thinking_level}"
    return f"budget={getattr(candidate, 'thinking_budget', None)}"


def _gemini_config(use_search: bool, thinking):
    """GenerateContentConfig for a call, or None if it would be empty."""
    kwargs = {}
    if use_search:
        kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
    if thinking is not None:
        kwargs["thinking_config"] = thinking
    return types.GenerateContentConfig(**kwargs) if kwargs else None


def _call_with_thinking(model: str, call):
    """call(thinking_config) -> result. Walks down _thinking_candidates() while the API rejects
    the thinking parameter itself; any OTHER error (429, 5xx, network) propagates untouched so
    the caller's normal tier fallback still works. The first accepted config is cached per model."""
    chosen = _thinking_choice.get(model, _UNSET)
    candidates = [chosen] if chosen is not _UNSET else _thinking_candidates()
    last = None
    for candidate in candidates:
        try:
            result = call(candidate)
        except Exception as e:
            if chosen is _UNSET and _looks_like_thinking_rejection(e):
                print(f"[llm_client] {model}: thinking setting rejected ({str(e)[:120]}); trying the next option.")
                last = e
                continue
            raise
        if chosen is _UNSET:
            _thinking_choice[model] = candidate
            print(f"[llm_client] {model}: thinking setting in use -> {_describe_thinking(candidate)}")
        return result
    raise last


def warm_up() -> None:
    """Pays the one-time costs at startup instead of inside the user's first turn: loads the local
    embedding model (used by memory recall on any personal message) and opens the HTTPS connection
    to Gemini with a metadata GET, which costs no generation quota. Safe to call from a thread."""
    t0 = time.perf_counter()
    try:
        _get_local_embed_model()
    except Exception as e:
        print(f"[llm_client] warm-up: local embedding skipped ({e})")
    if _gemini_client is not None:
        try:
            _gemini_client.models.get(model=CLOUD_TIERS[0]["model"])
        except Exception as e:
            print(f"[llm_client] warm-up: Gemini connection check skipped ({str(e)[:100]})")
    print(f"[llm_client] Warm-up done ({time.perf_counter() - t0:.1f}s).")


def cloud_available() -> bool:
    return _gemini_client is not None


def _tier_has_key(tier: dict) -> bool:
    return bool(os.environ.get(tier["api_key_env"]))


def configured_fallback_tiers() -> list:
    """Names of fallback tiers whose API key is actually set. Used at
    startup so sir can see which of the optional providers are live
    without digging through .env."""
    return [t["name"] for t in FALLBACK_TIERS if _tier_has_key(t)]


def quota_status() -> dict:
    """Remaining requests today per tracked tier (Gemini + any fallback
    tier that publishes a daily cap), e.g. {'gemini-3.5-flash': 12, ...}."""
    return _quota.status()


def check_ollama_available(timeout: float = 3.0) -> bool:
    """Pings the Ollama base URL. Returns False on any connection issue."""
    try:
        r = requests.get(OLLAMA_HOST, timeout=timeout)
        return r.status_code == 200
    except requests.RequestException:
        return False


def embed(text: str) -> "tuple[list[float], str] | None":
    """
    Return (embedding_vector, source_tag) for `text`, or None if no
    embedding backend is currently available at all.

    Order: local (fastembed, ONNX, offline after first download) first,
    then cloud (gemini-embedding-001) if local isn't available, then None.
    This is the opposite order from generate()'s cloud-first policy —
    deliberately: chat generation on this hardware genuinely needs cloud
    quality, but embedding a short fact is cheap enough to run locally,
    and doing so keeps semantic memory working (and private) regardless of
    cloud quota/availability/internet, exactly the gap flagged in
    ember_memory.py's own docstring.

    source_tag identifies which model/dimension produced the vector (e.g.
    "local:BAAI/bge-small-en-v1.5" vs "cloud:gemini-embedding-001").
    Callers MUST NOT cosine-compare vectors from different source_tags —
    they are different vector spaces with, in general, different
    dimensionality. ember_memory.py stores this tag alongside each vector
    and only compares like-for-like; anything else is silently wrong math,
    not just noisy math.

    Callers (ember_memory.py) are expected to treat a None return as an
    ordinary, expected outcome — not an exception to catch — and degrade
    to keyword-based recall. This mirrors how generate() treats local
    fallback: a missing capability, not a crash.
    """
    local_vector = _embed_local(text[:EMBEDDING_CHAR_LIMIT])
    if local_vector is not None:
        return local_vector, LOCAL_EMBEDDING_SOURCE

    if not cloud_available():
        return None
    try:
        result = _gemini_client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text[:EMBEDDING_CHAR_LIMIT],
        )
        return list(result.embeddings[0].values), CLOUD_EMBEDDING_SOURCE
    except Exception as e:
        print(f"[llm_client] Cloud embedding failed ({e}); recall will degrade to keyword matching.")
        return None


# ---- Autonomous search tool (function-calling) --------------------------
# Ported architecture, not code, from jarvisforember's core/jarvis_local.py:
# that repo gives every OpenAI-compatible backend a `background_search`
# function via native tool-calling (tools=[...], tool_choice="auto") and
# lets the MODEL decide when it needs current information, rather than a
# regex pre-classifying the message. That's a genuinely better mechanism
# than growing ember_core.py's keyword list forever, for one concrete
# reason: it's semantic, not lexical. A query like "what's the deal with
# Kevin Estre's move to WSBK" needs a search and contains none of
# needs_search()'s trigger words — no regex list will ever fully close
# that gap, but a model deciding for itself whether it actually knows the
# answer will catch it.
#
# What was NOT ported: jarvisforember's own search backend for that tool
# (scraping Google News RSS and DuckDuckGo Lite's HTML with BeautifulSoup —
# no API, no key, fragile to markup changes, easily blocked). Ember already
# has a real, working search backend in web_search() above (Tavily, a
# proper API with a published free tier) — the tool below just exposes
# that existing function to the model instead of ember_core.py pre-calling
# it, so THIS is a wrapper around already-battle-tested code, not new
# search infrastructure.
#
# Deliberately Gemini-excluded: Gemini keeps its own native Google Search
# grounding (types.Tool(google_search=...)), which is higher-quality
# first-party grounding and already gated by needs_search()/classify_intent
# specifically because grounding draws from the same metered quota as chat
# generation on the 20 RPD tier. Layering a second, different search tool
# on top of Gemini would just be redundant. This tool is offered to the
# OpenAI-compatible FALLBACK_TIERS only (Cerebras, Groq, NVIDIA NIM,
# Mistral) — which is exactly where the reported Groq hallucination
# happened, and exactly where use_search was previously being silently
# dropped (see the bug note in _generate_openai_compatible below).
#
# Deliberately NOT given to local Ollama: qwen2.5:7b-instruct via the
# /api/generate completion endpoint is not a reliable native tool-caller —
# small local models frequently either ignore tool schemas or hallucinate
# malformed calls. Ollama stays on the older, more deterministic path:
# ember_core.py pre-fetches evidence via web_search() itself (same
# mechanism the "verify" intent already uses successfully) and injects it
# straight into the prompt as text, which every backend can use regardless
# of tool-calling support.
WEB_SEARCH_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Searches the live web for current information. Use this for "
            "anything involving recent events, news, current status of a "
            "person/organization/product, prices, scores, or any factual "
            "question you are not confident you know the correct, current "
            "answer to. Do NOT use this for conversational messages, "
            "greetings, or things you already know with confidence and "
            "that are unlikely to have changed (e.g. historical facts, "
            "how something works, math). Only call this when you genuinely "
            "need external, current data."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."}
            },
            "required": ["query"],
        },
    },
}

MAX_TOOL_ITERATIONS = 3  # bounded round-trips per reply; mirrors jarvis_local.py's MAX_ITERATIONS pattern


TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_TIMEOUT_SECONDS = float(os.environ.get("EMBER_TAVILY_TIMEOUT", "8"))
_tavily_session = requests.Session()   # keeps the TLS connection open between searches (saves ~0.3-0.6 s each)


def web_search(query: str, max_results: int = 5) -> "list[dict] | None":
    """
    Standalone web search, independent of any LLM provider's own grounding.
    Returns a list of {"title", "url", "content"} dicts, or None if
    TAVILY_API_KEY isn't set or the call fails.

    This is what makes real verification possible regardless of which
    backend ends up answering. Gemini's Search grounding only works on
    Gemini itself — a claim checked while running on Cerebras, Groq, or
    local Ollama previously had no way to actually verify anything, only
    to honestly admit it couldn't. This gives every backend the same
    retrieved evidence to evaluate, since the evidence is just text
    handed into the prompt, not something only Gemini can produce.

    Requires a free Tavily account (tavily.com — permanent free tier,
    1,000 searches/month, no card required). If TAVILY_API_KEY isn't set,
    this returns None and callers fall back to their previous behavior
    (forcing Gemini grounding, or an honest refusal if that's also
    unavailable) — same graceful-degradation contract as everything else
    in this file.
    """
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        return None
    t_search = time.perf_counter()
    try:
        response = _tavily_session.post(
            TAVILY_SEARCH_URL,
            json={
                "api_key": api_key,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
            },
            timeout=TAVILY_TIMEOUT_SECONDS,
        )
        if not response.ok:
            print(f"[llm_client] web_search returned HTTP {response.status_code}: {response.text[:300]}")
            return None
        data = response.json()
        print(f"[llm_client] web_search took {time.perf_counter() - t_search:.1f}s ({len(data.get('results', []))} results)")
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", ""),
            }
            for r in data.get("results", [])
        ]
    except Exception as e:
        print(f"[llm_client] web_search failed ({e}).")
        return None


# ---- Public interface ---------------------------------------------------

def generate(prompt: str, system_prompt: str = "", use_search: bool = False, history: "list | None" = None) -> GenerateResult:
    t_start = time.perf_counter()
    """
    Generate a reply to `prompt`.

    Tries, in order:
      1. Gemini cloud tiers, best-quality first (skipping any whose tracked
         daily quota is already spent). Only this stage supports Search
         grounding.
      2. FALLBACK_TIERS, in the order defined above — each skipped
         entirely if its API key isn't set, and skipped if its tracked
         daily quota is spent (for the two tiers that publish one).
      3. Local Ollama.

    Args:
        prompt: the user's message (or, for verification-intent turns,
            ember_core's rewritten prompt that hands the model its own
            prior claim to check).
        system_prompt: Ember's persona/instructions, prepended to the prompt.
        use_search: if True, requests grounding. On Gemini this enables
            native Google Search grounding. On fallback tiers (Cerebras,
            Groq, NVIDIA NIM, Mistral) this adds a strong system-prompt
            directive telling the model to call the web_search tool before
            answering, on top of the tool already being available on every
            fallback call regardless of this flag (tool_choice="auto" lets
            the model decide for itself, as a safety net under
            ember_core.py's regex classifier). Local Ollama gets neither —
            it has no tool-calling and no grounding; ember_core.py instead
            pre-fetches evidence itself for search-flagged messages and
            injects it into the prompt text before ever calling generate().
            GenerateResult.grounded honestly reflects whether evidence was
            actually used, not just requested — see its docstring.
        history: optional list of {"role": "user"|"assistant", "content": str}
            dicts, most recent last. Without this, follow-up questions like
            "what about X?" are unanswerable — the model has no idea what
            X is being compared to. Threaded through to whichever backend
            actually answers.

    Returns:
        GenerateResult(text, source, model, grounded)

    Raises:
        RuntimeError if every tier — Gemini, every configured fallback,
        and local — fails.
    """
    history = history or []

    if cloud_available():
        for tier in CLOUD_TIERS:
            model = tier["model"]
            if not _quota.can_use(model):
                continue
            try:
                text, grounded = _generate_cloud(prompt, system_prompt, use_search, model, history)
                _quota.record(model)
                print(f"[llm_client] {model} answered in {time.perf_counter() - t_start:.1f}s (not streamed)")
                return GenerateResult(text=text, source="cloud", model=model, grounded=grounded)
            except Exception as e:
                print(f"[llm_client] {model} failed ({e}); trying next option.")
                continue
    else:
        print(f"[llm_client] Cloud unavailable ({_gemini_init_error}); trying fallbacks/local.")

    for tier in FALLBACK_TIERS:
        if not _tier_has_key(tier):
            continue  # not configured — skip silently, not an error
        if tier["rpd"] is not None and not _quota.can_use(tier["model"]):
            continue
        try:
            text, searched = _generate_openai_compatible(prompt, system_prompt, tier, history, use_search=use_search)
            if tier["rpd"] is not None:
                _quota.record(tier["model"])
            return GenerateResult(text=text, source="fallback", model=f"{tier['name']}:{tier['model']}", grounded=searched)
        except Exception as e:
            print(f"[llm_client] {tier['name']} failed ({e}); trying next option.")
            continue

    try:
        text = _generate_local(prompt, system_prompt, history)
        return GenerateResult(text=text, source="local", model=OLLAMA_MODEL, grounded=False)
    except Exception as e:
        raise RuntimeError(
            f"Every tier failed — Gemini, all configured fallbacks, and local. Local error: {e}"
        ) from e


# ---- History serialization ----------------------------------------------

def _history_to_transcript(history: list) -> str:
    """Serialize history into plain text for text-completion-style backends
    (Gemini's flat-prompt call here, and Ollama's /api/generate, which is
    completion-style rather than turn-aware). Empty string if no history."""
    if not history:
        return ""
    lines = []
    for turn in history:
        label = "User" if turn["role"] == "user" else "Ember"
        lines.append(f"{label}: {turn['content']}")
    return "\n".join(lines) + "\n"


# ---- Backend implementations ------------------------------------------

def _generate_cloud(prompt: str, system_prompt: str, use_search: bool, model: str, history: "list | None" = None) -> "tuple[str, bool]":
    """Call Gemini. Raises on any failure or empty response — caller handles it.

    Returns (text, actually_grounded). actually_grounded is read from the
    response's own grounding_metadata, not assumed from use_search — Gemini
    can be handed the google_search tool and still choose not to invoke it,
    and the previous version reported grounded=True purely because grounding
    was requested, which is exactly the kind of inaccurate "honest
    reporting of whether an answer was actually grounded" this project's
    own stated goals call out.

    AFC (automatic function calling) note: generate_content used to log
    "Direct use of automatic function calling (AFC) ... is not recommended"
    on every grounded call. Verified against the installed google-genai
    2.20.0 source (google/genai/models.py + _extra_utils.py) rather than
    guessing: AFC defaults to ON whenever `config.tools` is non-empty and
    none of those tools carry `function_declarations` — which describes
    exactly our google_search grounding tool, since it's a server-side
    built-in, not a client-executed Python function. The warning is Google
    recommending Chat.send_message for cases that actually use AFC's
    client-side auto-execution loop, which we don't — we never pass a
    function_declarations tool to Gemini at all (custom tool-calling only
    exists on the OpenAI-compatible fallback tiers, a completely separate
    code path). So this is not "migrate to Chat.send_message" — it's
    telling the SDK to skip a loop we were never using in the first place.
    Explicitly disabling automatic_function_calling does that, verified
    against _extra_utils.should_disable_afc's actual logic: setting
    disable=True routes generate_content straight to a single plain
    request, bypassing the AFC wrapper (and its warning) entirely, with no
    change to what the call actually does.
    """
    transcript = _history_to_transcript(history or [])
    full_prompt = f"{system_prompt}\n\n{transcript}User: {prompt}" if system_prompt else f"{transcript}User: {prompt}"

    def _call(thinking):
        return _gemini_client.models.generate_content(
            model=model,
            contents=full_prompt,
            config=_gemini_config(use_search, thinking),
        )

    response = _call_with_thinking(model, _call)

    text = response.text
    if not text:
        # Grounded responses occasionally come back as tool-call parts with
        # no plain-text field populated. Try to salvage text from the raw
        # candidate parts before giving up and letting the caller fall
        # through to the next tier.
        try:
            parts = response.candidates[0].content.parts
            text = "".join(getattr(p, "text", "") for p in parts).strip()
        except (AttributeError, IndexError):
            text = ""

    if not text:
        raise ValueError(f"{model} returned an empty response.")

    actually_grounded = False
    if use_search:
        try:
            grounding_metadata = response.candidates[0].grounding_metadata
            actually_grounded = bool(
                grounding_metadata and (
                    getattr(grounding_metadata, "web_search_queries", None)
                    or getattr(grounding_metadata, "grounding_chunks", None)
                )
            )
        except (AttributeError, IndexError):
            actually_grounded = False

    return text, actually_grounded

    return text


def _classify_http_status(status_code: int) -> str:
    """Short, human-meaningful category for an HTTP failure — used so logs
    say WHY a tier was skipped instead of just "failed", and so the
    tool-calling loop knows which failures are worth a no-tools retry
    (a schema rejection) versus which aren't (billing/auth/rate-limit will
    fail identically with or without a tools field, so retrying just wastes
    a call and produces a misleading "rejected the tools param" log line
    for what was actually a 402). Deliberately a single flat function, not
    an exception-class hierarchy — the buckets below cover what's actually
    been observed in practice, and a bigger taxonomy would be solving a
    problem that doesn't exist yet."""
    if status_code in (401, 403):
        return "auth_error"
    if status_code == 402:
        return "billing_unavailable"
    if status_code == 404:
        return "model_not_found"
    if status_code == 429:
        return "rate_limited"
    if status_code == 400:
        return "bad_request"  # plausibly a schema issue (e.g. unrecognized "tools" field) — worth a no-tools retry
    if 500 <= status_code < 600:
        return "provider_error"
    return "http_error"


# Categories where retrying the same call without the `tools` field is
# actually worth trying — i.e. the failure could plausibly BE the tools
# field. Everything else (billing, auth, rate limits, a bad model name,
# network drops) will fail exactly the same way with or without tools, so
# retrying just burns an extra round-trip and mislabels the real cause.
_TOOLS_RETRY_WORTHY_CATEGORIES = ("bad_request", "http_error")


def _post_chat_completion(tier: dict, api_key: str, messages: list, use_tools: bool) -> dict:
    """One HTTP round-trip to a tier's /chat/completions endpoint. Raised
    exceptions are caught by the caller, which retries without tools once
    IF the failure category suggests that's plausibly the cause — some
    OpenAI-compatible endpoints reject an unrecognized `tools` field
    outright rather than ignoring it, and that shouldn't take down an
    otherwise-working tier. A 402 or 429 isn't that case (see
    _TOOLS_RETRY_WORTHY_CATEGORIES) and shouldn't trigger a pointless
    identical retry.

    Every raised message is prefixed with a short category tag
    ([billing_unavailable], [rate_limited], [network_error], etc.) so
    generate()'s "tier failed" log line says why, not just that it did —
    this is what let Cerebras's 402 show up as an actual billing signal
    instead of an opaque "Cerebras failed" line."""
    payload = {"model": tier["model"], "messages": messages}
    if use_tools:
        payload["tools"] = [WEB_SEARCH_TOOL_SCHEMA]
        payload["tool_choice"] = "auto"

    try:
        response = requests.post(
            f"{tier['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        raise ValueError(f"[network_error] {tier['name']}: {e}") from e

    if not response.ok:
        # raise_for_status() alone discards the response body, which is
        # exactly where providers put the actually useful error (e.g.
        # "model_decommissioned") — that gap is what made a stale model ID
        # show up as a bare, unhelpful "404 Client Error" in practice.
        category = _classify_http_status(response.status_code)
        raise ValueError(f"[{category}] {tier['name']} HTTP {response.status_code}: {response.text[:300]}")
    return response.json()


def _generate_openai_compatible(
    prompt: str,
    system_prompt: str,
    tier: dict,
    history: "list | None" = None,
    use_search: bool = False,
) -> "tuple[str, bool]":
    """Call any OpenAI-/chat-completions-compatible endpoint (Cerebras, Groq,
    NVIDIA NIM, Mistral all qualify). Raises on any failure or empty final
    response — caller handles it, same contract as _generate_cloud.

    Returns (text, searched) — searched is True only when a tool call
    actually returned usable evidence (see the None/empty/results split
    below), not merely when a tool call was attempted. That distinction is
    the fix for a real inaccuracy: the previous version set searched=True
    the instant the model called the tool at all, even if the search
    backend had nothing to give it — so a Groq answer built entirely on
    "search unavailable" was still reported as grounded.

    Real root cause found by inspection of an actual failure log
    ("groq did not produce a final answer within 3 tool iterations"):
    the forced final call after the loop (use_tools=False) still carried
    the ORIGINAL system-prompt directive injected at the top of this
    function — "You MUST call the web_search tool before answering" — into
    a request where tools had just been removed. The model was being told
    to do something it could no longer do. That contradiction, not model
    quality, is what produced an empty/refused final answer. Fixed below
    by appending a corrective message before the forced call that
    explicitly says tool access has ended and to answer with whatever's
    already been gathered.

    Secondary fix: if web_search() returns None (not just an empty list —
    None specifically means Tavily isn't configured or the call itself
    failed, a state that will not change on retry), stop burning tool
    iterations. There's no reason to let the model try the same dead
    channel three times before giving up.
    """
    api_key = os.environ.get(tier["api_key_env"])

    effective_system = system_prompt or ""
    if use_search:
        effective_system += (
            "\n\nIMPORTANT: This question likely requires current information. "
            "You MUST call the web_search tool before answering — do not answer "
            "from your own training data for this one."
        )

    messages = []
    if effective_system:
        messages.append({"role": "system", "content": effective_system})
    for turn in (history or []):
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": prompt})

    searched = False
    use_tools = True
    seen_queries: list = []
    search_definitively_unavailable = False

    for iteration in range(MAX_TOOL_ITERATIONS):
        print(f"[llm_client] {tier['name']} tool-loop iteration {iteration + 1}/{MAX_TOOL_ITERATIONS} (tools_offered={use_tools})")
        try:
            data = _post_chat_completion(tier, api_key, messages, use_tools)
        except Exception as e:
            retry_worthy = use_tools and any(f"[{cat}]" in str(e) for cat in _TOOLS_RETRY_WORTHY_CATEGORIES)
            if retry_worthy:
                # Plausibly a schema rejection (unrecognized "tools" field)
                # rather than a real outage — worth one retry without tools
                # before giving up on this tier.
                print(f"[llm_client] {tier['name']} rejected the tools param ({e}); retrying without tool-calling.")
                use_tools = False
                try:
                    data = _post_chat_completion(tier, api_key, messages, use_tools)
                except Exception as e2:
                    raise ValueError(f"{tier['name']} failed with and without tools: {e2}") from e2
            else:
                # Billing, auth, rate-limit, missing model, or a network
                # drop — none of these are caused by the tools field, and
                # retrying identically would just waste a round-trip and
                # mislabel a 402 as a "tools rejected" issue. Fail straight
                # to the tier-level except in generate()'s fallback loop.
                raise

        try:
            choice = data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise ValueError(f"{tier['name']} returned an unexpected response shape: {e}")

        tool_calls = choice.get("tool_calls")
        if not tool_calls:
            text = (choice.get("content") or "").strip()
            print(f"[llm_client] {tier['name']} returned a final answer directly on iteration {iteration + 1} (no tool call). length={len(text)}")
            if not text:
                raise ValueError(f"{tier['name']} returned an empty response.")
            return text, searched

        # Model wants to search — append its own tool-call message, then
        # one "tool" role message per call with the result, then loop back
        # so it can use that evidence to actually answer.
        messages.append(choice)
        for call in tool_calls:
            try:
                args = json.loads(call["function"]["arguments"])
                query = args.get("query", prompt)
            except (KeyError, json.JSONDecodeError):
                query = prompt

            if query in seen_queries:
                print(f"[llm_client] {tier['name']} NOTE: repeated identical query {query!r} — model is re-asking the same thing.")
            seen_queries.append(query)

            results = web_search(query)
            print(f"[llm_client] {tier['name']} requested web_search(query={query!r}) -> "
                  f"{'UNAVAILABLE' if results is None else f'{len(results)} result(s)'}")

            if results is None:
                # Genuinely unavailable (no TAVILY_API_KEY, or the call
                # itself failed) — this will not change on retry within
                # the same turn, so tell the model plainly and stop it
                # from wasting the remaining iterations re-asking.
                evidence = (
                    "Search is currently unavailable (no API access, or the search request "
                    "failed) — there is no way to retrieve current information for this "
                    "question right now. Do not call web_search again this turn. Tell the "
                    "user plainly that you can't verify current information right now rather "
                    "than answering from memory."
                )
                search_definitively_unavailable = True
            elif not results:
                evidence = f"The search for {query!r} completed successfully but returned no relevant results."
            else:
                evidence = "\n".join(f"- {r['title']}: {r['content'][:300]} ({r['url']})" for r in results)
                searched = True  # only real, non-empty evidence counts as "grounded"

            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": evidence,
            })

        if search_definitively_unavailable:
            print(f"[llm_client] {tier['name']}: search backend is unavailable — forcing an early final answer instead of burning the remaining {MAX_TOOL_ITERATIONS - iteration - 1} iteration(s).")
            break

    # Either MAX_TOOL_ITERATIONS was exhausted while still calling tools, or
    # we broke out early because search was definitively unavailable.
    # Force a final answer — but first strip the contradiction that caused
    # the original bug: the system message still says "you MUST call
    # web_search", and this call has no tools. Append an explicit
    # correction rather than editing messages[0] in place, so the model
    # sees a clear, final instruction rather than a silently-altered
    # earlier message.
    messages.append({
        "role": "system",
        "content": (
            "Tool access has ended for this turn — you no longer have web_search "
            "available. Answer the user's original question now using only what's "
            "already been gathered above. If that's insufficient or search was "
            "unavailable, say so plainly instead of guessing or asking to search again."
        ),
    })
    try:
        data = _post_chat_completion(tier, api_key, messages, use_tools=False)
        text = (data["choices"][0]["message"].get("content") or "").strip()
        print(f"[llm_client] {tier['name']} forced final answer after tool loop: length={len(text)}")
    except Exception as e:
        print(f"[llm_client] {tier['name']} forced final call itself failed: {e}")
        text = ""
    if not text:
        raise ValueError(f"{tier['name']} did not produce a final answer within {MAX_TOOL_ITERATIONS} tool iterations.")
    return text, searched


def _generate_local(prompt: str, system_prompt: str, history: "list | None" = None) -> str:
    """Call the local Ollama server. Raises on failure (e.g. Ollama not running)."""
    transcript = _history_to_transcript(history or [])
    full_prompt = f"{system_prompt}\n\n{transcript}User: {prompt}" if system_prompt else f"{transcript}User: {prompt}"

    response = requests.post(
        OLLAMA_GENERATE_URL,
        json={
            "model": OLLAMA_MODEL,
            "prompt": full_prompt,
            "stream": False,
        },
        timeout=120,  # local generation is slow (theory: ~3-4 tok/s) — give it room
    )
    response.raise_for_status()
    return response.json()["response"]


# ---- Streaming generation -------------------------------------------------
# Added this session as a prerequisite for voice/interruption — before
# this, generate() was the ONLY way to get a reply: one blocking call,
# one complete blob back, nothing to interrupt and nothing for a TTS
# engine to start speaking before the whole thing exists. generate_stream()
# below yields incremental text deltas instead, and checks a caller-
# supplied cancel_check() between chunks — the actual mid-generation
# cancellation ember_conversation.py's request_cancel() docstring notes
# doesn't exist yet for the non-streaming path.
#
# Deliberately scoped OUT of this pass: tool-calling (search grounding) on
# the fallback tiers. Streaming a tool-call response requires buffering
# partial tool-call JSON across chunks, executing the tool, then
# resuming a SECOND stream — a materially different, harder protocol per
# provider, not just "the same loop but chunked." use_search=True on
# generate_stream() below is handled honestly, not silently ignored: it
# falls back to the existing non-streaming generate() (full tool-calling
# loop included) and republishes that single result as one chunk, so
# callers get a correct answer with search intent honored, just without
# incremental output for that one turn. Revisit once real usage shows
# streamed search grounding is actually needed, not assumed.
#
# Tier-selection mirrors generate()'s order and quota checks exactly —
# deliberately not reimplemented differently. The one new rule: once a
# tier has yielded at least one real chunk, generate_stream() does NOT
# fall back to a different tier on a later failure in that same tier —
# unlike the non-streaming path, the user has already SEEN partial output
# by then, and silently restarting from a different provider would either
# duplicate or contradict what's already on screen. A stream that dies
# midway ends with an explicit note instead.

@dataclass
class StreamChunk:
    text_delta: str = ""
    done: bool = False
    cancelled: bool = False
    source: "str | None" = None   # only set on the final chunk
    model: "str | None" = None    # only set on the final chunk
    error: "str | None" = None    # set if the stream ended on failure, partial or total
    recovered_from: "str | None" = None  # set on the final chunk when a mid-stream failure was healed by
                                          # continuing on another tier; holds the ORIGINAL error text (for logs)


def _stream_gemini(model: str, prompt: str, system_prompt: str, history: "list | None"):
    """Yields plain text deltas from Gemini's native streaming API. Raises
    before yielding anything if the stream can't even start (caller's
    tier-fallback loop treats that exactly like a non-streaming failure);
    once a chunk has been yielded, any further exception is caught by the
    caller as a mid-stream failure, not a reason to try the next tier."""
    transcript = _history_to_transcript(history or [])
    full_prompt = f"{system_prompt}\n\n{transcript}User: {prompt}" if system_prompt else f"{transcript}User: {prompt}"

    def _open(thinking):
        # The API validates the request when the first chunk is pulled, so pull it here — inside
        # the thinking ladder — and hand back an iterator that still includes it.
        stream = iter(_gemini_client.models.generate_content_stream(
            model=model, contents=full_prompt, config=_gemini_config(False, thinking)))
        for event in stream:
            text = getattr(event, "text", None)
            if text:
                return text, stream
        raise ValueError(f"{model} returned an empty stream.")

    first_text, stream = _call_with_thinking(model, _open)
    yield first_text
    for event in stream:
        text = getattr(event, "text", None)
        if text:
            yield text


def _stream_openai_compatible(tier: dict, prompt: str, system_prompt: str, history: "list | None"):
    """Yields plain text deltas from an OpenAI-compatible /chat/completions
    endpoint using stream=True (SSE). No tool-calling — see the module-
    level note above on why that's explicitly out of scope here."""
    api_key = os.environ.get(tier["api_key_env"])
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    for turn in (history or []):
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": prompt})

    response = requests.post(
        f"{tier['base_url']}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": tier["model"], "messages": messages, "stream": True},
        timeout=30,
        stream=True,
    )
    if not response.ok:
        category = _classify_http_status(response.status_code)
        raise ValueError(f"[{category}] {tier['name']} HTTP {response.status_code}: {response.text[:300]}")

    for line in response.iter_lines():
        if not line:
            continue
        line = line.decode("utf-8") if isinstance(line, bytes) else line
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
            delta = chunk["choices"][0]["delta"].get("content")
        except (json.JSONDecodeError, KeyError, IndexError):
            continue
        if delta:
            yield delta


def _stream_local(prompt: str, system_prompt: str, history: "list | None"):
    """Yields plain text deltas from Ollama's native streaming API
    (newline-delimited JSON, not SSE — a different wire format from the
    OpenAI-compatible tiers above, handled separately rather than forcing
    one parser to cover both)."""
    transcript = _history_to_transcript(history or [])
    full_prompt = f"{system_prompt}\n\n{transcript}User: {prompt}" if system_prompt else f"{transcript}User: {prompt}"
    response = requests.post(
        OLLAMA_GENERATE_URL,
        json={"model": OLLAMA_MODEL, "prompt": full_prompt, "stream": True},
        timeout=120,
        stream=True,
    )
    response.raise_for_status()
    for line in response.iter_lines():
        if not line:
            continue
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue
        delta = chunk.get("response")
        if delta:
            yield delta
        if chunk.get("done"):
            break



# ---- Mid-stream recovery ---------------------------------------------------
# Real failure that motivated this: Gemini returned 504 DEADLINE_EXCEEDED halfway
# through a streamed reply. The old rule ("never switch tiers once text has been
# shown") left the user with a truncated answer plus a raw error blob that was then
# saved into history, so the model itself started talking about "a server timeout".
#
# New rule: when a tier dies AFTER showing text, hand the next available tier the
# original request plus the partial answer and ask it to continue from exactly that
# point. The continuation is stitched on seamlessly (its first ~160 chars are held
# back briefly so a repeated tail can be trimmed off). Bounded: at most
# MAX_CONTINUATIONS recovery hops per reply, never back onto a tier that already
# failed this reply. Only if every remaining tier also fails does the caller get
# the original interruption error, as before.
#
# Known limit: a continuation is a different model picking up mid-thought, so
# tone can shift slightly at the seam, and a model that ignores the instruction and
# restarts from the top is only caught when it repeats the tail of the partial.
MAX_CONTINUATIONS = 2
_OVERLAP_HOLD_CHARS = 160
_MIN_OVERLAP = 5

CONTINUATION_SYSTEM_NOTE = (
    "You are resuming a reply that was cut off by a connection error. Output ONLY the "
    "remaining text. Never repeat text that was already written, never restart, and "
    "never mention the interruption or apologise."
)


def _continuation_prompt(original_prompt: str, partial: str) -> str:
    return (
        f"{original_prompt}\n\n"
        "[Your reply to the above was cut off. This is everything already shown to the user:]\n"
        f"<<<\n{partial}\n>>>\n"
        "Continue from exactly where it stops, starting with the very next word."
    )


def _overlap_len(tail: str, head: str) -> int:
    """How many leading chars of `head` duplicate the end of `tail` (0 if the
    overlap is too short to be trustworthy). Also tolerates the model having
    stripped leading whitespace; the return value is always in `head` chars."""
    for cand in (head, head.lstrip()):
        stripped = len(head) - len(cand)
        for k in range(min(len(tail), len(cand), 200), _MIN_OVERLAP - 1, -1):
            if tail.endswith(cand[:k]):
                return stripped + k
    return 0


class _OverlapTrimmer:
    """Holds the first ~_OVERLAP_HOLD_CHARS of a continuation, trims any repeat of
    what was already shown, restores a word boundary the model may have stripped,
    then passes everything after that straight through."""

    def __init__(self, already_shown: str):
        self.tail = already_shown[-200:]
        self.buf = ""
        self.released = False

    def feed(self, delta: str) -> str:
        if self.released:
            return delta
        self.buf += delta
        return self._release() if len(self.buf) >= _OVERLAP_HOLD_CHARS else ""

    def flush(self) -> str:
        return "" if self.released else self._release()

    def _release(self) -> str:
        self.released = True
        text, self.buf = self.buf, ""
        cut = _overlap_len(self.tail, text)
        if cut:
            return text[cut:]
        if self.tail and text and self.tail[-1].isalnum() and text[0].isalnum():
            return " " + text
        return text


def generate_stream(
    prompt: str,
    system_prompt: str = "",
    use_search: bool = False,
    history: "list | None" = None,
    cancel_check=None,
    _exclude=frozenset(),
    _continuations_left: int = MAX_CONTINUATIONS,
):
    """Streaming counterpart to generate() — yields StreamChunk objects
    instead of returning one GenerateResult. Same tier order and quota
    checks as generate(); see the module-level note above for the
    use_search=True fallback behavior and the "no mid-stream tier switch"
    rule.

    cancel_check: optional zero-arg callable (e.g.
    conversation.is_cancelled) checked between every yielded chunk. On a
    True, yields one final StreamChunk(done=True, cancelled=True) and
    stops — this IS real mid-generation cancellation, unlike the
    non-streaming path, because there's an actual loop here to check it
    inside of.
    """
    if use_search:
        result = generate(prompt, system_prompt=system_prompt, use_search=True, history=history)
        yield StreamChunk(text_delta=result.text, done=True, source=result.source, model=result.model)
        return

    t_start = time.perf_counter()
    history = history or []

    def _recover(partial_text: str, err_chunk):
        """Continue a reply on another tier after a mid-stream failure (see the
        "Mid-stream recovery" note above). Yields the stitched continuation, or the
        original error chunk if no other tier can take over."""
        import dataclasses
        print(f"[llm_client] {err_chunk.model} died mid-reply ({err_chunk.error}); continuing on another tier.")
        cont_system = (f"{system_prompt}\n\n" if system_prompt else "") + CONTINUATION_SYSTEM_NOTE
        trimmer = _OverlapTrimmer(partial_text)
        try:
            for c in generate_stream(
                _continuation_prompt(prompt, partial_text),
                system_prompt=cont_system, use_search=False, history=history, cancel_check=cancel_check,
                _exclude=frozenset(_exclude) | {err_chunk.model},
                _continuations_left=_continuations_left - 1,
            ):
                if c.text_delta:
                    out = trimmer.feed(c.text_delta)
                    if out:
                        yield StreamChunk(text_delta=out)
                    continue
                tail = trimmer.flush()
                if tail:
                    yield StreamChunk(text_delta=tail)
                if not c.error and not c.cancelled:
                    c = dataclasses.replace(c, recovered_from=err_chunk.error)
                    print(f"[llm_client] reply recovered: continued on {c.model}.")
                yield c
                return
        except Exception as e:
            print(f"[llm_client] recovery could not start on any other tier ({e}).")
        yield err_chunk  # nothing (more) could be done: surface the original interruption

    def _recovering(chunk_generator):
        partial = []
        for chunk in chunk_generator:
            if chunk.done and chunk.error and not chunk.cancelled and partial and _continuations_left > 0:
                yield from _recover("".join(partial), chunk)
                return
            if chunk.text_delta:
                partial.append(chunk.text_delta)
            yield chunk

    def _run_tier(chunk_generator, source: str, model: str):
        started = False
        try:
            for delta in chunk_generator:
                if cancel_check and cancel_check():
                    yield StreamChunk(done=True, cancelled=True, source=source, model=model)
                    return
                started = True
                yield StreamChunk(text_delta=delta)
            if not started:
                # Zero deltas ever produced — same "empty response" failure
                # generate() raises ValueError on for the non-streaming
                # path. Raised here (nothing has been yielded to the real
                # caller yet, since the for-loop body never ran) so the
                # tier-selection loop below sees this as "tier failed to
                # start" and correctly tries the next tier, rather than
                # silently treating an empty stream as a valid answer.
                raise ValueError(f"{model} returned an empty stream.")
            yield StreamChunk(done=True, source=source, model=model)
        except Exception as e:
            if started:
                # Already shown the user partial output from this tier —
                # per the module note, we do NOT silently jump to a
                # different tier mid-stream. Surface the interruption
                # honestly instead.
                yield StreamChunk(done=True, source=source, model=model, error=f"Stream interrupted: {e}")
            else:
                raise  # nothing shown yet — safe for the caller to try the next tier, same as generate()

    if cloud_available():
        for tier in CLOUD_TIERS:
            model = tier["model"]
            if not _quota.can_use(model) or model in _exclude:
                continue
            try:
                gen = _recovering(_run_tier(_stream_gemini(model, prompt, system_prompt, history), "cloud", model))
                first = next(gen)
                _quota.record(model)
                print(f"[llm_client] {model}: first token in {time.perf_counter() - t_start:.1f}s")
                yield first
                yield from gen
                return
            except StopIteration:
                continue
            except Exception as e:
                print(f"[llm_client] {model} streaming failed before any output ({e}); trying next option.")
                continue

    for tier in FALLBACK_TIERS:
        if not _tier_has_key(tier) or f"{tier['name']}:{tier['model']}" in _exclude:
            continue
        if tier["rpd"] is not None and not _quota.can_use(tier["model"]):
            continue
        try:
            model_tag = f"{tier['name']}:{tier['model']}"
            gen = _recovering(_run_tier(_stream_openai_compatible(tier, prompt, system_prompt, history), "fallback", model_tag))
            first = next(gen)
            print(f"[llm_client] {tier['name']}: first token in {time.perf_counter() - t_start:.1f}s")
            if tier["rpd"] is not None:
                _quota.record(tier["model"])
            yield first
            yield from gen
            return
        except StopIteration:
            continue
        except Exception as e:
            print(f"[llm_client] {tier['name']} streaming failed before any output ({e}); trying next option.")
            continue

    if OLLAMA_MODEL in _exclude:
        raise RuntimeError("Every other tier failed to stream, and local already failed this reply.")
    try:
        gen = _recovering(_run_tier(_stream_local(prompt, system_prompt, history), "local", OLLAMA_MODEL))
        first = next(gen)
        yield first
        yield from gen
    except StopIteration:
        raise RuntimeError("Every tier failed to stream — Gemini, all configured fallbacks, and local.")
    except Exception as e:
        raise RuntimeError(f"Every tier failed to stream — Gemini, all configured fallbacks, and local. Local error: {e}") from e