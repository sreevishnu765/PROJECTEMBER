"""
ember_proactive.py
===================
Adapted from jarvisforember's core/proactive_engine.py.

Real bug found and fixed in the original: it searches os.environ for an
NVIDIA key (prefix "nvapi-") to decide whether to run, then throws that
result away and unconditionally builds an OpenAI-compatible client against
BACKUP_4_BASE_URL / BACKUP_4_API_KEY (Mistral), regardless of what it found
or whether BACKUP_4 is even configured. If BACKUP_4 isn't set, the engine
silently no-ops forever — it never surfaces this as an error, just prints
and sleeps.

This version does not hardcode a provider at all. It takes an injected
call_fn so it goes through Ember's actual 6-tier fallback chain
(llm_client.py owns that; this module just calls into it), matching your
instruction that the LLM adapter stays responsible for model communication
and the orchestrator decides intent. It also does not hardcode Telegram —
notify_fn is injected so you can wire it to whatever channel Ember actually
has (console print today, Telegram/other later) without touching this file.

Kept from the original because it's a genuinely good idea: the engine asks
the model to decide its OWN next sleep interval based on urgency (goals +
time of day + telemetry), rather than polling on a fixed timer. Clamped to
5-240 minutes so a bad response can't spin-loop or sleep forever.
"""

import os
import json
import time
import threading
import datetime
from dataclasses import dataclass
from typing import Callable, Optional


def get_active_window_title() -> str:
    """Windows-only telemetry signal for anti-procrastination style nudges.
    Ported as-is from app_locator-adjacent code in the source repo — safe,
    read-only, and already guarded with a broad except."""
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value if buf.value else "Unknown"
    except Exception:
        return "Unknown"


PROACTIVE_PROMPT_TEMPLATE = """You are Ember's internal proactive-monitoring process.
Your job is to act as an unobtrusive accountability layer for the user — notice
things that need attention, don't manufacture busywork.

USER GOALS (data/goals.json):
{goals}

RECENT MEMORY CONTEXT:
{memory}

TELEMETRY:
Current time: {now}
Active window: {active_window}

INSTRUCTIONS:
1. Decide if a proactive message is actually warranted right now. Most cycles,
   it is not — set "message" to "NONE" unless there's a concrete reason.
2. Decide how many minutes to sleep before the next check. Urgent upcoming
   items should mean a short interval (15-30 min); nothing pressing should
   mean a long one (120-180 min).

Respond in JSON exactly as:
{{"message": "text or NONE", "sleep_minutes": 60}}
"""


@dataclass
class ProactiveEngine:
    """
    call_fn: Callable[[str], str] — should route through your existing
        provider chain (e.g. a thin wrapper around llm_client's tiered
        call), so this engine gets the same fallback/quota behavior as
        the rest of Ember for free.
    notify_fn: Callable[[str], None] — delivery side effect. Default is a
        plain console print; pass something else (Telegram, a desktop
        notification, a queued message for next chat turn) to change where
        proactive messages land.
    goals_path / memory_path: plain files, matching the original's simple
        JSON/txt approach for this specific subsystem. This is intentionally
        NOT routed through ember_memory.py's semantic store — goals are a
        small, explicitly-edited list, not something that benefits from
        embedding-based recall, and mixing the two would reintroduce the
        layer-conflation problem already flagged in the skills review.
    """

    call_fn: Callable[[str], str]
    notify_fn: Callable[[str], None] = print
    goals_path: str = os.path.join("data", "goals.json")
    memory_snapshot_fn: Optional[Callable[[], str]] = None
    min_sleep_minutes: int = 5
    max_sleep_minutes: int = 240
    _stop_event: threading.Event = None

    def __post_init__(self):
        self._stop_event = threading.Event()

    def _load_goals(self):
        if not os.path.exists(self.goals_path):
            return []
        try:
            with open(self.goals_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []

    def _run_cycle(self) -> int:
        """Runs one check cycle. Returns minutes to sleep before the next one."""
        goals = self._load_goals()
        if not goals:
            return 60  # nothing to watch — check back in an hour, don't hammer

        memory_text = self.memory_snapshot_fn() if self.memory_snapshot_fn else ""
        active_window = get_active_window_title()
        now = datetime.datetime.now().strftime("%I:%M %p on %A")

        prompt = PROACTIVE_PROMPT_TEMPLATE.format(
            goals=json.dumps(goals, indent=2),
            memory=memory_text or "(none)",
            now=now,
            active_window=active_window,
        )

        try:
            raw = self.call_fn(prompt).strip()
        except Exception as e:
            print(f"[proactive] call_fn failed: {e}")
            return 60

        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            print(f"[proactive] non-JSON response, skipping cycle: {raw[:200]}")
            return 60

        message = data.get("message", "NONE")
        sleep_minutes = data.get("sleep_minutes", 60)
        if not isinstance(sleep_minutes, (int, float)):
            sleep_minutes = 60
        sleep_minutes = max(self.min_sleep_minutes, min(self.max_sleep_minutes, int(sleep_minutes)))

        if message and message != "NONE":
            try:
                self.notify_fn(message)
            except Exception as e:
                print(f"[proactive] notify_fn failed: {e}")

        return sleep_minutes

    def _loop(self):
        # Let the rest of Ember finish booting first.
        self._stop_event.wait(timeout=10)
        while not self._stop_event.is_set():
            try:
                sleep_minutes = self._run_cycle()
            except Exception as e:
                print(f"[proactive] unhandled error, backing off: {e}")
                sleep_minutes = 60
            self._stop_event.wait(timeout=sleep_minutes * 60)

    def start(self):
        thread = threading.Thread(target=self._loop, daemon=True)
        thread.start()
        return thread

    def stop(self):
        self._stop_event.set()