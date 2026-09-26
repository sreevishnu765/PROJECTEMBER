"""
ember_tools/app_launcher.py
============================
Ported from jarvisforember/modules/app_locator.py — this file was well
structured in the original with no bugs found on inspection, so it's kept
close to verbatim. Changes made:
  - Cache path is now configurable (points at Ember's own data/ dir instead
    of living next to the module file, so it survives a reinstall cleanly).
  - Added launch_app(), which the original repo didn't include here (the
    actual subprocess.Popen call lived inline in jarvis_local.py's tool
    dispatch). Routing app *launches* through your ember_confirmation gate
    is a judgment call for you: launching Notepad is harmless, launching
    something that immediately sends data out is not. Default here does
    NOT auto-confirm — wire is_destructive-style gating at the call site
    in ember_core.py if you want blanket confirmation for all launches, or
    leave ungated since "open an app" is low-risk relative to file/email
    deletion. Left as your call, not baked in.

Windows-only (uses winreg, PowerShell Get-StartApps). No-ops safely on
non-Windows if imported, since your Vivobook target is Windows.
"""

import os
import sys
import json
import time
import difflib
import subprocess
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")

if IS_WINDOWS:
    import winreg


def _default_cache_path() -> str:
    data_dir = Path(__file__).resolve().parent.parent / "data"
    data_dir.mkdir(exist_ok=True)
    return str(data_dir / "app_cache.json")


CACHE_PATH = _default_cache_path()
CACHE_MAX_AGE_SECONDS = 24 * 3600


def get_powershell_startapps():
    apps = []
    if not IS_WINDOWS:
        return apps
    try:
        output = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", "Get-StartApps | ConvertTo-Json"],
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if output.strip():
            data = json.loads(output)
            if not isinstance(data, list):
                data = [data]
            for item in data:
                name = item.get("Name", "")
                appid = item.get("AppID", "")
                if os.path.exists(appid) and appid.lower().endswith(".exe"):
                    apps.append({"name": name, "path": appid, "source": "powershell_startapps"})
                elif appid and not os.path.exists(appid) and not appid.lower().endswith(".exe"):
                    apps.append({"name": name, "path": f"shell:AppsFolder\\{appid}", "source": "uwp"})
    except Exception:
        pass
    return apps


def get_start_menu_lnks():
    apps = []
    if not IS_WINDOWS:
        return apps
    script = """
    $dirs = @(
        "$env:ProgramData\\Microsoft\\Windows\\Start Menu\\Programs",
        "$env:APPDATA\\Microsoft\\Windows\\Start Menu\\Programs",
        "$env:USERPROFILE\\Desktop",
        "$env:PUBLIC\\Desktop"
    )
    $shell = New-Object -ComObject WScript.Shell
    $results = @()
    foreach ($dir in $dirs) {
        if (Test-Path $dir) {
            $files = Get-ChildItem -Path $dir -Include "*.lnk", "*.url" -Recurse -File -ErrorAction SilentlyContinue
            foreach ($file in $files) {
                try {
                    if ($file.Extension -eq ".url") {
                        $results += @{ name = $file.BaseName; path = $file.FullName; source = "desktop_url" }
                    } else {
                        $target = $shell.CreateShortcut($file.FullName).TargetPath
                        if ($target -match "\\.exe$") {
                            $results += @{ name = $file.BaseName; path = $file.FullName; source = "desktop_lnk" }
                        }
                    }
                } catch {}
            }
        }
    }
    $results | ConvertTo-Json -Depth 2
    """
    try:
        output = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", script],
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if output.strip():
            data = json.loads(output)
            if not isinstance(data, list):
                data = [data]
            for item in data:
                if os.path.exists(item.get("path", "")):
                    apps.append(item)
    except Exception:
        pass
    return apps


def get_registry_app_paths():
    apps = []
    if not IS_WINDOWS:
        return apps
    keys_to_check = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
    ]
    for hkey, subkey in keys_to_check:
        try:
            with winreg.OpenKey(hkey, subkey) as key:
                num_subkeys, _, _ = winreg.QueryInfoKey(key)
                for i in range(num_subkeys):
                    try:
                        app_key_name = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, app_key_name) as app_key:
                            try:
                                path, _ = winreg.QueryValueEx(app_key, "")
                                if path and os.path.exists(path) and path.lower().endswith(".exe"):
                                    name = os.path.splitext(app_key_name)[0]
                                    apps.append({"name": name, "path": path, "source": "registry"})
                            except OSError:
                                pass
                    except OSError:
                        pass
        except OSError:
            pass
    return apps


def get_path_apps():
    apps = []
    path_env = os.environ.get("PATH", "")
    for p in path_env.split(os.pathsep):
        if not os.path.exists(p) or not os.path.isdir(p):
            continue
        try:
            for file in os.listdir(p):
                if file.lower().endswith(".exe"):
                    name = os.path.splitext(file)[0]
                    apps.append({"name": name, "path": os.path.join(p, file), "source": "system_path"})
        except Exception:
            pass
    return apps


def build_app_cache():
    apps = []
    apps.extend(get_powershell_startapps())
    apps.extend(get_registry_app_paths())
    apps.extend(get_start_menu_lnks())
    apps.extend(get_path_apps())

    unique_apps = {}
    source_counts = {}
    for app in apps:
        norm_name = app["name"].lower()
        if not norm_name or norm_name in unique_apps:
            continue
        unique_apps[norm_name] = {
            "name": norm_name,
            "display_name": app["name"],
            "path": app["path"],
            "source": app["source"],
        }
        source_counts[app["source"]] = source_counts.get(app["source"], 0) + 1

    print(f"[app_launcher] Cache built — {sum(source_counts.values())} apps: {source_counts}")

    result = list(unique_apps.values())
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    return result


def _load_cache():
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return []


def refresh_if_stale():
    try:
        if not os.path.exists(CACHE_PATH):
            build_app_cache()
            return
        if time.time() - os.path.getmtime(CACHE_PATH) > CACHE_MAX_AGE_SECONDS:
            build_app_cache()
    except Exception as e:
        print(f"[app_launcher] cache refresh failed: {e}")


def find_app(query: str):
    """Returns a launchable path/AppID for the best-matching app, or None."""
    apps = _load_cache()
    if not apps:
        return None
    query = query.lower()

    for a in apps:
        if a["name"] == query or a["display_name"].lower() == query:
            return a["path"]

    matches = difflib.get_close_matches(query, [a["name"] for a in apps], n=1, cutoff=0.4)
    if matches:
        for a in apps:
            if a["name"] == matches[0]:
                return a["path"]

    for a in apps:
        if query in a["name"] or a["name"] in query:
            return a["path"]

    return None


def get_suggestions(query: str, n: int = 3):
    apps = _load_cache()
    if not apps:
        return []
    return difflib.get_close_matches(query, [a["display_name"] for a in apps], n=n, cutoff=0.2)


def launch_app(query: str) -> str:
    """
    Resolves query to a path and launches it. This is the actual execution
    step the original repo left inline in jarvis_local.py's tool dispatch —
    pulled out here as its own function so ember_core.py can call it
    directly (and optionally gate it through ember_confirmation first).
    """
    if not IS_WINDOWS:
        return "App launching is only supported on Windows."

    refresh_if_stale()
    path = find_app(query)
    if not path:
        suggestions = get_suggestions(query)
        if suggestions:
            return f"Couldn't find '{query}'. Did you mean: {', '.join(suggestions)}?"
        return f"Couldn't find an app matching '{query}'."

    try:
        if path.startswith("shell:AppsFolder"):
            os.startfile(path)  # noqa: S606 — Windows-only, path resolved from local Start Menu enumeration
        else:
            try:
                subprocess.Popen([path])
            except OSError as e:
                # Real, reproduced failure: "notepad" resolved to a real,
                # existing .exe-suffixed path, but launching it via
                # subprocess.Popen threw WinError 193 ("not a valid Win32
                # application"). Modern Windows ships several built-in
                # apps (Notepad included, post-modernization) as APP
                # EXECUTION ALIASES — small reparse-point stub files that
                # pass an os.path.exists()+".exe" check but aren't real PE
                # binaries, so CreateProcess (what subprocess.Popen uses)
                # rejects them outright. os.startfile() goes through
                # ShellExecute instead, which resolves execution aliases
                # correctly — falling back to it here fixes exactly this
                # class of app without weakening the normal-.exe path.
                if getattr(e, "winerror", None) == 193:
                    os.startfile(path)  # noqa: S606 — same allowlisted, locally-resolved path as above
                else:
                    raise
        return f"Launched {query}."
    except Exception as e:
        return f"Found '{query}' but failed to launch it: {e}"
