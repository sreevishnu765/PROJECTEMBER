"""
ember_tools/drive.py
======================
Google Drive file/folder downloader, picked up from Nexus VII's
modules/drive_downloader.py.

What was kept as-is because it's genuinely solid: recursive folder
download via the official API (not scraping, not the pagination-limited
"export as zip" trick some Drive tools use), 5MB chunked download with
exponential-backoff retry, filename collision handling (never overwrites
an existing file — appends "(1)", "(2)", ...), and Windows-invalid-
character sanitization on every file/folder name pulled from Drive
metadata (Drive lets you name a file "reports/Q3?.pdf"; Windows does not).

Real (minor) issue found and fixed: the original decided file-vs-folder
with `"/folders/" in url or "folder" in url.lower()` — that second
condition is not just redundant (every real Drive folder-share URL
already contains "/folders/") but can misfire on a FILE link whose name
or path happens to contain the substring "folder" anywhere in the URL.
Fixed by dropping the bare substring check; folder vs. file is now
decided purely by the presence of the actual `/folders/<id>` URL segment,
same as every other URL-shape check in this file.

Design change from the original: downloads now go through
ember_tools/computer.py's allowlist rather than defaulting silently to
the user's real Downloads folder. This is the one capability in this
batch that writes an unbounded amount of new data to disk from an
external source (a shared Drive folder could contain anything, arbitrary
size, arbitrarily nested) — routing it through the same permission
boundary as read_file/run_script means a Drive download can't land
outside directories the user has explicitly approved, consistent with
the next-phase brief's "implement permission boundaries... for risky
actions" instruction. If no output_dir is given, it defaults to
data/drive_downloads/ under the project root (already allowlisted by
default) rather than the OS Downloads folder outside Ember's visibility.
"""

import io
import os
import re
import time
from pathlib import Path

from ember_tools import computer as computer_tools
from ember_tools import google_auth

_TOOLS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _TOOLS_DIR.parent
DEFAULT_DOWNLOAD_DIR = _PROJECT_ROOT / "data" / "drive_downloads"


def _sanitize_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name.strip(". ") or "untitled"


def _download_single_file(service, file_id: str, file_name: str, output_dir: str, max_retries: int = 3) -> str:
    from googleapiclient.http import MediaIoBaseDownload

    os.makedirs(output_dir, exist_ok=True)
    file_name = _sanitize_name(file_name)
    file_path = os.path.join(output_dir, file_name)

    base, ext = os.path.splitext(file_name)
    counter = 1
    while os.path.exists(file_path):
        file_path = os.path.join(output_dir, f"{base} ({counter}){ext}")
        counter += 1

    fh = None
    for attempt in range(max_retries):
        try:
            request = service.files().get_media(fileId=file_id)
            fh = io.FileIO(file_path, mode="wb")
            downloader = MediaIoBaseDownload(fh, request, chunksize=1024 * 1024 * 5)
            done = False
            while not done:
                _status, done = downloader.next_chunk()
            fh.close()
            return file_path
        except Exception as e:
            if fh:
                try:
                    fh.close()
                except Exception:
                    pass
            if os.path.exists(file_path):
                os.remove(file_path)
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise Exception(f"Failed to download {file_name} after {max_retries} attempts: {e}")


def _download_folder_recursive(service, folder_id: str, current_output_dir: str) -> int:
    os.makedirs(current_output_dir, exist_ok=True)
    downloaded_count = 0
    page_token = None

    while True:
        results = service.files().list(
            q=f"'{folder_id}' in parents",
            fields="nextPageToken, files(id, name, mimeType)",
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()

        for item in results.get("files", []):
            if item["mimeType"] == "application/vnd.google-apps.folder":
                subfolder_dir = os.path.join(current_output_dir, _sanitize_name(item["name"]))
                downloaded_count += _download_folder_recursive(service, item["id"], subfolder_dir)
            else:
                try:
                    _download_single_file(service, item["id"], item["name"], current_output_dir)
                    downloaded_count += 1
                except Exception as e:
                    print(f"[ember_tools.drive] Skipping '{item['name']}': {e}")

        page_token = results.get("nextPageToken")
        if page_token is None:
            break

    return downloaded_count


def download_drive_link(url: str, output_dir: "str | None" = None) -> "tuple[str, str | None]":
    """Downloads a file or folder from a Google Drive sharing URL. Returns
    (message, path) — never raises. path is the real destination directory
    (folder download) or file (single download) on success, None on any
    failure. Added the path return (this pass) so callers that need to
    actually DO something with the result — ember_core.py's Files-panel
    recording, in particular — don't have to scrape a path back out of the
    prose message, which is exactly what that caller was doing before this
    existed, same real gap calendar.py's add_event() already closed by
    returning (message, event_id) instead of just a string.

    If output_dir isn't given, defaults to data/drive_downloads/
    (allowlisted by default, unlike the OS Downloads folder the original
    used)."""
    output_dir = output_dir or str(DEFAULT_DOWNLOAD_DIR)
    if not computer_tools.is_path_allowed(output_dir):
        return (
            f"'{output_dir}' isn't on the allowed-paths list, sir — Drive downloads are "
            f"bounded by the same permission list as file access. Use the allow-path "
            f"command to add it first, or leave the destination unspecified to use the "
            f"default (already-allowed) download folder."
        ), None
    os.makedirs(output_dir, exist_ok=True)

    try:
        service = google_auth.get_drive_service()
    except Exception as e:
        return f"Couldn't reach Google Drive, sir: {e}", None

    try:
        folder_match = re.search(r"/folders/([a-zA-Z0-9_-]+)", url)

        if folder_match:
            folder_id = folder_match.group(1)
            try:
                folder_meta = service.files().get(fileId=folder_id, fields="name", supportsAllDrives=True).execute()
                folder_name = _sanitize_name(folder_meta.get("name", f"Drive_Folder_{folder_id}"))
            except Exception:
                folder_name = _sanitize_name(f"Drive_Folder_{folder_id}")

            folder_output_dir = os.path.join(output_dir, folder_name)
            total = _download_folder_recursive(service, folder_id, folder_output_dir)
            return f"Downloaded {total} file(s) from the Drive folder '{folder_name}' to {folder_output_dir}, sir.", folder_output_dir

        file_match = re.search(r"/d/([a-zA-Z0-9_-]+)", url) or re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
        if not file_match:
            return f"I couldn't find a file or folder ID in that URL, sir: {url}", None

        file_id = file_match.group(1)
        file_meta = service.files().get(fileId=file_id, fields="name", supportsAllDrives=True).execute()
        file_name = file_meta.get("name", f"drive_file_{file_id}")
        path = _download_single_file(service, file_id, file_name, output_dir)
        return f"Downloaded '{file_name}' to {path}, sir.", path

    except Exception as e:
        return f"Drive download failed, sir: {e}", None
