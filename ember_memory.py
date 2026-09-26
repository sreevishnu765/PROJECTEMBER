"""
Ember Memory — Stage 2 persistent memory / RAG module.

Lineage: rebuilt from a review of a friend's JARVIS-style assistant
("Nexus VII"), which used a flat-JSON embedding store. That approach had
four concrete reliability bugs, all fixed here:

  1. Whole-file JSON rewrite on every write -> crash mid-write corrupts
     the entire memory store. Fixed: SQLite in WAL mode, one row per
     memory, atomic per-row commits.
  2. No thread-safety around the in-memory store, despite the caller
     running on background threads. Fixed: a single write lock around
     every mutating operation.
  3. Silent truncation of long text before embedding (no warning, no
     record that it happened). Fixed: truncation is recorded on the row
     (`partial_embedding`) and surfaced back to the caller.
  4. Deletion by list index, which shifts under concurrent deletes.
     Fixed: stable integer primary key.

Deliberately deferred (log, don't silently skip — per Ember's own
standard):
  - Semantic (embedding-similarity) de-duplication. Only exact-text
    de-dup is implemented for now; near-duplicate phrasing will still
    be stored twice. Revisit once memory volume is large enough for it
    to matter in practice.
  - Recency decay is a simple linear half-life, not tuned. Treat the
    DECAY_HALF_LIFE_DAYS constant as a first guess, not a final answer.
  - No ANN index. Cosine similarity is a linear scan over all rows.
    Fine into the low thousands of memories on this hardware; revisit
    if that ceiling is ever approached.

Design note: this module does NOT do its own API-key rotation or
provider fallback. JARVIS's version re-implemented backup-key iteration
independently in three separate places (vector_memory._embed, the image
upload handler, the proactive engine), which is exactly the kind of
duplicated, drifting logic Ember's llm_client.py tiered fallback exists
to prevent. Instead, this module takes an `embed_fn` callable injected
by the caller — Ember's own llm_client should own quota tracking and
provider fallback for embeddings the same way it already does for chat
completions. One source of truth for "how do we call the model," not
two.

Local embedding fallback (added [this session]): llm_client.embed() now
tries a local model (fastembed) before cloud, and returns
(vector, source_tag) rather than a bare vector — source_tag identifies
which model/dimensionality produced it (e.g. "local:BAAI/bge-small-en-v1.5"
vs "cloud:gemini-embedding-001"). This module stores that tag per-row and
NEVER cosine-compares vectors whose source_tag differs from the current
query's — two different embedding models' vector spaces are not
comparable, even when the raw dimension happens to match by coincidence.
A memory embedded under one source before a provider switch is not lost —
its text is still there for keyword recall, and it's still a semantic
match candidate again the moment a query comes in on that same source
(e.g. cloud briefly unavailable, local currently down) — it's just
correctly excluded from cross-space scoring rather than silently
mismeasured. No backfill/re-embedding migration is implemented here;
mixed-source rows accumulating over time is an explicit, logged
possibility (see `stats()`), not a silent one, and worth revisiting only
if it actually causes noticeable recall gaps in practice.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

DECAY_HALF_LIFE_DAYS = 30.0
SIMILARITY_FLOOR = 0.3
EMBED_CHAR_LIMIT = 2048

# embed_fn now returns (vector, source_tag) — see the local-embedding note
# above. source_tag is what lets recall() avoid comparing vectors from
# different embedding models/dimensions.
EmbedFn = Callable[[str], Optional["tuple[list[float], str]"]]


@dataclass
class RecallResult:
    text: str
    degraded: bool          # True if we fell back to keyword or raw-dump recall
    reason: str             # human-readable explanation, for transparency caveats
    sources_used: int       # how many memory rows contributed


class EmberMemory:
    """SQLite-backed semantic memory store with explicit, logged degradation."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._init_schema()

    def _init_schema(self):
        with self._lock, self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    embedding TEXT,              -- JSON list[float], NULL if embedding failed
                    embed_source TEXT,           -- which model produced `embedding`, e.g. "local:BAAI/bge-small-en-v1.5" or "cloud:gemini-embedding-001"; NULL alongside a NULL embedding
                    partial_embedding INTEGER NOT NULL DEFAULT 0,
                    source TEXT,                 -- e.g. "conversation", "manual", "import"
                    memory_type TEXT NOT NULL DEFAULT 'semantic',  -- 'episodic' (something that happened), 'semantic' (a fact), 'preference' (how the user wants Ember to behave)
                    project TEXT,                 -- free-text project tag ("Ember", "Alpha", ...), NULL if unscoped/general
                    importance REAL NOT NULL DEFAULT 0.5,  -- 0-1, caller-supplied; used only as a recall tiebreaker for now, see recall()
                    confidence REAL NOT NULL DEFAULT 1.0,  -- 0-1; lower for inferred/heuristic facts, 1.0 for explicit "remember that" commands
                    created_at REAL NOT NULL,
                    last_accessed_at REAL,
                    access_count INTEGER NOT NULL DEFAULT 0
                )
            """)
            # Backward-compat migration: a DB created before these columns
            # existed won't have them. ALTER TABLE ADD COLUMN is the
            # standard SQLite way to add columns in place without a
            # rebuild; existing rows get sensible defaults on the new
            # columns rather than NULL-induced query breakage.
            existing_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(memories)")}
            migrations = [
                ("embed_source", "ALTER TABLE memories ADD COLUMN embed_source TEXT"),
                ("memory_type", "ALTER TABLE memories ADD COLUMN memory_type TEXT NOT NULL DEFAULT 'semantic'"),
                ("project", "ALTER TABLE memories ADD COLUMN project TEXT"),
                ("importance", "ALTER TABLE memories ADD COLUMN importance REAL NOT NULL DEFAULT 0.5"),
                ("confidence", "ALTER TABLE memories ADD COLUMN confidence REAL NOT NULL DEFAULT 1.0"),
            ]
            for col, ddl in migrations:
                if col not in existing_cols:
                    self._conn.execute(ddl)

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def remember(
        self,
        text: str,
        embed_fn: EmbedFn,
        source: str = "conversation",
        memory_type: str = "semantic",
        project: "str | None" = None,
        importance: float = 0.5,
        confidence: float = 1.0,
    ) -> dict:
        """Store a new memory. Returns a status dict — never raises for
        expected failure modes (embedding unavailable), only for
        genuinely unexpected ones.

        memory_type/project/importance/confidence are the Memory 2.0
        fields: memory_type distinguishes an event ("episodic" — yesterday
        we switched TTS to Isabella) from a standing fact ("semantic" —
        Project Alpha uses XGBoost Mk7) from a behavioral preference
        ("preference" — how the user wants Ember to act). project is a
        free-text tag for grouping ("Ember", "Alpha", ...), left NULL for
        general/unscoped facts. Callers decide these — this method doesn't
        try to infer them; ember_core.py's extract_memory_candidate() is
        where that heuristic classification happens, kept separate from
        storage mechanics the same way embedding and recall already are."""
        text = (text or "").strip()
        if len(text) < 5:
            return {"stored": False, "reason": "text too short"}

        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM memories WHERE lower(trim(text)) = ?",
                (text.lower(),),
            ).fetchone()
            if existing:
                return {"stored": False, "reason": "exact duplicate", "id": existing[0]}

        embed_input = text[:EMBED_CHAR_LIMIT]
        partial = len(text) > EMBED_CHAR_LIMIT
        embedding = None
        embed_source = None
        embed_error = None
        try:
            embed_result = embed_fn(embed_input)
            if embed_result is not None:
                embedding, embed_source = embed_result
        except Exception as e:
            embed_error = str(e)

        now = time.time()
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO memories
                   (text, embedding, embed_source, partial_embedding, source,
                    memory_type, project, importance, confidence,
                    created_at, last_accessed_at, access_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    text,
                    json.dumps(embedding) if embedding is not None else None,
                    embed_source,
                    int(partial),
                    source,
                    memory_type,
                    project,
                    importance,
                    confidence,
                    now,
                    now,
                ),
            )
            new_id = cur.lastrowid

        result = {"stored": True, "id": new_id, "embedded": embedding is not None}
        if partial:
            result["warning"] = f"text exceeded {EMBED_CHAR_LIMIT} chars; only the prefix was embedded (full text is still stored)"
        if embed_error:
            result["embed_error"] = embed_error
        return result

    def update(self, memory_id: int, new_text: str, embed_fn: EmbedFn, importance: "float | None" = None, confidence: "float | None" = None) -> dict:
        """Overwrites an existing memory's text (and re-embeds it) in
        place, keeping the same id — so access_count/last_accessed_at
        history isn't reset to zero and anything else that referenced
        this row by id still resolves. This is the "update" half of
        Memory 2.0's contradiction handling: a fact that changed
        ("internship starts in October" superseding "...in September")
        should replace the old row, not sit alongside it as a second,
        contradictory memory that recall() might return either of."""
        new_text = (new_text or "").strip()
        if len(new_text) < 5:
            return {"updated": False, "reason": "text too short"}

        embed_input = new_text[:EMBED_CHAR_LIMIT]
        partial = len(new_text) > EMBED_CHAR_LIMIT
        embedding = None
        embed_source = None
        embed_error = None
        try:
            embed_result = embed_fn(embed_input)
            if embed_result is not None:
                embedding, embed_source = embed_result
        except Exception as e:
            embed_error = str(e)

        with self._lock:
            existing = self._conn.execute("SELECT text, importance, confidence FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if not existing:
            return {"updated": False, "reason": "no memory with that id"}
        old_text, old_importance, old_confidence = existing

        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE memories SET text = ?, embedding = ?, embed_source = ?, partial_embedding = ?,
                   importance = ?, confidence = ?, created_at = ?, last_accessed_at = ?
                   WHERE id = ?""",
                (
                    new_text,
                    json.dumps(embedding) if embedding is not None else None,
                    embed_source,
                    int(partial),
                    importance if importance is not None else old_importance,
                    confidence if confidence is not None else old_confidence,
                    now, now, memory_id,
                ),
            )

        result = {"updated": True, "id": memory_id, "old_text": old_text, "new_text": new_text}
        if embed_error:
            result["embed_error"] = embed_error
        return result

    def find_best_match(self, query: str, embed_fn: EmbedFn, project: "str | None" = None, threshold: float = 0.80) -> "dict | None":
        """Returns the single most semantically similar existing memory
        to `query`, if its similarity clears `threshold`, else None.
        Deliberately separate from recall() (which returns several
        results for context-injection) — this is a much stricter,
        single-best-candidate lookup used to decide "is this new
        statement actually about the same thing as something we already
        know," which is what remember_or_update() and the UPDATE_MEMORY
        intent need, not a broad recall list.

        threshold=0.80 is a first guess, not a tuned constant — same
        honest caveat as every other similarity threshold in this project
        (SIMILARITY_FLOOR, DECAY_HALF_LIFE_DAYS). Too low risks
        overwriting an unrelated-but-topically-similar memory; too high
        risks never matching genuine restatements. Revisit once real
        usage shows which way it's wrong."""
        with self._lock:
            if project:
                rows = self._conn.execute(
                    "SELECT id, text, embedding, embed_source, memory_type, project FROM memories WHERE project = ? OR project IS NULL",
                    (project,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, text, embedding, embed_source, memory_type, project FROM memories"
                ).fetchall()
        if not rows:
            return None

        try:
            query_result = embed_fn(query)
        except Exception:
            query_result = None
        if query_result is None:
            return None  # no embedding available -> can't do a confident semantic match; caller falls back to "create new" rather than guessing
        query_embedding, query_source = query_result

        best = None
        for mem_id, text, emb_json, emb_source, memory_type, mem_project in rows:
            if not emb_json or emb_source != query_source:
                continue
            sim = self._cosine(query_embedding, json.loads(emb_json))
            if sim >= threshold and (best is None or sim > best["similarity"]):
                best = {"id": mem_id, "text": text, "similarity": sim, "memory_type": memory_type, "project": mem_project}
        return best

    def remember_or_update(
        self,
        text: str,
        embed_fn: EmbedFn,
        source: str = "conversation",
        memory_type: str = "semantic",
        project: "str | None" = None,
        importance: float = 0.5,
        confidence: float = 1.0,
        update_threshold: float = 0.80,
    ) -> dict:
        """The actual Memory 2.0 contradiction-avoidance primitive: looks
        for an existing memory close enough to `text` to plausibly be the
        SAME fact restated or corrected, and updates it in place instead
        of inserting a second, potentially-contradictory row. Falls back
        to a normal remember() (including its own exact-duplicate check)
        when no sufficiently similar memory exists, or when no embedding
        backend is available at all (find_best_match returns None in that
        case rather than guessing).

        Returns one of:
          {"action": "updated", "id", "old_text", "new_text"}
          {"action": "created", "id", ...remember()'s usual fields}
          {"action": "duplicate", "id"}  (remember()'s exact-dup path)
          {"action": "skipped", "reason"}
        """
        match = self.find_best_match(text, embed_fn, project=project, threshold=update_threshold)
        if match:
            result = self.update(match["id"], text, embed_fn, importance=importance, confidence=confidence)
            if result.get("updated"):
                return {"action": "updated", **result}
            return {"action": "skipped", "reason": result.get("reason", "update failed")}

        result = self.remember(text, embed_fn, source=source, memory_type=memory_type, project=project, importance=importance, confidence=confidence)
        if not result.get("stored"):
            if result.get("reason") == "exact duplicate":
                return {"action": "duplicate", "id": result.get("id")}
            return {"action": "skipped", "reason": result.get("reason", "not stored")}
        return {"action": "created", **result}

    def delete(self, memory_id: int) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return cur.rowcount > 0

    def forget(self, text_query: str, limit: int = 5) -> "list[dict]":
        """Explicit forget: deletes memories whose text contains
        `text_query` (case-insensitive substring match — deliberately
        simple and predictable, not semantic, so 'forget about the trip'
        doesn't accidentally sweep up unrelated rows a similarity score
        happened to like). Returns the list of deleted rows (id + text)
        so the caller can tell the user exactly what was removed rather
        than just a count — an explicit-forget command should be
        auditable, not a black box the same way clear_all() bulk-deletes
        blindly (that one is fine precisely because it's total and
        confirmed; a *selective* forget needs to show its work)."""
        q = f"%{text_query.strip().lower()}%"
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT id, text FROM memories WHERE lower(text) LIKE ? LIMIT ?",
                (q, limit),
            ).fetchall()
            if rows:
                ids = [r[0] for r in rows]
                self._conn.executemany("DELETE FROM memories WHERE id = ?", [(i,) for i in ids])
        return [{"id": r[0], "text": r[1]} for r in rows]

    def clear_all(self) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM memories")
            return cur.rowcount

    def reembed_stale(self, embed_fn: EmbedFn, batch_size: int = 50) -> dict:
        """Migration utility: re-embeds every row whose embed_source does
        NOT match what `embed_fn` currently produces — i.e. rows left
        behind by a provider switch (cloud -> local, or vice versa after
        an outage). This is the fix for the gap flagged in the previous
        pass: those rows were correctly excluded from cross-space
        comparison (never silently mismeasured), but they'd stay
        keyword-only forever with no path back to semantic recall. This
        gives them one.

        Deliberately NOT run automatically on every recall — that would
        turn one query into an unbounded batch-embedding job. Call this
        explicitly (e.g. a one-off maintenance command, or on startup if
        you want it scheduled) and it processes up to `batch_size` stale
        rows per call, so a large backlog degrades to 'takes a few calls
        to fully catch up' rather than 'blocks for a long time.'

        Returns {"checked": int, "reembedded": int, "still_stale": int,
        "current_source": str|None}. current_source is None (nothing to
        do) if embed_fn itself returns None on a probe call — i.e. no
        embedding backend is available at all right now."""
        probe = None
        try:
            probe = embed_fn("probe")
        except Exception:
            probe = None
        if probe is None:
            return {"checked": 0, "reembedded": 0, "still_stale": 0, "current_source": None}
        _, current_source = probe

        with self._lock:
            stale_rows = self._conn.execute(
                "SELECT id, text FROM memories WHERE embedding IS NOT NULL AND "
                "(embed_source IS NULL OR embed_source != ?) LIMIT ?",
                (current_source, batch_size),
            ).fetchall()
            total_stale = self._conn.execute(
                "SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL AND "
                "(embed_source IS NULL OR embed_source != ?)",
                (current_source,),
            ).fetchone()[0]

        reembedded = 0
        for mem_id, text in stale_rows:
            try:
                result = embed_fn(text[:EMBED_CHAR_LIMIT])
            except Exception:
                result = None
            if result is None:
                continue
            new_embedding, new_source = result
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE memories SET embedding = ?, embed_source = ? WHERE id = ?",
                    (json.dumps(new_embedding), new_source, mem_id),
                )
            reembedded += 1

        return {
            "checked": len(stale_rows),
            "reembedded": reembedded,
            "still_stale": max(0, total_stale - reembedded),
            "current_source": current_source,
        }

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def list_recent(self, n: int = 50, project: "str | None" = None, memory_type: "str | None" = None) -> "list[dict]":
        """Plain browse, newest first — no query, no embedding call, no
        similarity scoring. This is the missing piece a real Memory panel
        needs: recall() always requires a query string and does
        similarity search, which is the wrong tool for "just show me
        what's in there." Returns structured dicts (not RecallResult's
        flattened bullet text) since a UI list needs per-row id/type/
        project to render forget/edit affordances, not a single blob of
        prose meant for an LLM prompt."""
        clauses = []
        params: list = []
        if project:
            clauses.append("(project = ? OR project IS NULL)")
            params.append(project)
        if memory_type:
            clauses.append("memory_type = ?")
            params.append(memory_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._lock:
            rows = self._conn.execute(
                f"""SELECT id, text, embedding, memory_type, project, importance,
                           confidence, created_at, last_accessed_at, access_count
                    FROM memories {where}
                    ORDER BY created_at DESC LIMIT ?""",
                (*params, n),
            ).fetchall()

        return [
            {
                "id": r[0],
                "text": r[1],
                "embedded": r[2] is not None,
                "memory_type": r[3],
                "project": r[4],
                "importance": r[5],
                "confidence": r[6],
                "created_at": r[7],
                "last_accessed_at": r[8],
                "access_count": r[9],
            }
            for r in rows
        ]

    def recall(self, query: str, embed_fn: EmbedFn, n: int = 8, project: "str | None" = None) -> RecallResult:
        """project, if given, restricts recall to memories tagged with
        that project (or untagged/general ones) — e.g. so a question
        while working on "Alpha" doesn't surface unrelated facts filed
        under "Beta". Left None (the default) searches everything, same
        as before this parameter existed."""
        with self._lock:
            if project:
                rows = self._conn.execute(
                    "SELECT id, text, embedding, embed_source, created_at, importance FROM memories "
                    "WHERE project = ? OR project IS NULL",
                    (project,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, text, embedding, embed_source, created_at, importance FROM memories"
                ).fetchall()

        if not rows:
            return RecallResult(text="", degraded=False, reason="no memories stored", sources_used=0)

        query_embed_result = None
        try:
            query_embed_result = embed_fn(query)
        except Exception:
            pass

        if query_embed_result is None:
            return self._keyword_recall(query, rows, n, reason="embedding call failed; used keyword overlap instead")

        query_embedding, query_source = query_embed_result

        now = time.time()
        scored = []
        cross_source_skipped = 0
        for mem_id, text, emb_json, emb_source, created_at, importance in rows:
            if not emb_json:
                continue
            if emb_source != query_source:
                # Different embedding model (e.g. this row was embedded
                # locally but the query just got embedded via cloud, or
                # vice versa after a provider switch) — the vector spaces
                # aren't comparable, full stop, regardless of whether the
                # raw dimension happens to match. Skip it for semantic
                # scoring; its text is still eligible for keyword fallback
                # below if nothing else scores.
                cross_source_skipped += 1
                continue
            embedding = json.loads(emb_json)
            sim = self._cosine(query_embedding, embedding)
            age_days = max(0.0, (now - created_at) / 86400.0)
            recency_weight = 0.5 ** (age_days / DECAY_HALF_LIFE_DAYS)
            importance_weight = 0.9 + 0.2 * (importance if importance is not None else 0.5)
            # Recency and importance nudge ranking without letting an
            # old-but-highly-relevant or low-importance-but-relevant memory
            # get buried or promoted outright — both are tiebreakers on top
            # of similarity, not a veto or an override of it.
            combined = sim * (0.85 + 0.15 * recency_weight) * importance_weight
            scored.append((combined, sim, mem_id, text))

        if not scored:
            reason = "no rows had usable embeddings; used keyword overlap instead"
            if cross_source_skipped:
                reason = (
                    f"{cross_source_skipped} stored memory(ies) were embedded with a different "
                    f"model ({query_source} is current); used keyword overlap instead"
                )
            return self._keyword_recall(query, rows, n, reason=reason)

        scored.sort(key=lambda x: x[0], reverse=True)
        relevant = [(mem_id, text) for combined, sim, mem_id, text in scored[:n] if sim > SIMILARITY_FLOOR]

        if not relevant:
            return self._keyword_recall(query, rows, n, reason=f"no memory cleared the {SIMILARITY_FLOOR} similarity floor; used keyword overlap instead")

        self._touch(mid for mid, _ in relevant)
        return RecallResult(
            # Was "\n---\n".join(...) — a plain-text divider that's fine
            # in the CLI but a real, reproduced bug once a Markdown
            # renderer is in the picture: a "---" line directly after a
            # line of text is a CommonMark setext heading underline, so
            # the frontend was silently turning stored memory lines into
            # oversized bold headings instead of a visible divider. A
            # "- " bullet prefix has no such special meaning immediately
            # after text, reads fine in the CLI, and renders as an actual
            # clean bullet list wherever Markdown IS rendered.
            text="\n".join(f"- {t}" for _, t in relevant),
            degraded=False,
            reason="semantic recall",
            sources_used=len(relevant),
        )

    def _keyword_recall(self, query: str, rows, n: int, reason: str) -> RecallResult:
        q_words = set(query.lower().split())
        scored = []
        for row in rows:
            mem_id, text = row[0], row[1]
            overlap = len(q_words & set(text.lower().split()))
            if overlap:
                scored.append((overlap, mem_id, text))
        if not scored:
            return RecallResult(text="", degraded=True, reason=reason + "; no keyword matches either", sources_used=0)
        scored.sort(reverse=True)
        top = scored[:n]
        self._touch(mid for _, mid, _ in top)
        return RecallResult(
            text="\n".join(f"- {t}" for _, _, t in top),  # see recall()'s comment above — same Markdown-divider fix
            degraded=True,
            reason=reason,
            sources_used=len(top),
        )

    def _touch(self, ids):
        ids = list(ids)
        if not ids:
            return
        now = time.time()
        with self._lock, self._conn:
            self._conn.executemany(
                "UPDATE memories SET last_accessed_at = ?, access_count = access_count + 1 WHERE id = ?",
                [(now, i) for i in ids],
            )

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def stats(self) -> dict:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            embedded = self._conn.execute("SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL").fetchone()[0]
            partial = self._conn.execute("SELECT COUNT(*) FROM memories WHERE partial_embedding = 1").fetchone()[0]
            by_source_rows = self._conn.execute(
                "SELECT embed_source, COUNT(*) FROM memories WHERE embedding IS NOT NULL GROUP BY embed_source"
            ).fetchall()
        by_source = {src or "(unknown/pre-migration)": count for src, count in by_source_rows}
        result = {
            "total": total,
            "embedded": embedded,
            "without_embedding": total - embedded,
            "partial_embedding": partial,
            "embedded_by_source": by_source,
        }
        # Surfaced, not hidden: if memories are split across more than one
        # embedding source, some fraction of semantic recall queries will
        # only ever see whichever source is currently active — worth
        # knowing about even though no automatic fix is implemented.
        if len(by_source) > 1:
            result["note"] = (
                "Memories exist under more than one embedding source — recall() only "
                "scores rows matching the CURRENT source per query, so rows under other "
                "sources fall back to keyword matching until re-embedded (no automatic "
                "re-embedding migration exists yet)."
            )
        return result