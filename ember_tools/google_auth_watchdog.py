"""
ember_tools/google_auth_watchdog.py
=====================================
Deterministic background check for Google's Testing-mode ~7-day consent
grant expiry (see google_auth.py's module docstring for the full
reasoning on why Ember is staying in Testing rather than pursuing full
verification — drive.readonly forces a Restricted-scope, paid CASA
security assessment that's disproportionate for a single-user personal
assistant).

Deliberately NOT routed through ember_proactive.py's engine — that one
asks the model to judge whether a message is warranted from open-ended
context (goals, telemetry, memory). This doesn't need or want that: the
actual answer ("how many days since last_consent_timestamp()") is a
plain number with a fixed threshold, the same "small enumerable
question -> deterministic check, not an LLM call" reasoning already
used throughout this project (needs_search, _looks_sufficient,
ember_intent.py's structural regex checks, etc.). Spending a generation
call and adding latency/quota cost to ask a model "should I warn about
this" when the honest answer is just `age_days >= 6.0` would be the
exact anti-pattern this project has repeatedly caught and fixed
elsewhere.

Fires "google_auth.expiring_soon" on the shared bus AT MOST ONCE per
actual consent cycle, not once per check — _last_warned_for tracks WHICH
consent timestamp has already been warned about, so re-authenticating
(which writes a new, later last_consent_timestamp()) naturally resets
the warning for the new cycle without any extra bookkeeping. Without
that guard, a person who's seen the warning and just hasn't gotten
around to refreshing yet would get nagged on every single poll cycle
for the rest of the week, which is worse than useless — the reminder
system elsewhere in this project (ember_reminders.py) is explicitly
one-shot-or-scheduled-recurrence, never "repeat until acknowledged."

This is a stopgap tied to a specific, named trade-off (staying in
Testing mode), not a permanent architectural fixture — if the scopes
are ever narrowed enough to drop out of the Restricted tier and the app
gets verified into Production, last_consent_timestamp()'s age simply
never crosses the threshold again and this becomes a silent no-op
rather than something that needs to be torn back out.
"""

import threading
import time
from typing import Optional

from ember_tools import google_auth

DEFAULT_POLL_SECONDS = 6 * 3600   # a few checks a day -- this is a local file read, not an API call, so frequent polling costs nothing
WARNING_THRESHOLD_DAYS = 6.0      # Google's Testing-mode grant is ~7 days from actual consent; warn with a day of buffer rather than cutting it exactly to the wire


class GoogleAuthWatchdog:
    """One daemon thread, same start()/stop() shape as ProactiveEngine
    and ReminderStore so EmberRuntime can compose all three identically
    (see ember_runtime.py)."""

    def __init__(self, bus, poll_seconds: int = DEFAULT_POLL_SECONDS, threshold_days: float = WARNING_THRESHOLD_DAYS):
        self._bus = bus
        self._poll_seconds = poll_seconds
        self._threshold_days = threshold_days
        self._stop_event = threading.Event()
        self._last_warned_for: Optional[float] = None

    def _check_once(self) -> None:
        consented_at = google_auth.last_consent_timestamp()
        if consented_at is None:
            return  # never actually consented on this machine yet -- nothing to watch
        age_days = (time.time() - consented_at) / 86400.0
        if age_days >= self._threshold_days and self._last_warned_for != consented_at:
            self._last_warned_for = consented_at
            self._bus.publish("google_auth.expiring_soon", {
                "age_days": round(age_days, 1),
                "consented_at": consented_at,
            })

    def _loop(self):
        # Give the rest of Ember a moment to finish booting first, same
        # courtesy ember_proactive.py's own loop extends before its first
        # cycle.
        self._stop_event.wait(timeout=10)
        while not self._stop_event.is_set():
            try:
                self._check_once()
            except Exception as e:
                # A failed check (e.g. a transient file-read hiccup) must
                # never take down the whole background runtime -- same
                # graceful-degradation contract as every other loop here.
                # Just try again next cycle.
                print(f"[google_auth_watchdog] check failed, will retry next cycle: {e}")
            self._stop_event.wait(timeout=self._poll_seconds)

    def start(self):
        thread = threading.Thread(target=self._loop, daemon=True)
        thread.start()
        return thread

    def stop(self):
        self._stop_event.set()
