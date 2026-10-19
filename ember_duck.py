"""Lower every OTHER app's volume while Ember speaks an announcement (a reminder or timer).

Why: a reminder that fires while a race is playing at full volume is spoken at the same loudness as
the commentary and simply can't be heard. Windows keeps a separate volume per app (the Volume Mixer);
this turns the other apps down for the few seconds Ember talks and puts every one back afterwards.

What it never does:
  * touch Ember's own audio (the app and its helper processes are recognised by name and by being
    this process),
  * touch the master volume or system sounds,
  * leave anything quiet. Three independent ways back: the normal release, a watchdog timer
    (MAX_DUCK_SECONDS), and a journal file (data/duck_restore.json) that the next start of Ember reads
    to undo a duck left behind by a crash or a force-quit.

It is Windows-only and needs `pip install pycaw` (which brings comtypes and psutil). Without them, or on
another OS, every call is a harmless no-op and one line says why. Turn it off with EMBER_DUCK_OTHER_AUDIO=0;
change how far others are turned down with EMBER_DUCK_LEVEL (fraction of their current volume, default
0.10); never touch certain apps with EMBER_DUCK_EXCLUDE="spotify.exe,vlc.exe".
"""
from __future__ import annotations

import atexit
import json
import os
import sys
import threading
import time
from typing import Callable, Iterable, Optional

DEFAULT_LEVEL = 0.10
MAX_DUCK_SECONDS = 25.0
_RESTORE_TOLERANCE = 0.03

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
JOURNAL_PATH = os.path.join(_DATA_DIR, "duck_restore.json")


def _log(msg: str) -> None:
    print(f"[ember_duck] {msg}")


class AudioSession:
    """One app's entry in the Windows volume mixer. `key` is stable for the life of that entry."""

    def __init__(self, key: str, pid: Optional[int], name: str,
                 get_volume: Callable[[], float], set_volume: Callable[[float], None],
                 is_muted: Callable[[], bool] = lambda: False):
        self.key, self.pid, self.name = key, pid, (name or "").lower()
        self.get_volume, self.set_volume, self.is_muted = get_volume, set_volume, is_muted


def _pycaw_sessions() -> "list[AudioSession]":
    """The real thing. Must be called on a thread that has initialised COM (the ducker does)."""
    from pycaw.pycaw import AudioUtilities  # type: ignore

    out: "list[AudioSession]" = []
    for s in AudioUtilities.GetAllSessions():
        try:
            proc = s.Process
            vol = s.SimpleAudioVolume
            if vol is None:
                continue
            pid = int(proc.pid) if proc is not None else int(getattr(s, "ProcessId", 0) or 0) or None
            name = proc.name() if proc is not None else ""
            key = (getattr(s, "InstanceIdentifier", None) or getattr(s, "Identifier", None) or f"pid:{pid}")
            out.append(AudioSession(
                key=str(key), pid=pid, name=name,
                get_volume=lambda v=vol: float(v.GetMasterVolume()),
                set_volume=lambda x, v=vol: v.SetMasterVolume(max(0.0, min(1.0, float(x))), None),
                is_muted=lambda v=vol: bool(v.GetMute()),
            ))
        except Exception:
            continue    # one odd session must not stop the rest
    return out


def _protected_pids() -> "set[int]":
    """This process, plus every process that looks like the Ember desktop app (the app and the Electron
    helper that actually plays the audio all share the app's executable name)."""
    pids = {os.getpid()}
    try:
        import psutil  # type: ignore
        for p in psutil.process_iter(["pid", "name"]):
            if "ember" in (p.info.get("name") or "").lower():
                pids.add(int(p.info["pid"]))
    except Exception:
        pass
    return pids


class AudioDucker:
    def __init__(
        self,
        enabled: Optional[bool] = None,
        level: Optional[float] = None,
        journal_path: str = JOURNAL_PATH,
        session_provider: Optional[Callable[[], "list[AudioSession]"]] = None,
        protected_pids: Optional[Callable[[], "set[int]"]] = None,
        max_seconds: float = MAX_DUCK_SECONDS,
        exclude: Optional[Iterable[str]] = None,
    ):
        env_on = os.environ.get("EMBER_DUCK_OTHER_AUDIO", "1").strip() not in ("0", "false", "no", "off")
        self._explicit_provider = session_provider is not None
        self.enabled = (env_on and sys.platform == "win32") if enabled is None else bool(enabled)
        try:
            self.level = float(os.environ.get("EMBER_DUCK_LEVEL", DEFAULT_LEVEL)) if level is None else float(level)
        except ValueError:
            self.level = DEFAULT_LEVEL
        self.level = max(0.0, min(1.0, self.level))
        self._journal = journal_path
        self._provider = session_provider or _pycaw_sessions
        self._protected = protected_pids or _protected_pids
        self._max_seconds = max_seconds
        env_ex = os.environ.get("EMBER_DUCK_EXCLUDE", "")
        self._exclude = {x.strip().lower() for x in (list(exclude or []) + env_ex.split(",")) if x.strip()}

        self._lock = threading.RLock()
        self._holds = 0
        self._saved: "dict[str, float]" = {}
        self._watchdog: "threading.Timer | None" = None
        self._unavailable_logged = False
        self._recovered = False
        atexit.register(self.restore_now)

    # ------------------------------------------------------------------ public
    def acquire(self) -> bool:
        """Turn the other apps down (first caller) or join an existing duck. Pair with release()."""
        if not self.enabled:
            return False
        with self._lock:
            self._recover_once()
            self._holds += 1
            if self._holds == 1:
                if not self._duck():
                    self._holds = 0
                    return False
                self._arm_watchdog()
            return True

    def release(self) -> None:
        """Drop one hold; the last one puts every app back."""
        with self._lock:
            if self._holds <= 0:
                return
            self._holds -= 1
            if self._holds == 0:
                self._cancel_watchdog()
                self._restore()

    def restore_now(self) -> None:
        """Unconditional restore (exit, watchdog, manual)."""
        with self._lock:
            self._holds = 0
            self._cancel_watchdog()
            self._restore()

    # ------------------------------------------------------------------ internals
    def _com(self):
        if self._explicit_provider:
            return None
        try:
            import comtypes  # type: ignore
            comtypes.CoInitialize()
            return comtypes
        except Exception:
            return None

    def _sessions(self) -> "list[AudioSession]":
        try:
            return list(self._provider())
        except ImportError:
            if not self._unavailable_logged:
                self._unavailable_logged = True
                _log("other-app volume lowering is unavailable: run `pip install pycaw` in Ember's venv "
                     "(set EMBER_DUCK_OTHER_AUDIO=0 to silence this).")
            self.enabled = False
            return []
        except Exception as e:
            if not self._unavailable_logged:
                self._unavailable_logged = True
                _log(f"couldn't read the volume mixer ({type(e).__name__}: {e}); other apps stay at full volume.")
            return []

    def _duck(self) -> bool:
        com = self._com()
        try:
            sessions = self._sessions()
            if not sessions:
                return False
            protected = self._protected()
            saved: "dict[str, float]" = {}
            for s in sessions:
                if not s.pid or s.pid in protected:          # system sounds / Ember itself
                    continue
                if any(x in s.name for x in self._exclude if x):
                    continue
                try:
                    if s.is_muted():
                        continue
                    original = s.get_volume()
                    if original <= 0.02:
                        continue
                    saved[s.key] = original
                    s.set_volume(original * self.level)
                except Exception:
                    saved.pop(s.key, None)
            self._saved = saved
            self._write_journal(saved)
            if saved:
                _log(f"lowered {len(saved)} app(s) to {int(self.level * 100)}% while Ember speaks")
            return bool(saved)
        finally:
            if com is not None:
                try:
                    com.CoUninitialize()
                except Exception:
                    pass

    def _restore(self) -> None:
        saved, self._saved = self._saved, {}
        if not saved:
            return
        self._apply_restore(saved, only_if_still_ducked=False)
        self._clear_journal()

    def _apply_restore(self, saved: "dict[str, float]", only_if_still_ducked: bool) -> int:
        com = self._com()
        restored = 0
        try:
            for s in self._sessions():
                original = saved.get(s.key)
                if original is None:
                    continue
                try:
                    if only_if_still_ducked and s.get_volume() > original * self.level + _RESTORE_TOLERANCE + 0.05:
                        continue    # somebody changed it since: leave their setting alone
                    s.set_volume(original)
                    restored += 1
                except Exception:
                    continue
        finally:
            if com is not None:
                try:
                    com.CoUninitialize()
                except Exception:
                    pass
        return restored

    def _recover_once(self) -> None:
        """A previous run that died mid-duck left its originals in the journal: put them back."""
        if self._recovered:
            return
        self._recovered = True
        try:
            with open(self._journal, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        saved = {str(k): float(v) for k, v in (data.get("volumes") or {}).items()}
        if saved:
            n = self._apply_restore(saved, only_if_still_ducked=True)
            _log(f"restored {n} app volume(s) left lowered by an earlier run")
        self._clear_journal()

    def recover(self) -> None:
        """Call once at startup; safe to call any time."""
        if not self.enabled:
            return
        with self._lock:
            self._recover_once()

    def _write_journal(self, saved: "dict[str, float]") -> None:
        try:
            os.makedirs(os.path.dirname(self._journal), exist_ok=True)
            with open(self._journal, "w", encoding="utf-8") as f:
                json.dump({"saved_at": time.time(), "volumes": saved}, f)
        except OSError:
            pass

    def _clear_journal(self) -> None:
        try:
            os.remove(self._journal)
        except OSError:
            pass

    def _arm_watchdog(self) -> None:
        self._cancel_watchdog()
        t = threading.Timer(self._max_seconds, self._watchdog_fired)
        t.daemon = True
        t.start()
        self._watchdog = t

    def _cancel_watchdog(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    def _watchdog_fired(self) -> None:
        _log(f"duck held for more than {self._max_seconds:.0f}s - restoring volumes")
        self.restore_now()


_ducker: "AudioDucker | None" = None
_ducker_lock = threading.Lock()


def get_ducker() -> AudioDucker:
    global _ducker
    with _ducker_lock:
        if _ducker is None:
            _ducker = AudioDucker()
            _ducker.recover()
        return _ducker
