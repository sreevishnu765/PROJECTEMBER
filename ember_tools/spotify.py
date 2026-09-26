"""
ember_tools/spotify.py
========================
Picked up from Nexus VII's core/jarvis_local.py (play_music_on_spotify /
_run_spotify_search_and_play) — a genuinely good, zero-API-key trick
directly matching a named example in the next-phase brief ("Spotify" is
listed alongside calendar/weather/GitHub as a target capability).

How it works: a headless Playwright browser hits Spotify's public search
page (no login, no API key, no OAuth) to resolve a text query to a track
ID, then hands playback off to the user's already-installed Spotify
desktop app via the `spotify:track:<id>` URI scheme (os.startfile). No
new dependency — Ember already requires Playwright for pdf_export.py, so
this reuses that install as-is.

Real bug found and fixed: the original ran the search on a background
thread and returned "Searching and playing '<query>' on Spotify." to the
user IMMEDIATELY, before the search had even started — if Spotify wasn't
installed, the track wasn't found, or the page layout changed, the
failure was swallowed by a bare `print()` inside the thread and the user
was never told. That's exactly the "no silent failures" violation Ember's
own standard rules out. Fixed by publishing a bus event on both outcomes
("spotify.play_failed" / nothing needed on success since the desktop app
itself becomes the visible confirmation) — NotificationManager surfaces
the failure the same way it already surfaces a reminder or a proactive
message, instead of it vanishing into a background thread's stdout.

Windows-only (os.startfile + the spotify: URI scheme), matching Ember's
actual deployment target — guarded the same way ember_tools/app_launcher.py
guards its own Windows-only calls, which the original file did NOT do at
all (would raise AttributeError on non-Windows with no clear message).
"""

import sys
import threading
import urllib.parse

from ember_bus import get_bus

IS_WINDOWS = sys.platform.startswith("win")
SEARCH_TIMEOUT_MS = 15000
SELECTOR_TIMEOUT_MS = 10000


def _run_search_and_play(query: str) -> None:
    """Runs on a background thread — see play_music_on_spotify() for why
    this doesn't block the conversational turn. Every failure path
    publishes onto the bus rather than only printing, so it actually
    reaches the user via NotificationManager instead of disappearing."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(f"https://open.spotify.com/search/{urllib.parse.quote(query)}/tracks", timeout=SEARCH_TIMEOUT_MS)
            page.wait_for_selector('a[href^="/track/"]', timeout=SELECTOR_TIMEOUT_MS)
            href = page.locator('a[href^="/track/"]').first.get_attribute("href")
            browser.close()

        if not href:
            get_bus().publish("spotify.play_failed", {"query": query, "reason": "No matching track found on Spotify."})
            return

        track_id = href.split("/")[-1]
        import os
        os.startfile(f"spotify:track:{track_id}")  # noqa: S606 — Windows-only, resolved from Spotify's own search results, not user-supplied input
    except Exception as e:
        get_bus().publish("spotify.play_failed", {"query": query, "reason": str(e)})


def play_music_on_spotify(query: str) -> str:
    """Kicks off the search+play on a background thread (a real browser
    round-trip is a few seconds — blocking the conversational turn on it
    would make Ember feel unresponsive for something that's supposed to
    be a quick "play this song" request) and returns an immediate
    acknowledgment. Unlike the original, a failure that happens after this
    function returns is NOT silent — see _run_search_and_play above."""
    if not IS_WINDOWS:
        return "Spotify playback control is only wired up for Windows right now, sir."
    if not query or not query.strip():
        return "Play what, sir? I didn't catch a song or artist."

    thread = threading.Thread(target=_run_search_and_play, args=(query.strip(),), daemon=True)
    thread.start()
    return f"Searching for '{query}' on Spotify, sir — I'll let you know if it doesn't turn up anything."
