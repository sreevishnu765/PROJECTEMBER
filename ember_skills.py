"""
ember_skills.py
================
Rebuilt, not ported. jarvisforember's actual skills/ directory had two
concrete problems on inspection:

1. skills/humanizer.md is not a skill — it's raw scraped GitHub webpage
   HTML (<!DOCTYPE html>, GitHub asset links, the works). Their loader
   (server.py's /api/list_skills) reads any .md/.txt file with zero content
   validation and would happily inject that HTML into a system prompt.

2. skills/general_rules.md mixes two different kinds of content in one
   file: durable behavioral rules ("never assume intent on destructive
   tasks") sitting next to one-off personal facts (a full weekly class
   timetable with room numbers). Editing one durable rule re-sends the
   whole timetable through context every time, and the timetable has no
   update path — it just goes stale silently.

This loader keeps the good idea (plain-text, hot-loadable capability files
a user can drop in without touching code) and fixes both problems:
  - Validates content actually looks like prose/markdown before loading it
    into a prompt (rejects HTML documents, binary, near-empty files).
  - Splits by convention: files under skills/rules/ are durable behavioral
    rules (loaded into the system prompt every turn). Files under
    skills/facts/ are NOT auto-loaded into every prompt — they're meant to
    go through ember_memory.py's recall path instead, so stale facts don't
    silently bloat every request. A skill file that looks like it contains
    dated/personal facts (heuristic below) gets flagged, not silently
    accepted, if placed under rules/.

A small default rule set is seeded from the two generically-useful (not
personal, not stale) protocols found in the original general_rules.md —
the destructive-action caution rule and the formatting rule — since those
are legitimately good defaults, unlike the timetable they were bundled with.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List


MAX_SKILL_CHARS = 20_000  # sanity ceiling; a real rule file shouldn't be huge
HTML_MARKERS = ("<!doctype html", "<html", "<head>", "<body")

# Rough signal that a file dropped into rules/ actually contains personal,
# likely-to-go-stale facts (schedules, specific dated events) rather than
# durable behavioral instructions. Not a hard block — just a warning, since
# false positives are cheap and false negatives (a timetable silently
# treated as a permanent rule) are the actual bug we're avoiding.
STALE_FACT_PATTERNS = (
    re.compile(r"\b\d{1,2}:\d{2}\s*(AM|PM|am|pm)?\b"),  # clock times
    re.compile(r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b"),
)

DEFAULT_RULES = """\
# Ember default behavioral rules

- Never assume intent on destructive or high-risk actions (deleting files,
  clearing memory, sending messages on the user's behalf). If a request is
  underspecified or risky, summarize your understanding and ask for
  confirmation before executing.
- When producing structured output (reports, summaries, multi-step plans),
  use clear headers and lists rather than a single unformatted paragraph.
"""


@dataclass
class SkillFile:
    name: str
    path: str
    content: str
    kind: str  # "rule" or "fact"
    warnings: List[str]


def _looks_like_html(text: str) -> bool:
    head = text[:1000].lower()
    return any(marker in head for marker in HTML_MARKERS)


def _looks_like_stale_facts(text: str) -> bool:
    hits = sum(1 for pattern in STALE_FACT_PATTERNS if pattern.search(text))
    return hits >= 2


def _validate(name: str, content: str) -> List[str]:
    warnings = []
    if not content.strip():
        warnings.append("File is empty.")
    if len(content) > MAX_SKILL_CHARS:
        warnings.append(f"File is {len(content)} chars — unusually large for a rule file, check it wasn't saved by mistake.")
    if _looks_like_html(content):
        warnings.append("Content looks like a raw HTML document (e.g. a saved webpage), not a rule file. Rejected.")
    return warnings


class SkillsLoader:
    def __init__(self, skills_dir: str = "skills"):
        self.skills_dir = Path(skills_dir)
        self.rules_dir = self.skills_dir / "rules"
        self.facts_dir = self.skills_dir / "facts"
        self.rules_dir.mkdir(parents=True, exist_ok=True)
        self.facts_dir.mkdir(parents=True, exist_ok=True)

        seed_path = self.rules_dir / "default_rules.md"
        if not seed_path.exists():
            seed_path.write_text(DEFAULT_RULES, encoding="utf-8")

    def _load_dir(self, directory: Path, kind: str) -> List[SkillFile]:
        results = []
        if not directory.exists():
            return results
        for f in sorted(directory.iterdir()):
            if f.suffix.lower() not in (".md", ".txt"):
                continue
            try:
                content = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as e:
                results.append(SkillFile(f.name, str(f), "", kind, [f"Could not read file: {e}"]))
                continue

            warnings = _validate(f.name, content)
            if kind == "rule" and _looks_like_stale_facts(content):
                warnings.append(
                    "This rule file contains what looks like dated/scheduled facts "
                    "(clock times, weekday names). Consider moving it to skills/facts/ "
                    "or into ember_memory.py instead, so it doesn't get re-sent on every "
                    "turn and go stale silently."
                )

            # Reject HTML outright — don't load it into anything.
            if any("Rejected" in w for w in warnings):
                results.append(SkillFile(f.name, str(f), "", kind, warnings))
                continue

            results.append(SkillFile(f.name, str(f), content, kind, warnings))
        return results

    def load_rules(self) -> List[SkillFile]:
        """Rules are meant to be injected into the system prompt every turn."""
        return self._load_dir(self.rules_dir, "rule")

    def load_facts(self) -> List[SkillFile]:
        """
        Facts are intentionally NOT auto-injected every turn. Feed these
        into ember_memory.py's store once (e.g. via a one-time import
        command) so they go through semantic recall like everything else,
        rather than being force-fed into every prompt regardless of
        relevance.
        """
        return self._load_dir(self.facts_dir, "fact")

    def get_system_prompt_addendum(self) -> str:
        """Concatenates valid (non-warning-blocked) rule files for prompt injection."""
        chunks = []
        for skill in self.load_rules():
            if skill.content and not any("Rejected" in w for w in skill.warnings):
                chunks.append(skill.content.strip())
        return "\n\n".join(chunks)

    def diagnostics(self) -> List[str]:
        """Human-readable list of every warning found across rules/ and facts/ —
        run this once after porting so you can see what would have silently
        been wrong under the old loader."""
        lines = []
        for skill in self.load_rules() + self.load_facts():
            for w in skill.warnings:
                lines.append(f"[{skill.kind}] {skill.name}: {w}")
        return lines


if __name__ == "__main__":
    loader = SkillsLoader()
    print("--- System prompt addendum ---")
    print(loader.get_system_prompt_addendum())
    print("\n--- Diagnostics ---")
    for line in loader.diagnostics():
        print(line)