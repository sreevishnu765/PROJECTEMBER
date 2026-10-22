"""
ember_tools/power.py
=====================
Shut the laptop down on request, with a window to change your mind.

Design, in the same spirit as computer.py:

  * No shell. The OS tool is called with an argument list (`shutdown /s /t N`), never a string built from
    user text, so nothing the user (or a fetched web page) says can end up on a command line.
  * The countdown belongs to Windows, not to Ember. `shutdown /s /t 30` keeps counting even if Ember crashes,
    and Windows itself shows a "you're about to be signed out" notice, so a mis-heard command is visible on
    screen, not only in Ember's reply. `shutdown /a` is the cancel.
  * Never `/f`. Per Microsoft's docs `/f` is implied when /t is greater than zero, so the countdown must be
    given enough room to save work; Ember says so in its reply rather than pretending it is a clean exit.
  * Windows only. On anything else the answer is an honest "not supported here", not a guess at another OS's
    command.

Confirmation is NOT done here: the tool is registered `destructive=True` in ember_core.py, so every request
goes through the confirmation gate (dialog, or a spoken yes/no) before schedule_shutdown() is ever called.
"""

import subprocess
import sys

DEFAULT_DELAY_SECONDS = 30
MIN_DELAY_SECONDS = 10               # never less: the cancel window is the whole safety story
MAX_DELAY_SECONDS = 24 * 60 * 60

_ERR_NOT_IN_PROGRESS = 1116          # `shutdown /a` when nothing is scheduled
_ERR_ALREADY_SCHEDULED = 1190        # `shutdown /s` when a shutdown is already counting down

# Replaced in tests; never reassigned elsewhere.
_run = subprocess.run
_popen = subprocess.Popen


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def spoken_duration(seconds: int) -> str:
    """30 -> '30 seconds', 600 -> '10 minutes', 5400 -> '1 hour 30 minutes'."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return " ".join(parts) or f"{seconds} seconds"


def _call(args: "list[str]") -> "tuple[int, str] | str":
    """Runs shutdown.exe. Returns (returncode, combined output), or an error message string."""
    try:
        r = _run(args, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return "I couldn't find the Windows shutdown command, sir."
    except subprocess.TimeoutExpired:
        return "The shutdown command didn't answer in time, sir, so I can't say whether it took."
    except OSError as e:
        return f"Couldn't run the shutdown command, sir: {e}"
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def schedule_shutdown(delay_seconds: "int | None" = None, active_reminders: int = 0) -> str:
    """Starts the countdown and returns the reply to show. Never raises."""
    if not _is_windows():
        return "Shutting down the machine is only set up for Windows, sir — nothing was done."
    delay = DEFAULT_DELAY_SECONDS if delay_seconds is None else int(delay_seconds)
    if delay > MAX_DELAY_SECONDS:
        return "I'll schedule a shutdown up to 24 hours away, sir, not further — nothing was done."
    delay = max(delay, MIN_DELAY_SECONDS)

    out = _call([
        "shutdown", "/s", "/t", str(delay),
        "/c", "Ember is shutting this computer down at your request. Say 'Ember, cancel the shutdown' to stop it.",
    ])
    if isinstance(out, str):
        return out
    code, text = out
    if code == _ERR_ALREADY_SCHEDULED:
        return "A shutdown is already counting down, sir. Say 'cancel the shutdown' if you want to stop it."
    if code != 0:
        return f"Windows refused the shutdown, sir (code {code}){': ' + text if text else ''}. Nothing was scheduled."

    msg = (f"Shutting down in {spoken_duration(delay)}, sir. Save anything open — Windows closes apps when the "
           f"countdown ends. Say 'cancel the shutdown' to stop it.")
    if active_reminders:
        n = active_reminders
        msg += f" You have {n} active reminder{'s' if n != 1 else ''}; they won't fire while the laptop is off."
    return msg


def cancel_shutdown() -> str:
    """Stops a pending countdown. Never raises."""
    if not _is_windows():
        return "There's no shutdown of mine to cancel on this system, sir."
    out = _call(["shutdown", "/a"])
    if isinstance(out, str):
        return out
    code, text = out
    if code == 0:
        return "Shutdown cancelled, sir."
    if code == _ERR_NOT_IN_PROGRESS:
        return "No shutdown is pending, sir."
    return f"Windows wouldn't cancel it, sir (code {code}){': ' + text if text else ''}."


# Fixed text, no user input anywhere in it. Application.SetSuspendState(Suspend) really sleeps; the older
# `rundll32 powrprof.dll,SetSuspendState` hibernates instead whenever hibernation is enabled, which is not what
# "sleep" means. The short pause lets Ember's spoken reply finish before the machine goes down.
_SLEEP_SCRIPT = ("Start-Sleep -Seconds 3; Add-Type -AssemblyName System.Windows.Forms; "
                 "[System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false) | Out-Null")


def sleep_now(active_reminders: int = 0) -> str:
    """Puts the PC to sleep (not shut down) a few seconds from now. Never raises. Nothing to confirm or cancel:
    sleep keeps every open app and wakes with a key press."""
    if not _is_windows():
        return "Sleep is only set up for Windows, sir — nothing was done."
    try:
        _popen(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _SLEEP_SCRIPT],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return "I couldn't find PowerShell, sir, so I can't put the laptop to sleep."
    except OSError as e:
        return f"Couldn't start the sleep command, sir: {e}"
    msg = "Lights out, sir."
    if active_reminders:
        n = active_reminders
        msg += f" You have {n} active reminder{'s' if n != 1 else ''}; they won't fire while it's asleep."
    return msg
