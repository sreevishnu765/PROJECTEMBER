"""
ember_tools/computer.py
========================
Computer-interaction capabilities, per the next-phase brief's item #6 —
with the brief's own explicit caveat taken seriously: "Do not give
arbitrary unrestricted execution to an LLM. Implement permission
boundaries and confirmation for risky/destructive actions."

Design choice made here, deliberately conservative: every operation
(list, read, execute) is checked against an explicit allowlist of
directory prefixes (`data/allowed_paths.json`), not "anything on disk."
This is a real permission boundary, not a token gesture — a path outside
the allowlist is refused outright, with a clear message telling the user
how to add it, rather than silently widened or worked around. The
allowlist seeds itself with just the project root on first use; the user
opts individual other directories in (e.g. wherever "Project Alpha"
actually lives on disk) rather than Ember defaulting to full filesystem
visibility the moment this file is imported.

Script execution (`run_script`) is additionally always routed through
ember_confirmation regardless of what the allowlist says — registered as
`destructive=True` in ember_core.py's tool registration, not
conditionally. Read-only operations (list/read) are NOT destructive and
don't prompt every time, since confirming every file read would make the
feature unusable and the actual risk (disclosure of something already on
an allowlisted, user-approved path) is much lower than execution.

What this deliberately does NOT do: no shell string execution
(`subprocess.run(cmd, shell=True)` on arbitrary user-composed text) — only
a resolved, allowlisted, on-disk script file is ever invoked. No sudo /
elevated execution. No network egress control here (the script itself can
still do whatever a normal user-level process can do) — this bounds WHICH
files/directories/scripts are reachable through Ember, it does not sandbox
what an allowed script does once running, which is a materially larger
feature (containerization/process isolation) explicitly out of scope for
a personal-assistant pass like this one.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _TOOLS_DIR.parent
_ALLOWLIST_PATH = _PROJECT_ROOT / "data" / "allowed_paths.json"

RUNNABLE_EXTENSIONS = {".py", ".sh", ".bat", ".ps1"}
MAX_READ_CHARS = 8000
SCRIPT_TIMEOUT_SECONDS = 60


def _load_allowlist() -> "list[str]":
    if not _ALLOWLIST_PATH.exists():
        _ALLOWLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        default = [str(_PROJECT_ROOT)]
        with open(_ALLOWLIST_PATH, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2)
        return default
    try:
        with open(_ALLOWLIST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return [str(_PROJECT_ROOT)]


def is_path_allowed(path: str) -> bool:
    """Resolves `path` to an absolute, symlink-free form and checks
    whether it falls under any allowlisted prefix. Resolving first is
    what stops '../../../etc/passwd'-style traversal from a naive prefix
    string-match — comparison is always done on the fully resolved path,
    never the raw string the caller supplied."""
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        return False
    for allowed in _load_allowlist():
        try:
            allowed_resolved = Path(allowed).expanduser().resolve()
        except OSError:
            continue
        if resolved == allowed_resolved or allowed_resolved in resolved.parents:
            return True
    return False


def add_allowed_path(path: str) -> str:
    """User-facing way to opt a new directory in, without hand-editing
    JSON. Stores the resolved absolute path."""
    resolved = str(Path(path).expanduser().resolve())
    allowlist = _load_allowlist()
    if resolved in allowlist:
        return f"'{resolved}' is already allowed, sir."
    allowlist.append(resolved)
    with open(_ALLOWLIST_PATH, "w", encoding="utf-8") as f:
        json.dump(allowlist, f, indent=2)
    return f"Added '{resolved}' to the allowed paths, sir."


def list_directory(path: str) -> str:
    if not is_path_allowed(path):
        return (
            f"'{path}' isn't on the allowed-paths list, sir — I won't browse outside "
            f"approved directories. Use the allow-path command to add it first if you want me to."
        )
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        return f"'{path}' isn't a directory, sir."
    try:
        entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    except OSError as e:
        return f"Couldn't list '{path}', sir: {e}"
    lines = [f"  {'[dir] ' if e.is_dir() else '      '}{e.name}" for e in entries]
    return f"Contents of {p}, sir:\n" + ("\n".join(lines) if lines else "  (empty)")


def read_file(path: str, max_chars: int = MAX_READ_CHARS) -> str:
    if not is_path_allowed(path):
        return (
            f"'{path}' isn't on the allowed-paths list, sir — I won't read outside "
            f"approved directories. Use the allow-path command to add it first if you want me to."
        )
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        return f"'{path}' isn't a file, sir."
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"Couldn't read '{path}', sir: {e}"
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n[... truncated, sir — {len(text)} chars total, showing first {max_chars}]"
    return text


def run_script(path: str, args: "list[str] | None" = None, timeout: int = SCRIPT_TIMEOUT_SECONDS) -> str:
    """Executes an allowlisted script file directly (no shell string
    interpolation) and returns captured stdout/stderr. Callers MUST gate
    this behind confirmation — this function itself does not prompt; it's
    the tool registry's `destructive=True` flag on this specific tool that
    ensures the user is asked every time, not a case-by-case judgment call
    inside this function that could be forgotten at a new call site."""
    if not is_path_allowed(path):
        return f"'{path}' isn't on the allowed-paths list, sir — I won't execute it."
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        return f"'{path}' isn't a file, sir."
    if p.suffix.lower() not in RUNNABLE_EXTENSIONS:
        return f"'{p.suffix}' isn't a script type I'll run, sir (allowed: {', '.join(sorted(RUNNABLE_EXTENSIONS))})."

    if p.suffix.lower() == ".py":
        cmd = [sys.executable, str(p)] + (args or [])
    elif p.suffix.lower() == ".ps1":
        cmd = ["powershell", "-NoProfile", "-File", str(p)] + (args or [])
    else:
        cmd = [str(p)] + (args or [])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(p.parent))
    except subprocess.TimeoutExpired:
        return f"'{p.name}' timed out after {timeout}s, sir — killed it."
    except OSError as e:
        return f"Couldn't run '{p.name}', sir: {e}"

    output = (result.stdout or "").strip()
    error = (result.stderr or "").strip()
    parts = [f"'{p.name}' finished with exit code {result.returncode}, sir."]
    if output:
        parts.append(f"Output:\n{output[:MAX_READ_CHARS]}")
    if error:
        parts.append(f"Errors:\n{error[:MAX_READ_CHARS]}")
    return "\n\n".join(parts)
