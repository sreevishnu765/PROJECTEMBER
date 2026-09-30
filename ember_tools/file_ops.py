"""
ember_tools/file_ops.py
========================
"Move the drivetrain pdf to Documents" / "rename it to final" / "delete that
file" — file operations by NAME, so nobody has to type a full path.

Where this sits: ember_tools/computer.py already owns the permission
boundary (the allowlist, is_path_allowed) for listing/reading/running. This
module reuses that boundary unchanged and adds only three things on top:

  1. Resolution — turning a spoken name into exactly one real file/folder.
     Candidates come from (a) files Ember already knows about (uploads, PDF
     exports, downloads, via FileRegistry) and (b) a bounded walk of the
     ALLOWLISTED directories only. Matching is fuzzy on purpose (typos like
     "comparision", "the drivetrain pdf", missing extensions all work), but
     Ember never guesses between near-equals: two plausible files means it
     asks, with the folders listed, instead of acting.
  2. Extra protection that the allowlist alone doesn't give. The allowlist
     includes the project root by default, so without this Ember could move
     its own source, .env, auth tokens or databases. Inside the project only
     data/uploads, data/exports, data/drive_downloads and data/screenshots
     are operable; everything else there is refused.
  3. Recoverability instead of confirmation prompts. Nothing is ever
     overwritten or permanently deleted: a move/rename onto an existing name
     is refused; "delete" moves the file to data/trash/ (after a real
     confirmation showing the exact path); and every operation is journaled
     so "undo that" reverses it.

Everything here returns plain-language results and never raises.
"""

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ember_tools import computer

_PROJECT_ROOT = computer._PROJECT_ROOT
TRASH_DIR = _PROJECT_ROOT / "data" / "trash"
JOURNAL_PATH = _PROJECT_ROOT / "data" / "file_ops.json"
JOURNAL_CAP = 30

OPERABLE_PROJECT_SUBDIRS = ("uploads", "exports", "drive_downloads", "screenshots")
SKIP_DIRS = {
    ".git", "node_modules", "venv", ".venv", "__pycache__", "site-packages",
    "$RECYCLE.BIN", "System Volume Information", "embedding_models", "trash",
}
MAX_DEPTH = 5
MAX_SCANNED = 30_000
MIN_SCORE = 0.62
AMBIGUITY_MARGIN = 0.06
_INVALID_NAME_CHARS = set('<>:"/\\|?*')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

_lock = threading.Lock()


# ---------------------------------------------------------------- results ----

@dataclass
class Resolution:
    status: str                       # "ok" | "none" | "ambiguous" | "blocked"
    path: "Path | None" = None
    candidates: "list[Path]" = field(default_factory=list)
    message: str = ""


@dataclass
class OpResult:
    ok: bool
    message: str
    op: str = ""
    src: "str | None" = None          # where the file WAS (for the registry / journal)
    dst: "str | None" = None          # where it IS now
    is_dir: bool = False              # the thing acted on was a folder (registry needs prefix updates)


# ------------------------------------------------------- request detection ----

_FILE_WORD = re.compile(r"\b(?:files?|folders?|directory|directories)\b", re.I)
_FILE_TYPE_WORD = re.compile(
    r"\b(?:pdf|docx?|xlsx?|pptx?|csv|txt|png|jpe?g|zip|screenshot|image|photo|picture|spreadsheet|document|presentation)s?\b", re.I)
_FILENAME_TOKEN = re.compile(r"\S+\.[A-Za-z][A-Za-z0-9]{1,4}\b")
_PATHLIKE = re.compile(r"[A-Za-z]:[\\/]|~[\\/]|\\\\|/[\w.-]+/")
_WELL_KNOWN_WORD = re.compile(r"\b(?:desktop|downloads?|documents|pictures|music|videos)\b", re.I)


_APP_NAME = re.compile(r"^\s*(?:the\s+|my\s+)?(?:windows\s+|file\s+)?(?:explorer|manager|files)(?:\s+app)?\s*$", re.I)


def is_app_name(tail: str) -> bool:
    """'open file explorer' / 'open files' mean the Windows APP, not a file named that —
    they contain file-ish words but must keep going to the app launcher."""
    return bool(_APP_NAME.match(_clean(tail) or tail or "")) or bool(_APP_NAME.match(tail or ""))


_PRONOUN_LEAD = re.compile(r"^\s*(?:it|that|this|them)\s+(?:to|into|as)\s+", re.I)


def pronoun_request(tail: str) -> bool:
    """'it to archive' / 'that as final': a bare pronoun as the thing being moved
    or renamed. Only honoured when there IS a recent file for it to refer to (the
    caller checks that), so it can't hijack e.g. 'move that to friday' otherwise."""
    return bool(_PRONOUN_LEAD.match(tail or ""))


def looks_like_file_request(text: str) -> bool:
    """Guard used by the tool registry. "move the meeting to friday" or "move
    this conversation to my phone" must NOT reach a file tool; a real file
    request carries at least one file-ish signal (the word file/folder, a
    filename with an extension, a type word like pdf/screenshot, a path, or a
    well-known folder)."""
    return bool(
        _FILE_WORD.search(text) or _FILENAME_TOKEN.search(text) or _FILE_TYPE_WORD.search(text)
        or _PATHLIKE.search(text) or _WELL_KNOWN_WORD.search(text)
    )


# ----------------------------------------------------------- name matching ----

_FILLER = re.compile(r"^(?:the|my|this|that|a|an|file|named|called|document)\b\s*", re.I)
_PRONOUNS = {
    "", "it", "that", "this", "them", "that one", "this one", "last one", "the last one",
    "last file", "latest file", "the file i just uploaded", "the file i uploaded", "the uploaded file",
    "the one i just uploaded",
}
_TYPE_WORDS = {
    "pdf": {".pdf"},
    "image": {".png", ".jpg", ".jpeg", ".webp", ".gif"}, "picture": {".png", ".jpg", ".jpeg", ".webp", ".gif"},
    "photo": {".png", ".jpg", ".jpeg", ".webp", ".gif"}, "screenshot": {".png", ".jpg", ".jpeg"},
    "spreadsheet": {".xlsx", ".xls", ".csv"}, "presentation": {".pptx", ".ppt"},
    "doc": {".doc", ".docx"}, "docx": {".docx"}, "word": {".doc", ".docx"}, "txt": {".txt"}, "csv": {".csv"},
}


def _clean(q: str) -> str:
    q = (q or "").strip().strip("\"'`").strip(" .!?,")
    while True:
        n = _FILLER.sub("", q).strip()
        if n == q:
            break
        q = n
    return q.strip("\"'` ")


def is_pronoun(q: str) -> bool:
    return _clean(q).lower() in _PRONOUNS


def _tokens(s: str) -> "list[str]":
    return re.findall(r"[a-z0-9]+", s.lower())


def _strip_type_word(q: str) -> "tuple[str, set | None]":
    """'drivetrain pdf' -> ('drivetrain', {'.pdf'}); 'the pdf' -> ('', {'.pdf'})."""
    words = q.split()
    if words and words[-1].lower().rstrip("s") in _TYPE_WORDS and len(words[-1]) <= 12:
        return " ".join(words[:-1]), _TYPE_WORDS[words[-1].lower().rstrip("s")]
    return q, None


def _score(query: str, name: str, is_dir: bool = False) -> float:
    q, n = query.lower().strip(), name.lower()
    q_stem, q_ext = os.path.splitext(q)
    n_stem, n_ext = os.path.splitext(n)
    if is_dir:
        base_q, base_n = q, n      # folder names have no extension ("v2.1" is just a name)
    elif q_ext and len(q_ext) <= 6 and q_ext[1:].isalnum() and not q_ext[1:].isdigit():
        if q == n:
            return 1.0
        if q_ext != n_ext:
            return 0.0
        base_q, base_n = q_stem, n_stem
    else:
        base_q, base_n = q, n_stem
    if base_q == base_n:
        return 1.0
    qt, nt = _tokens(base_q), _tokens(base_n)
    if not qt or not nt:
        return 0.0
    if set(qt) == set(nt):
        return 0.98   # same words as the file name, just formatted differently
    if all(t in nt for t in qt):
        return 0.92 - min(0.1, 0.01 * (len(nt) - len(qt)))
    if all(any(t in x for x in nt) for t in qt):
        return 0.8
    return difflib.SequenceMatcher(None, " ".join(qt), " ".join(nt)).ratio() * 0.95


# ------------------------------------------------------- safety & scanning ----

def is_protected(path: Path) -> "str | None":
    """Reason string if `path` is off-limits for moving/renaming/deleting, else None."""
    try:
        p = Path(path).resolve()
        rel = p.relative_to(Path(_PROJECT_ROOT).resolve())
    except (ValueError, OSError):
        return None  # outside the project: the allowlist is the only boundary
    parts = rel.parts
    if len(parts) >= 2 and parts[0] == "data" and parts[1] in OPERABLE_PROJECT_SUBDIRS:
        return None
    return "that's part of Ember's own project files"


def _roots() -> "list[Path]":
    roots = []
    for r in computer._load_allowlist():
        try:
            roots.append(Path(r).expanduser().resolve())
        except OSError:
            continue
    # drop roots nested inside another root so nothing is scanned twice
    return [r for r in roots if not any(o != r and o in r.parents for o in roots)]


def _walk(want_dirs: bool):
    """Yields files (or folders) under the allowlisted roots — bounded by depth
    and total entries so a huge drive can't stall a turn. Protected project
    areas and noise directories are never yielded."""
    scanned = 0
    project = Path(_PROJECT_ROOT).resolve()
    for root in _roots():
        if want_dirs and is_protected(root) is None:
            yield root
        base_depth = len(root.parts)
        for dirpath, dirs, files in os.walk(root, topdown=True):
            here = Path(dirpath)
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            if here.resolve() == project:
                dirs[:] = [d for d in dirs if d == "data"]
            elif here.resolve() == project / "data":
                dirs[:] = [d for d in dirs if d in OPERABLE_PROJECT_SUBDIRS]
            if len(here.parts) - base_depth >= MAX_DEPTH:
                dirs[:] = []
            if want_dirs:
                for d in dirs:
                    child = here / d
                    if is_protected(child) is None:
                        scanned += 1
                        yield child
            elif is_protected(here) is None:
                for f in files:
                    scanned += 1
                    yield here / f
            scanned += 1
            if scanned > MAX_SCANNED:
                return


def _pretty(paths: "list[Path]") -> str:
    return "\n".join(f"  {i}. {p.name}{' (folder)' if p.is_dir() else ''} — in {p.parent}" for i, p in enumerate(paths[:5], 1))


def _not_allowed_msg(p: Path) -> str:
    return (
        f"'{p}' isn't on my allowed-paths list, sir — say \"allow access to {p if p.is_dir() else p.parent}\" "
        "first if you want me to work there."
    )


def _pick(scored: "list[tuple[float, Path]]", label: str) -> Resolution:
    scored = [(s, p) for s, p in scored if s >= MIN_SCORE]
    if not scored:
        return Resolution("none", message=f"I couldn't find {label} in the folders I'm allowed to use, sir.")
    scored.sort(key=lambda sp: (-sp[0], -_mtime(sp[1])))
    top_score, top = scored[0]
    close = [p for s, p in scored if top_score - s <= AMBIGUITY_MARGIN]
    uniq = []
    for p in close:
        if p not in uniq:
            uniq.append(p)
    if len(uniq) > 1:
        return Resolution(
            "ambiguous", candidates=uniq,
            message=f"More than one match for {label}, sir — which did you mean? Add the folder or extension:\n{_pretty(uniq)}",
        )
    return Resolution("ok", path=top)


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


# -------------------------------------------------------------- resolvers ----

_DIR_WORD_TAIL = re.compile(r"\s+(?:folder|directory)$", re.I)
_DIR_WORD_HEAD = re.compile(r"^(?:folder|directory)\s+", re.I)
_WELL_KNOWN_NAMES = ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")


def _query_has_ext(q: str) -> bool:
    ext = os.path.splitext(q)[1]
    return bool(ext) and len(ext) <= 6 and ext[1:].isalnum() and not ext[1:].isdigit()


def _dir_unsafe_reason(p: Path) -> "str | None":
    """Folders are riskier than files: moving/renaming/deleting the wrong one
    can take Ember, a whole drive or the user's standard folders with it."""
    try:
        p = p.resolve()
        if p.parent == p:
            return "that's a drive root"
        roots = _roots()
        if any(p == r for r in roots):
            return "that's one of the top-level folders I'm allowed to use"
        if any(p in r.parents for r in roots):
            return "it contains folders I'm allowed to use"
        home = Path.home().resolve()
        if p == home or p in home.parents:
            return "that's your home folder"
        for name in _WELL_KNOWN_NAMES:
            for base in (home, home / "OneDrive"):
                if p == (base / name).resolve():
                    return f"'{name}' is one of your standard Windows folders"
        project = Path(_PROJECT_ROOT).resolve()
        if p == project or p in project.parents:
            return "it contains Ember itself"
        if p.parent == project / "data" and p.name in OPERABLE_PROJECT_SUBDIRS:
            return "Ember needs that folder"
    except OSError:
        return "I can't inspect that folder safely"
    return None


def _checked_item(p: Path, kinds: str, modify: bool = True) -> Resolution:
    if p.is_file():
        if kinds == "dir":
            return Resolution("none", message=f"'{p.name}' is a file, not a folder, sir.")
    elif p.is_dir():
        if kinds == "file":
            return Resolution("none", message=f"'{p.name}' is a folder, sir — I was expecting a file.")
        reason = _dir_unsafe_reason(p) if modify else None
        if reason:
            return Resolution("blocked", message=f"I won't touch the folder '{p.name}', sir — {reason}.")
    else:
        return Resolution("none", message=f"'{p}' doesn't exist, sir.")
    if not computer.is_path_allowed(str(p)):
        return Resolution("blocked", message=_not_allowed_msg(p))
    reason = is_protected(p)
    if reason:
        return Resolution("blocked", message=f"I won't touch '{p.name}', sir — {reason}.")
    return Resolution("ok", path=p)


def resolve_item(query: str, known_paths=(), last_file: "str | None" = None, kinds: str = "any", modify: bool = True) -> Resolution:
    """Resolve a spoken name to exactly one file or folder. kinds: "any" | "file" | "dir".
    modify=False (opening/revealing) skips the dangerous-folder rules that only matter
    when something is about to be moved, renamed or deleted."""
    q = _clean(query)
    if q.lower() in ("folder", "directory"):          # "that folder" -> filler stripped -> bare word
        q, kinds = "", ("dir" if kinds in ("any", "dir") else kinds)
    elif _DIR_WORD_TAIL.search(q) or _DIR_WORD_HEAD.match(q):
        q = _DIR_WORD_HEAD.sub("", _DIR_WORD_TAIL.sub("", q)).strip()
        kinds = "dir" if kinds in ("any", "dir") else kinds

    def kind_ok(p: Path) -> bool:
        return (p.is_file() and kinds != "dir") or (p.is_dir() and kinds != "file")

    known = []
    for kp in known_paths or ():
        try:
            pp = Path(kp).resolve()
            if kind_ok(pp):
                known.append(pp)
        except (OSError, TypeError):
            continue

    if q.lower() in _PRONOUNS:
        what = {"any": "file or folder", "file": "file", "dir": "folder"}[kinds]
        for cand in ([last_file] if last_file else []) + [str(k) for k in known]:
            try:
                if cand and Path(cand).exists() and kind_ok(Path(cand)):
                    return _checked_item(Path(cand).resolve(), kinds, modify)
            except OSError:
                pass
        return Resolution("none", message=f"I don't have a {what} in mind, sir — which one did you mean?")

    if _PATHLIKE.search(q):
        p = Path(q).expanduser()
        if not p.exists():
            return Resolution("none", message=f"There's nothing at '{q}', sir.")
        if not computer.is_path_allowed(str(p)):
            return Resolution("blocked", message=_not_allowed_msg(p.resolve()))
        return _checked_item(p.resolve(), kinds, modify)

    name_q, exts = _strip_type_word(q)
    if exts or _query_has_ext(name_q):
        kinds = "file" if kinds == "any" else kinds     # a type word / extension means "a file"
    seen, scored = set(), []

    def consider(p: Path, bonus: float):
        key = str(p)
        if key in seen:
            return
        seen.add(key)
        is_dir = p.is_dir() if key in known_set else key in dir_set
        if is_dir:
            if kinds == "file":
                return
        elif exts and p.suffix.lower() not in exts:
            return
        score = 0.7 if not name_q else _score(name_q, p.name, is_dir=is_dir)   # "the pdf": recency decides
        if score > 0:
            scored.append((min(1.0, score + bonus), p))

    known_set = {str(k) for k in known}
    dir_set: set = set()
    for p in known:
        if is_protected(p) is None and computer.is_path_allowed(str(p)):
            consider(p, 0.03)
    if kinds != "dir":
        for p in _walk(want_dirs=False):
            consider(p.resolve(), 0.0)
    if kinds != "file":
        for d in _walk(want_dirs=True):
            d = d.resolve()
            dir_set.add(str(d))
            consider(d, 0.0)

    label = f"'{q}'" if q else "that"
    res = _pick(scored, label)
    if res.status == "ok" and not name_q and exts and len(scored) > 1:
        res = Resolution("ok", path=max((p for _, p in scored), key=_mtime))
    if res.status == "ok":
        return _checked_item(res.path, kinds, modify)
    return res


def resolve_file(query: str, known_paths=(), last_file: "str | None" = None) -> Resolution:
    """Files only (kept for callers that must never act on a folder)."""
    return resolve_item(query, known_paths, last_file, kinds="file")


def _well_known_dir(word: str) -> "Path | None":
    home = Path.home()
    for base in (home, home / "OneDrive"):
        cand = base / word.capitalize()
        if cand.is_dir():
            return cand
    return None


def resolve_folder(query: str) -> Resolution:
    """A DESTINATION folder (may be a protected-free anything-allowed folder,
    including top-level roots — unlike a folder being moved/renamed/deleted)."""
    q = _clean(query)
    q = re.sub(r"^(?:folder|directory)\s+", "", q, flags=re.I)
    q = re.sub(r"\s+(?:folder|directory)$", "", q, flags=re.I).strip()
    if not q:
        return Resolution("none", message="Which folder, sir?")

    if _PATHLIKE.search(q):
        p = Path(q).expanduser()
        if not p.is_dir():
            return Resolution("none", message=f"There's no folder at '{q}', sir.")
        return _checked_folder(p.resolve())

    if q.lower() in ("desktop", "downloads", "download", "documents", "pictures", "music", "videos"):
        name = "downloads" if q.lower() == "download" else q.lower()
        wk = _well_known_dir(name)
        if wk is not None:
            return _checked_folder(wk.resolve())

    scored, seen = [], set()
    for d in _walk(want_dirs=True):
        d = d.resolve()
        if str(d) in seen:
            continue
        seen.add(str(d))
        s = _score(q, d.name, is_dir=True)
        if s > 0:
            scored.append((s, d))
    return _pick(scored, f"a folder called '{q}'")


def _checked_folder(p: Path) -> Resolution:
    if not computer.is_path_allowed(str(p)):
        return Resolution("blocked", message=_not_allowed_msg(p))
    reason = is_protected(p)
    if reason:
        return Resolution("blocked", message=f"I won't put files in '{p.name}', sir — {reason}.")
    return Resolution("ok", path=p)


# ---------------------------------------------------------------- journal ----

def _journal_load() -> "list[dict]":
    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _journal_save(entries: "list[dict]") -> None:
    try:
        Path(JOURNAL_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(JOURNAL_PATH, "w", encoding="utf-8") as f:
            json.dump(entries[-JOURNAL_CAP:], f, indent=2)
    except OSError as e:
        print(f"[file_ops] Warning: couldn't persist the undo journal ({e}).")


def _journal_add(op: str, src: str, dst: str) -> None:
    with _lock:
        entries = _journal_load()
        entries.append({"id": uuid.uuid4().hex[:8], "op": op, "src": src, "dst": dst, "at": time.time(), "undone": False})
        _journal_save(entries)


# --------------------------------------------------------------- operations ----

def _splits(tail: str, words: "tuple[str, ...]") -> "list[tuple[str, str]]":
    pattern = r"\s+(?:" + "|".join(words) + r")\s+"
    return [(tail[:m.start()], tail[m.end():]) for m in re.finditer(pattern, tail, re.I)][::-1]


def _resolve_left(splits, known, last_file):
    """Try each connector position (last first) until the left side resolves to
    exactly one file/folder; otherwise report the first failure so the message is useful."""
    first_fail = None
    for left, right in splits:
        res = resolve_item(left, known, last_file)
        if res.status == "ok":
            return res, right
        if first_fail is None:
            first_fail = res
    return first_fail, None


def _kind_word(p: Path) -> str:
    return "folder" if p.is_dir() else "file"


def move_file(tail: str, known_paths=(), last_file: "str | None" = None) -> OpResult:
    """Moves a file OR folder into a destination folder (named in `tail`)."""
    try:
        splits = _splits(tail, ("into", "to"))
        if not splits:
            return OpResult(False, "Move it where, sir? Say e.g. 'move drivetrain.pdf to Documents'.", "move")
        res, dest_q = _resolve_left(splits, known_paths, last_file)
        if res.status != "ok":
            return OpResult(False, res.message, "move")
        dres = resolve_folder(dest_q)
        if dres.status != "ok":
            return OpResult(False, dres.message, "move")

        src, dest_dir = res.path, dres.path
        is_dir = src.is_dir()
        if is_dir and (dest_dir == src or src in dest_dir.parents):
            return OpResult(False, f"I can't move the folder '{src.name}' into itself, sir.", "move")
        final = dest_dir / src.name
        if final == src:
            return OpResult(False, f"'{src.name}' is already in {dest_dir}, sir.", "move")
        if final.exists():
            return OpResult(False, f"{dest_dir} already has a {_kind_word(final)} named '{src.name}', sir — I won't overwrite it. Rename one first.", "move")
        shutil.move(str(src), str(final))
        _journal_add("move", str(src), str(final))
        return OpResult(True, f"Moved the {_kind_word(final)} '{src.name}' from {src.parent} to {dest_dir}, sir. (Say \"undo that\" to reverse it.)",
                        "move", str(src), str(final), is_dir)
    except OSError as e:
        return OpResult(False, f"Couldn't move that, sir: {e}", "move")


def _valid_new_name(name: str, src: Path) -> "tuple[str | None, str | None]":
    name = (name or "").strip().strip("\"'`").strip(" .!?,")
    name = re.sub(r"^(?:the\s+name\s+|named\s+|called\s+)", "", name, flags=re.I).strip("\"'` ")
    if not name:
        return None, "Rename it to what, sir?"
    if any(c in _INVALID_NAME_CHARS for c in name):
        return None, "A name can't contain any of  < > : \" / \\ | ? *  — try a simpler one, sir."
    if src.is_dir():
        stem = name            # folders have no extension to keep
    else:
        stem, ext = os.path.splitext(name)
        if not ext and src.suffix:
            name = name + src.suffix   # "rename report.pdf to final" keeps .pdf
            stem = os.path.splitext(name)[0]
    if stem.upper() in _RESERVED or name.endswith((" ", ".")):
        return None, f"'{name}' isn't a name Windows allows, sir."
    if len(name) > 200:
        return None, "That name is too long, sir."
    return name, None


def rename_file(tail: str, known_paths=(), last_file: "str | None" = None) -> OpResult:
    """Renames a file OR folder in place."""
    try:
        splits = _splits(tail, ("to", "as"))
        if not splits:
            return OpResult(False, "Rename it to what, sir? Say e.g. 'rename notes.txt to todo'.", "rename")
        res, new_q = _resolve_left(splits, known_paths, last_file)
        if res.status != "ok":
            return OpResult(False, res.message, "rename")
        src = res.path
        new_name, err = _valid_new_name(new_q, src)
        if err:
            return OpResult(False, err, "rename")
        target = src.with_name(new_name)
        if target.name == src.name:
            return OpResult(False, f"It's already called '{src.name}', sir.", "rename")
        if target.exists() and not os.path.samefile(target, src):
            return OpResult(False, f"There's already something called '{new_name}' in {src.parent}, sir — I won't overwrite it.", "rename")
        is_dir = src.is_dir()
        src.rename(target)
        _journal_add("rename", str(src), str(target))
        return OpResult(True, f"Renamed the {_kind_word(target)} '{src.name}' to '{new_name}', sir. (Say \"undo that\" to reverse it.)",
                        "rename", str(src), str(target), is_dir)
    except OSError as e:
        return OpResult(False, f"Couldn't rename that, sir: {e}", "rename")


TRASH_MAX_ITEMS = 20_000
TRASH_MAX_BYTES = 2 * 1024 ** 3


def _dir_stats(p: Path) -> "tuple[int, int, bool]":
    """(files, bytes, too_big). Bounded walk: stops at the caps instead of crawling a huge tree."""
    files = total = 0
    for dirpath, _dirs, names in os.walk(p):
        for n in names:
            files += 1
            try:
                total += os.path.getsize(os.path.join(dirpath, n))
            except OSError:
                pass
            if files > TRASH_MAX_ITEMS or total > TRASH_MAX_BYTES:
                return files, total, True
    return files, total, False


def _human(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.0f} KB" if n < 1024 ** 2 else f"{n / 1024 ** 2:.1f} MB" if n < 1024 ** 3 else f"{n / 1024 ** 3:.1f} GB"


def trash_file(tail: str, confirm_fn, known_paths=(), last_file: "str | None" = None) -> OpResult:
    """Deletes a file OR folder by moving it to data/trash/ — recoverable.
    `confirm_fn(path_str)` (files) / `confirm_fn(path_str, detail)` (folders) is called with
    the exact resolved path BEFORE anything happens; a missing or failing confirmation means
    no deletion (fail closed). Folders over the size caps are refused: copying a huge tree
    into the trash (possibly across drives) isn't a safe "undoable" delete."""
    try:
        res = resolve_item(tail, known_paths, last_file)
        if res.status != "ok":
            return OpResult(False, res.message, "delete")
        src = res.path
        is_dir = src.is_dir()
        try:
            if is_dir:
                files, size, too_big = _dir_stats(src)
                if too_big:
                    return OpResult(False, f"The folder '{src.name}' is too big for me to trash safely, sir "
                                           f"(over {TRASH_MAX_ITEMS} files or {_human(TRASH_MAX_BYTES)}) — delete it in File Explorer.", "delete")
                approved = bool(confirm_fn(str(src), f"folder with {files} file(s), {_human(size)}"))
            else:
                approved = bool(confirm_fn(str(src)))
        except Exception:
            approved = False
        if not approved:
            return OpResult(False, f"Understood, sir — '{src.name}' was not deleted.", "delete")
        Path(TRASH_DIR).mkdir(parents=True, exist_ok=True)
        dest = Path(TRASH_DIR) / f"{int(time.time())}_{uuid.uuid4().hex[:6]}_{src.name}"
        shutil.move(str(src), str(dest))
        _journal_add("delete", str(src), str(dest))
        return OpResult(True, f"Deleted the {'folder' if is_dir else 'file'} '{src.name}', sir — it's in Ember's trash, so \"undo that\" brings it back.",
                        "delete", str(src), str(dest), is_dir)
    except OSError as e:
        return OpResult(False, f"Couldn't delete that, sir: {e}", "delete")


def undo_last() -> OpResult:
    try:
        with _lock:
            entries = _journal_load()
            entry = next((e for e in reversed(entries) if not e.get("undone")), None)
            if entry is None:
                return OpResult(False, "There's nothing to undo, sir.", "undo")
            cur, orig = Path(entry["dst"]), Path(entry["src"])
            if not cur.exists():
                entry["undone"] = True
                _journal_save(entries)
                return OpResult(False, f"I can't undo that, sir — '{cur.name}' isn't where I left it any more.", "undo")
            if orig.exists():
                return OpResult(False, f"I can't undo that, sir — something else is now at {orig}.", "undo")
            is_dir = cur.is_dir()
            orig.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(cur), str(orig))
            entry["undone"] = True
            _journal_save(entries)
        verb = {"move": "Moved back", "rename": "Renamed back", "delete": "Restored"}.get(entry["op"], "Reversed")
        return OpResult(True, f"{verb}: {orig}, sir.", "undo", str(cur), str(orig), is_dir)
    except OSError as e:
        return OpResult(False, f"Couldn't undo that, sir: {e}", "undo")


# ------------------------------------------------------------------- opening ----
# Opening hands the path to the OS default handler, which for a script/installer
# means RUNNING it. Those types are never opened; they're shown in File Explorer
# instead (the user can still double-click them on purpose).
OPEN_BLOCKED_EXTS = {
    ".exe", ".dll", ".msi", ".scr", ".com", ".bat", ".cmd", ".vbs", ".vbe", ".lnk", ".ps1", ".psm1",
    ".py", ".pyw", ".js", ".jse", ".jar", ".sh", ".reg", ".hta", ".wsf", ".cpl", ".msc", ".url", ".appref-ms",
}


def _native_open(path: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa: S606 — path was resolved/validated by the caller
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def _native_reveal(path: str) -> None:
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", f"/select,{path}"])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", str(Path(path).parent)])


def open_path(path: str, reveal: bool = False) -> OpResult:
    """Opens (or reveals in the file manager) an already-resolved path. Used by the
    Files panel (a direct user click on an entry Ember recorded) and by open_item."""
    try:
        p = Path(path)
        if not p.exists():
            return OpResult(False, f"'{p.name}' isn't at {p.parent} any more, sir — it may have been moved or deleted.", "open")
        if not reveal and p.is_file() and p.suffix.lower() in OPEN_BLOCKED_EXTS:
            _native_reveal(str(p))
            return OpResult(True, f"I don't launch '{p.suffix}' files, sir — I've shown '{p.name}' in File Explorer instead.", "open", None, str(p), False)
        if reveal:
            _native_reveal(str(p))
            return OpResult(True, f"Showing '{p.name}' in File Explorer, sir.", "open", None, str(p), p.is_dir())
        _native_open(str(p))
        return OpResult(True, f"Opened the {_kind_word(p)} '{p.name}', sir.", "open", None, str(p), p.is_dir())
    except OSError as e:
        return OpResult(False, f"Couldn't open that, sir: {e}", "open")


def open_item(tail: str, known_paths=(), last_file: "str | None" = None, reveal: bool = False) -> OpResult:
    res = resolve_item(tail, known_paths, last_file, modify=False)
    if res.status != "ok":
        return OpResult(False, res.message, "open")
    return open_path(str(res.path), reveal=reveal)
