"""
ember_research.py
===================
Multi-step web research, per the next-phase brief's item #7: "search ->
read -> compare -> search again if necessary -> synthesize." The brief's
own hard requirement was already fixed in an earlier pass (never silently
swap a real search requirement for an ungrounded guess); this module adds
the "search again if necessary" step that was still missing — Ember
previously always did exactly one search round, full stop.

Deliberately bounded, not an open-ended agent loop: at most
MAX_ROUNDS (2) search calls per turn. "Insufficient evidence" is decided
by a plain COUNT/diversity check — how many results came back, from how
many distinct domains — not an LLM judgment call. That's a conscious
choice, not a shortcut: asking an LLM "was that enough evidence?" costs a
full generation call and adds latency to every single search turn, for a
question a simple heuristic answers almost as well (thin/single-domain
results are a reasonable, cheap proxy for "this needs a second look").
Same "heuristic, not classifier, first guess not a tuned constant"
caveat as every other threshold in this project.

Round 2's reformulation is a plain heuristic, not an LLM call either:
strip a leading question word/phrase ("what is", "who is", "how does",
...) to get closer to the bare subject, on the theory that a
question-shaped query sometimes searches worse than a keyword-shaped one.
This will have gaps — it's a first pass at "search again differently,"
not a general query-rewriting system.
"""

import re
from dataclasses import dataclass, field

MAX_ROUNDS = 2
MIN_RESULTS_SUFFICIENT = 2
MIN_DOMAINS_SUFFICIENT = 2
MAX_EVIDENCE_RETURNED = 8

_LEADING_QUESTION_RE = re.compile(
    r"^\s*(?:what|who|when|where|why|how|is|are|does|do|did|can|could|will|would)\b\s*(?:is|are|does|do|did)?\s*",
    re.IGNORECASE,
)


@dataclass
class ResearchResult:
    evidence: list = field(default_factory=list)   # list of {"title","url","content"}
    rounds_used: int = 0
    queries_tried: list = field(default_factory=list)
    relevant: bool = True  # False if evidence was retrieved but none of it appears
                            # topically related to the query at all (see
                            # _has_topical_overlap) -- True when evidence is empty,
                            # since "no evidence" is already a distinct, separately
                            # handled case for callers; this flag only matters when
                            # `evidence` is non-empty but was worth double-checking.


def _domain(url: str) -> str:
    m = re.search(r"https?://([^/]+)/?", url or "")
    return m.group(1) if m else url or ""


_RELEVANCE_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "of", "in", "on", "for",
    "and", "or", "to", "that", "this", "it", "what", "who", "when", "where",
    "how", "which", "will", "do", "does", "did", "please", "recheck",
    "source", "sir", "verify", "check", "again", "thats", "you", "your",
    # Added after a reproduced failure: a verify query reduced to just
    # "and who won it?" (a bare pronoun-heavy follow-up) had exactly one
    # significant word left — "won" — which trivially appears in almost
    # any competitive-result article regardless of sport or topic. That
    # let genuinely off-topic evidence (football results, in the observed
    # case) pass the overlap gate. "won"/"win"/"winner" carry essentially
    # no topical signal on their own, the same reasoning that already
    # stopworded "check"/"verify"/"recheck" (about the ACT of asking, not
    # the subject) — they're about the shape of a result, not its topic.
    "won", "win", "wins", "winner", "winners", "winning",
}


def _significant_words(text: str) -> set:
    words = re.findall(r"[a-zA-Z0-9']+", (text or "").lower())
    return {w for w in words if w not in _RELEVANCE_STOPWORDS and len(w) > 2}


_MIN_OVERLAP_WORDS = 2  # a single shared generic word ("team", "racing") isn't
                         # enough signal on its own -- found via testing: the exact
                         # off-topic "vintage apparel" evidence from the reported
                         # failure still shared the single word "team" with a
                         # Porsche-team query, which would have wrongly counted as
                         # relevant under a bare any-word-overlap check.


def _has_topical_overlap(query: str, evidence: list) -> bool:
    """Cheap keyword-overlap relevance gate, deliberately separate from
    _looks_sufficient's count/domain-diversity check above — those two
    questions are genuinely different. Real failure that motivated this:
    a Tavily search for a Porsche WEC team lineup came back with 5
    results, from several distinct domains, entirely about 'vintage
    apparel, memorabilia, and book reviews' — which counted as
    'sufficient' by count/diversity alone and got handed to the model as
    real evidence to reason over. The model correctly noticed the
    evidence didn't address the question and fell back to reasserting
    its own prior (unverified, and in that case wrong) claim — which is
    a worse outcome than just admitting no usable evidence was found.

    Deliberately blunt keyword overlap, not semantic similarity — same
    heuristic-not-classifier caveat as every other threshold in this
    file. Requires at least _MIN_OVERLAP_WORDS DISTINCT significant query
    words to appear (word-boundary matched, not bare substring — matters
    for short tokens like "911") anywhere across the combined evidence
    text, not just any single word in any single result — one
    coincidental shared word (e.g. "team") between an unrelated result
    and the query is not enough signal on its own, which a bare
    any-overlap check would have missed. Short queries (fewer than
    _MIN_OVERLAP_WORDS significant words to begin with) fall back to
    requiring all of them, not more than exist."""
    query_words = _significant_words(query)
    if not query_words:
        return True  # nothing meaningful to check against -- don't block on an empty signal
    combined_blob = " ".join(f"{item.get('title', '')} {item.get('content', '')}" for item in evidence).lower()
    overlap = {w for w in query_words if re.search(rf"\b{re.escape(w)}\b", combined_blob)}
    threshold = min(_MIN_OVERLAP_WORDS, len(query_words))
    return len(overlap) >= threshold


def _looks_sufficient(evidence: list, query: str) -> bool:
    if len(evidence) < MIN_RESULTS_SUFFICIENT:
        return False
    domains = {_domain(r.get("url", "")) for r in evidence}
    if len(domains) < MIN_DOMAINS_SUFFICIENT:
        return False
    return _has_topical_overlap(query, evidence)


def _reformulate(query: str) -> str:
    stripped = _LEADING_QUESTION_RE.sub("", query).strip(" ?")
    return stripped if stripped and stripped.lower() != query.lower() else query + " latest"


def research(query: str, web_search_fn, max_rounds: int = MAX_ROUNDS, cancel_check=None) -> ResearchResult:
    """Runs up to `max_rounds` search calls, stopping as soon as evidence
    looks sufficient. `web_search_fn` is injected (llm_client.web_search)
    so this stays testable without a real Tavily call, same pattern as
    every other injected-callable module in this project.

    cancel_check: optional zero-arg callable (e.g. conversation.is_cancelled),
    checked at the top of every round before that round's web_search_fn call
    fires -- lets a mid-turn cancellation stop a second Tavily call from
    firing between round 1 and round 2, and skips round 1 itself if the
    turn was already cancelled before research() was even entered. Defaults
    to None (never checked) so every caller that doesn't pass this keyword
    continues to work unchanged.

    Sufficiency is checked against the ORIGINAL `query`, not whatever
    `current_query` a later round happens to be using -- reformulation is a
    different way of searching for the same underlying information need,
    not a different need, so relevance is always judged against what the
    person actually asked."""
    all_evidence: list = []
    seen_urls = set()
    queries_tried = []
    current_query = query
    rounds_used = 0

    for round_num in range(max_rounds):
        if cancel_check and cancel_check():
            break
        results = web_search_fn(current_query)
        rounds_used += 1
        queries_tried.append(current_query)

        if results:
            new_results = [r for r in results if r.get("url") not in seen_urls]
            for r in new_results:
                seen_urls.add(r.get("url"))
            all_evidence.extend(new_results)

        if _looks_sufficient(all_evidence, query):
            break
        if round_num + 1 >= max_rounds:
            break
        if cancel_check and cancel_check():
            break
        current_query = _reformulate(current_query)

    final_evidence = all_evidence[:MAX_EVIDENCE_RETURNED]
    return ResearchResult(
        evidence=final_evidence,
        rounds_used=rounds_used,
        queries_tried=queries_tried,
        relevant=_has_topical_overlap(query, final_evidence) if final_evidence else True,
    )
