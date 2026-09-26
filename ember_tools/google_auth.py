"""
ember_tools/google_auth.py
============================
Shared Google OAuth plumbing for Calendar and Drive — picked up from
Nexus VII's modules/calendar_module.py (get_google_credentials), which
Drive's downloader also depended on there via a cross-import
(`from .calendar_module import get_google_credentials`). Pulled out into
its own module here instead: "Drive imports from Calendar" was a
structural smell in the original — two independent capabilities shouldn't
depend on each other just because one happened to write the shared auth
code first. Both now import this instead.

SCOPES is intentionally minimal: calendar (read/write, since Ember needs
to both check and create events) plus drive.readonly (Drive is a
download-only capability here — no upload/delete surface, so no reason to
request write access). This is the one thing the original got right that's
worth explicitly preserving, not just inheriting by accident: least-
privilege OAuth scope requests.

Setup this genuinely needs from you, not something I can do for you: a
Google Cloud Console project with the Calendar and Drive APIs enabled, an
OAuth 2.0 Client ID downloaded as auth/credentials.json, and running
through the one-time browser consent flow (which writes auth/token.json,
auto-refreshed after that). Until that's done, get_calendar_service() and
get_drive_service() raise a clear, caught exception — every calling tool
handler turns that into a plain-language "not configured yet" message
rather than a stack trace.

Deliberate decision to stay in Google's "Testing" publishing status
rather than pursue full verification (see conversation history): the
current SCOPES include drive.readonly, which Google classifies as a
Restricted scope — moving to Production would require a paid third-party
CASA security assessment plus domain ownership plus a hosted privacy
policy, none of which buys anything real for an app with exactly one
user. The accepted cost of staying in Testing: a test user's
authorization expires roughly 7 days after the actual consent — not 7
days after the last successful refresh, which matters below.

Consent-timestamp tracking (added this session): token.json gets
rewritten on EVERY successful refresh, not just a fresh consent (see the
`with open(_TOKEN_PATH, ...)` write below — it runs after either branch
of the `if not creds or not creds.valid` block). That makes the token
file's own mtime useless as a proxy for "how long until the Testing-mode
grant actually expires" — a well-behaved refresh cycle would keep
touching that file every call, making it look perpetually fresh right up
until the moment the underlying grant silently dies. auth/last_consent.json
is written ONLY in the branch that actually runs the interactive
flow.run_local_server() — i.e. only on a genuine consent event — so
last_consent_timestamp() below gives a reliable answer for
ember_tools/google_auth_watchdog.py to warn ahead of the ~7-day cutoff,
instead of after a random future calendar/drive call mysteriously fails.
"""

import json
import os
import time
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive.readonly",
]

_TOOLS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _TOOLS_DIR.parent
_AUTH_DIR = _PROJECT_ROOT / "auth"
_TOKEN_PATH = _AUTH_DIR / "token.json"
_CREDENTIALS_PATH = _AUTH_DIR / "credentials.json"
_CONSENT_MARKER_PATH = _AUTH_DIR / "last_consent.json"


def _record_consent() -> None:
    """Marks the moment a REAL interactive consent flow just completed —
    deliberately separate from token.json's own write (see module
    docstring: that happens on every refresh too, not just a fresh
    consent). Never raises — a failure to write this marker degrades to
    "the watchdog can't warn ahead of time for this cycle," never to a
    broken auth flow; the actual credentials are already valid and
    returned to the caller regardless of whether this succeeds."""
    try:
        _AUTH_DIR.mkdir(parents=True, exist_ok=True)
        with open(_CONSENT_MARKER_PATH, "w", encoding="utf-8") as f:
            json.dump({"consented_at": time.time()}, f)
    except OSError as e:
        print(f"[google_auth] Warning: couldn't record consent timestamp ({e}) — "
              f"the expiry watchdog won't be able to warn ahead of time this cycle.")


def last_consent_timestamp() -> "float | None":
    """Returns the unix timestamp of the last real interactive consent
    flow, or None if one has never been recorded (e.g. a fresh checkout
    that hasn't gone through the flow yet, or a marker predating this
    watchdog's existence). Never raises — read by
    ember_tools/google_auth_watchdog.py, which already treats None as
    "nothing to watch yet," not an error."""
    try:
        with open(_CONSENT_MARKER_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("consented_at")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def get_google_credentials():
    """Returns valid Google credentials for SCOPES, refreshing or running
    the interactive consent flow as needed. Raises FileNotFoundError with
    a clear, actionable message if auth/credentials.json hasn't been
    downloaded from Google Cloud Console yet — callers are expected to
    catch this (or any Exception) and turn it into a plain-language reply,
    same contract as every other capability in Ember."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if _TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                # Covers the exact Testing-mode failure this session is
                # working around: a refresh_token past its ~7-day consent
                # grant fails here with invalid_grant, not on the .valid
                # check above. Falling through to a fresh interactive
                # flow below is already the right recovery — the one gap
                # was doing it silently, so a browser window popping open
                # mid-conversation looked like a mystery rather than an
                # expected, explained recovery step.
                print("[google_auth] Your Google authorization needs to be renewed, sir "
                      "(likely the Testing-mode ~7-day grant lapsing) — opening the "
                      "browser for a quick re-consent.")
                creds = None

        if not creds:
            if not _CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    "No Google OAuth credentials configured, sir — download an OAuth "
                    "2.0 Client ID from Google Cloud Console (with the Calendar and "
                    "Drive APIs enabled) and save it as auth/credentials.json."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(_CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
            _record_consent()

        _AUTH_DIR.mkdir(parents=True, exist_ok=True)
        with open(_TOKEN_PATH, "w", encoding="utf-8") as token_file:
            token_file.write(creds.to_json())

    return creds


def get_calendar_service():
    from googleapiclient.discovery import build
    return build("calendar", "v3", credentials=get_google_credentials())


def get_drive_service():
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=get_google_credentials())
