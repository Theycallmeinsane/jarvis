"""File system by voice: browse folders, read and summarize files, create, rename, copy, move, tidy up,
find duplicates, see what's using disk space, zip and unzip.

Safety rules (see `files:` in config.yaml):
  * Deleting only ever moves things to the Recycle Bin, and always asks first, naming the item.
  * Renames, moves and tidying a folder ask first; bulk actions say how many files first.
  * Writes are limited to `files.allowed_roots` (default: your user folder and non-system drives).
  * Windows, Program Files, ProgramData, AppData, the drive roots and Jarvis's own folder are read-only.
  * Nothing is ever overwritten: a copy or extracted folder gets a new name instead.
Spoken names don't need to be exact: "my downloads", "the D drive", "resume pdf in documents".
"""
from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import logging
import os
import re
import shutil
import string
import time
import zipfile
from pathlib import Path

from ..config import ROOT
from .builtin import ToolFailed
from .registry import Spoken, tool

log = logging.getLogger(__name__)

TRIGGERS_READ = ("file", "folder", "director", "drive", "disk", "space", "storage", "download", "document",
                 "desktop", "picture", "photo", "music", "video", "read", "summar", "open", "what's in", "whats in",
                 "what is in", "list", "show", "biggest", "largest", "duplicate", "size", "pdf", "doc", "txt",
                 "zip", "in my", "content")
SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__", "$recycle.bin", "system volume information"}
PARTIAL = (".crdownload", ".part", ".partial", ".tmp", ".download", ".opdownload")
TYPE_GROUPS = {
    "Documents": {".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".md", ".xls", ".xlsx", ".csv", ".ppt", ".pptx",
                  ".epub"},
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".svg", ".tif", ".tiff", ".ico"},
    "Videos": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".flv"},
    "Music": {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".wma", ".opus"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"},
    "Installers": {".exe", ".msi", ".msix", ".appx", ".bat", ".cmd"},
    "Code": {".py", ".js", ".ts", ".html", ".css", ".json", ".yaml", ".yml", ".java", ".c", ".cpp", ".cs",
             ".go", ".rs", ".ipynb", ".sql", ".sh", ".ps1"},
}
TEXT_EXT = {".txt", ".md", ".csv", ".log", ".json", ".yaml", ".yml", ".ini", ".cfg", ".toml", ".xml", ".html",
            ".htm", ".css", ".rtf", ".srt", ".bat", ".cmd", ".ps1", ".sh"} | TYPE_GROUPS["Code"]


# ------------------------------------------------------------------ known folders and spoken paths

def _shell_folder(reg_name: str, fallback: str) -> Path:
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
                return Path(os.path.expandvars(winreg.QueryValueEx(k, reg_name)[0]))
        except OSError:
            pass
    return Path.home() / fallback


def known_folders() -> dict[str, Path]:
    """Spoken folder names -> real paths (follows OneDrive / moved folders on Windows)."""
    f = {
        "home": Path.home(), "user folder": Path.home(), "my files": Path.home(),
        "desktop": _shell_folder("Desktop", "Desktop"),
        "documents": _shell_folder("Personal", "Documents"),
        "downloads": _shell_folder("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"),
        "pictures": _shell_folder("My Pictures", "Pictures"),
        "music": _shell_folder("My Music", "Music"),
        "videos": _shell_folder("My Video", "Videos"),
    }
    f.update({"photos": f["pictures"], "movies": f["videos"], "docs": f["documents"], "download": f["downloads"],
              "document": f["documents"], "video": f["videos"], "picture": f["pictures"]})
    return f


def _norm(s: str) -> str:
    return re.sub(r"[\s_.()-]+", " ", s.lower()).strip()


def _child_match(parent: Path, name: str) -> Path | None:
    """Best child of `parent` for a spoken name: exact, every spoken word in the name, then close spelling."""
    exact = parent / name
    if exact.exists():
        return exact
    try:
        children = [c for c in parent.iterdir()]
    except OSError:
        return None
    words = _norm(name).split()
    if not words:
        return None
    hits = [c for c in children if all(w in _norm(c.name) for w in words)]
    if hits:
        return min(hits, key=lambda c: len(c.name))       # "resume" -> Resume.pdf before Resume_old_v2.pdf
    by_norm = {_norm(c.name): c for c in children}
    by_stem = {_norm(c.stem): c for c in children}
    for table in (by_stem, by_norm):
        close = difflib.get_close_matches(_norm(name), list(table), n=1, cutoff=0.75)
        if close:
            return table[close[0]]
    return None


def _drive(token: str) -> Path | None:
    m = re.fullmatch(r"(?:the )?(?:([a-z]) drive|drive ([a-z])|([a-z]):[\\/]?)", token.strip().lower())
    if not m:
        return None
    letter = next(g for g in m.groups() if g)
    return Path(f"{letter.upper()}:\\") if os.name == "nt" else Path("/")


def resolve(spoken: str, cfg=None, must_exist: bool = True) -> Path:
    """'my downloads', 'D drive', 'resume pdf in documents', 'C:\\x\\y', '~/x' -> a real path."""
    text = (spoken or "").strip().strip("'\"")
    if not text:
        raise ToolFailed("Which file or folder?")
    t = re.sub(r"^(?:the|my|in|inside|from|on)\s+", "", text, flags=re.I)
    t = re.sub(r"\s+(?:folder|directory)$", "", t, flags=re.I).strip()
    folders = known_folders()
    p: Path | None = None
    expanded = os.path.expandvars(os.path.expanduser(t))
    if os.path.isabs(expanded) or re.match(r"^[a-zA-Z]:[\\/]", expanded):
        p = Path(expanded)
        if not p.exists() and must_exist:
            parent = p.parent
            fixed = _child_match(parent, p.name) if parent.exists() else None
            p = fixed or p
    elif t.lower() in folders:
        p = folders[t.lower()]
    elif _drive(t):
        p = _drive(t)
    else:
        m = re.fullmatch(r"(?P<item>.+?)\s+(?:in|inside|from|on|under)\s+(?:the\s+|my\s+)?(?P<where>.+)", t, re.I)
        if m:
            base = resolve(m.group("where"), cfg)
            if must_exist:
                p = _child_match(base, m.group("item")) if base.is_dir() else None
            else:                                   # creating: only an exact name counts as "already there"
                p = base / m.group("item")
            if p is None and must_exist:
                p = _search(m.group("item"), [base], cfg) or None
            if p is None:
                if must_exist:
                    raise ToolFailed(f"I couldn't find {m.group('item')} in {base.name or base}.")
                p = base / m.group("item")
        else:
            parts = re.split(r"[\\/]", t)
            head = parts[0].lower()
            base = folders.get(head) or _drive(head)
            if base is not None and len(parts) > 1:
                p = base
                for i, part in enumerate(parts[1:], 2):
                    last_new = not must_exist and i == len(parts)
                    nxt = _child_match(p, part) if p.is_dir() and not last_new else None
                    if nxt is None:
                        if must_exist:
                            raise ToolFailed(f"I couldn't find {part} in {p.name or p}.")
                        nxt = p / part
                    p = nxt
            elif not must_exist:                    # a new item named on its own goes in Documents
                p = folders["documents"] / t
            else:
                p = _search(t, None, cfg)
                if p is None:
                    raise ToolFailed(f"I couldn't find a file or folder called {t}.")
    if must_exist and not p.exists():
        raise ToolFailed(f"{p} doesn't exist.")
    return p


def _search(name: str, roots: list[Path] | None, cfg, budget: float = 3.0) -> Path | None:
    """Find a file or folder by spoken name under the usual folders, newest first."""
    if roots is None:
        folders = known_folders()
        roots = [folders[k] for k in ("desktop", "documents", "downloads", "pictures", "music", "videos")]
        if cfg is not None:
            from ..config import resolve_path

            roots += [resolve_path(d) for d in (cfg.get("tools") or {}).get("search_dirs") or []]
    words = _norm(name).split()
    hits: list[Path] = []
    deadline = time.monotonic() + budget
    for root in dict.fromkeys(roots):
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith(".")]
            for n in dirnames + filenames:
                if all(w in _norm(n) for w in words):
                    hits.append(Path(dirpath) / n)
            if time.monotonic() > deadline or len(hits) > 50:
                break
    if not hits:
        return None

    def score(p: Path):
        exact = _norm(p.stem) == " ".join(words) or _norm(p.name) == " ".join(words)
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0
        return (not exact, -mtime)
    return sorted(hits, key=score)[0]


# ------------------------------------------------------------------ where Jarvis may write

def _system_drive() -> Path:
    return Path((os.environ.get("SystemDrive") or "C:") + "\\") if os.name == "nt" else Path("/")


def _fixed_drives() -> list[Path]:
    if os.name != "nt":
        return []
    import ctypes

    mask = ctypes.windll.kernel32.GetLogicalDrives()
    drives = []
    for i, letter in enumerate(string.ascii_uppercase):
        if mask >> i & 1:
            root = f"{letter}:\\"
            if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:      # DRIVE_FIXED
                drives.append(Path(root))
    return drives


def allowed_roots(cfg) -> list[Path]:
    roots = (cfg.get("files") or {}).get("allowed_roots", "auto") if cfg is not None else "auto"
    if roots in (None, "auto", ["auto"]):
        sysdrive = _system_drive()
        return [Path.home()] + [d for d in _fixed_drives() if d != sysdrive]
    return [Path(os.path.expandvars(os.path.expanduser(r))) for r in roots]


def protected_paths(cfg) -> list[Path]:
    sysdrive = _system_drive()
    env = os.environ
    found = [Path(env.get("SystemRoot", sysdrive / "Windows")), Path(env.get("ProgramFiles", sysdrive / "Program Files")),
             Path(env.get("ProgramFiles(x86)", sysdrive / "Program Files (x86)")),
             Path(env.get("ProgramData", sysdrive / "ProgramData")), ROOT]
    for d in _fixed_drives() or [sysdrive]:
        found += [d / "$Recycle.Bin", d / "System Volume Information", d / "Recovery"]
    return found + _soft_protected(cfg)


def _soft_protected(cfg) -> list[Path]:
    """Protected unless you allowed a folder inside them on purpose (e.g. a scratch folder under %TEMP%)."""
    extra = (cfg.get("files") or {}).get("protected") or [] if cfg is not None else []
    return [Path.home() / "AppData"] + [Path(os.path.expandvars(os.path.expanduser(p))) for p in extra]


def _explicit_roots(cfg) -> list[Path]:
    roots = (cfg.get("files") or {}).get("allowed_roots", "auto") if cfg is not None else "auto"
    return [] if roots in (None, "auto", ["auto"]) else allowed_roots(cfg)


def _inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def check_writable(p: Path, cfg, what: str = "change") -> None:
    """Raise ToolFailed (said to the user) unless Jarvis may create/modify/remove `p`."""
    rp = p.resolve()
    soft = {s.resolve() for s in _soft_protected(cfg)}
    for prot in protected_paths(cfg):
        if _inside(rp, prot):
            if prot.resolve() in soft and any(_inside(r, prot) and _inside(rp, r) for r in _explicit_roots(cfg)):
                continue                      # a folder you allowed inside it wins over the general rule
            raise ToolFailed(f"{prot} is a protected folder, so I won't {what} anything in it.")
    if rp == rp.parent or rp == Path.home().resolve() or any(rp == r.resolve() for r in allowed_roots(cfg)):
        raise ToolFailed(f"I won't {what} {rp} itself, only things inside it.")
    if not any(_inside(rp, r) for r in allowed_roots(cfg)):
        raise ToolFailed(f"{rp} is outside the folders I'm allowed to {what}. "
                         "You can add it under files: allowed_roots in your settings.")


def _free_name(p: Path) -> Path:
    """p, or 'name (2).ext', 'name (3).ext'... so nothing is ever overwritten."""
    if not p.exists():
        return p
    for i in range(2, 1000):
        cand = p.with_name(f"{p.stem} ({i}){p.suffix}") if p.suffix else p.with_name(f"{p.name} ({i})")
        if not cand.exists():
            return cand
    raise ToolFailed(f"Too many copies of {p.name} already.")


def human_size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("bytes", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _when(ts: float) -> str:
    d = dt.datetime.fromtimestamp(ts)
    today = dt.date.today()
    if d.date() == today:
        return "today " + d.strftime("%H:%M")
    if (today - d.date()).days == 1:
        return "yesterday"
    return d.strftime("%d %b %Y")


def _stat(p: Path):
    try:
        return p.stat()
    except OSError:
        return None


def _tree_size(p: Path, deadline: float) -> tuple[int, int, bool]:
    """(bytes, files, complete) for a folder, giving up at the deadline."""
    total = files = 0
    for dirpath, dirnames, filenames in os.walk(p):
        dirnames[:] = [d for d in dirnames if d.lower() not in ("$recycle.bin", "system volume information")]
        for f in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
                files += 1
            except OSError:
                pass
        if time.monotonic() > deadline:
            return total, files, False
    return total, files, True


PATH_PARAM = {"type": "string", "description": "Folder or file as the user said it: 'downloads', 'D drive', "
                                               "'invoices in documents', or a full path"}


# ------------------------------------------------------------------ read-only tools

@tool("List what's in a folder (files and subfolders), newest, largest or by name.",
      {"path": PATH_PARAM, "sort": {"type": "string", "enum": ["newest", "largest", "name"]},
       "count": {"type": "integer", "description": "How many to list (default 12)"}},
      required=["path"], triggers=TRIGGERS_READ)
def list_folder(path: str, sort: str = "newest", count: int = 12, ctx=None):
    p = resolve(path, ctx.cfg if ctx else None)
    if not p.is_dir():
        raise ToolFailed(f"{p.name} is a file, not a folder.")
    entries = []
    try:
        for c in p.iterdir():
            st = _stat(c)
            if st is not None and not c.name.lower() in ("desktop.ini", "thumbs.db"):
                entries.append((c, st))
    except PermissionError:
        raise ToolFailed(f"Windows won't let me look inside {p.name}.") from None
    dirs = sum(1 for c, _ in entries if c.is_dir())
    key = {"largest": lambda e: -e[1].st_size if not e[0].is_dir() else 1,
           "name": lambda e: e[0].name.lower()}.get(sort, lambda e: -e[1].st_mtime)
    entries.sort(key=key)
    head = f"{p} has {len(entries) - dirs} files and {dirs} folders."
    if not entries:
        return Spoken(f"{p.name or p} is empty.")
    lines = [head] + [
        f"- {c.name}{'/' if c.is_dir() else ''}" +
        ("" if c.is_dir() else f" ({human_size(st.st_size)})") + f", {_when(st.st_mtime)}"
        for c, st in entries[:max(1, min(count, 40))]]
    if len(entries) > count:
        lines.append(f"...and {len(entries) - count} more.")
    return "\n".join(lines)


@tool("Show what's using disk space: free space on the drive and the biggest subfolders of a folder.",
      {"path": PATH_PARAM}, required=["path"], triggers=TRIGGERS_READ)
def disk_usage(path: str, ctx=None):
    p = resolve(path, ctx.cfg if ctx else None)
    if not p.is_dir():
        raise ToolFailed(f"{p.name} is a file; it uses {human_size(p.stat().st_size)}.")
    du = shutil.disk_usage(p)
    lines = [f"{p.anchor or p}: {human_size(du.free)} free of {human_size(du.total)}."]
    deadline = time.monotonic() + 8
    sizes, complete = [], True
    loose = 0
    try:
        children = list(p.iterdir())
    except PermissionError:
        raise ToolFailed(f"Windows won't let me look inside {p.name}.") from None
    for c in children:
        if time.monotonic() > deadline:
            complete = False
            break
        if c.is_dir() and not c.is_symlink():
            size, _, done = _tree_size(c, deadline)
            complete &= done
            sizes.append((size, c.name))
        else:
            st = _stat(c)
            loose += st.st_size if st else 0
    sizes.sort(reverse=True)
    lines.append(f"Biggest folders in {p.name or p}:")
    lines += [f"- {name}: {human_size(size)}" for size, name in sizes[:8] if size]
    if loose:
        lines.append(f"- files directly in it: {human_size(loose)}")
    if not complete:
        lines.append("(Stopped early: that's a lot of files, so these are lower bounds.)")
    return "\n".join(lines)


@tool("Find the biggest files inside a folder (including its subfolders).",
      {"path": PATH_PARAM, "count": {"type": "integer", "description": "How many (default 8)"}},
      required=["path"], triggers=TRIGGERS_READ)
def largest_files(path: str, count: int = 8, ctx=None):
    p = resolve(path, ctx.cfg if ctx else None)
    found: list[tuple[int, Path]] = []
    deadline = time.monotonic() + 8
    complete = True
    for dirpath, dirnames, filenames in os.walk(p):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
        for f in filenames:
            fp = Path(dirpath) / f
            st = _stat(fp)
            if st:
                found.append((st.st_size, fp))
        if time.monotonic() > deadline:
            complete = False
            break
    if not found:
        return Spoken(f"There are no files in {p.name or p}.")
    found.sort(reverse=True)
    lines = [f"Biggest files in {p}:"] + [f"- {fp.relative_to(p)}: {human_size(s)}" for s, fp in found[:count]]
    if not complete:
        lines.append("(Searched for 8 seconds; there may be bigger ones deeper down.)")
    return "\n".join(lines)


def _read_text(p: Path, limit: int) -> str:
    ext = p.suffix.lower()
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ToolFailed("Reading PDFs needs the pypdf package; run setup.ps1 again.") from None
        reader = PdfReader(str(p))
        out = []
        for i, page in enumerate(reader.pages):
            out.append(page.extract_text() or "")
            if sum(map(len, out)) > limit:
                break
        text = "\n".join(out)
        if not text.strip():
            raise ToolFailed(f"{p.name} has no readable text; it may be a scanned image.")
        return f"[PDF, {len(reader.pages)} pages]\n" + text
    if ext == ".docx":
        try:
            import docx
        except ImportError:
            raise ToolFailed("Reading Word files needs the python-docx package; run setup.ps1 again.") from None
        return "\n".join(par.text for par in docx.Document(str(p)).paragraphs)
    if ext in TEXT_EXT or ext == "":
        raw = p.read_bytes()[: limit * 4]
        if b"\x00" in raw[:4096] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise ToolFailed(f"{p.name} isn't a text file.")
        for enc in ("utf-8-sig", "utf-16", "cp1252"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")
    raise ToolFailed(f"I can read text, PDF, Word and code files, but not {ext} files.")


@tool("Read a text, PDF, Word or code file so you can summarize it or answer questions about it. "
      "Summarize briefly for speech unless the user asks for it word for word.",
      {"path": PATH_PARAM, "max_chars": {"type": "integer", "description": "Default 6000"}},
      required=["path"], triggers=TRIGGERS_READ)
def read_file(path: str, max_chars: int = 6000, ctx=None):
    p = resolve(path, ctx.cfg if ctx else None)
    if p.is_dir():
        raise ToolFailed(f"{p.name} is a folder. Ask me what's in it instead.")
    if p.stat().st_size > 200 * 2**20:
        raise ToolFailed(f"{p.name} is {human_size(p.stat().st_size)}, too big to read aloud.")
    limit = max(1, min(max_chars, 20000))
    text = _read_text(p, limit)
    cut = len(text) > limit
    return f"Contents of {p.name}{' (first part)' if cut else ''}:\n{text[:limit]}"


@tool("Find duplicate files (same content) inside a folder and how much space they waste.",
      {"path": PATH_PARAM}, required=["path"], triggers=TRIGGERS_READ)
def find_duplicates(path: str, ctx=None):
    p = resolve(path, ctx.cfg if ctx else None)
    by_size: dict[int, list[Path]] = {}
    deadline = time.monotonic() + 10
    complete = True
    for dirpath, dirnames, filenames in os.walk(p):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
        for f in filenames:
            fp = Path(dirpath) / f
            st = _stat(fp)
            if st and st.st_size > 0:
                by_size.setdefault(st.st_size, []).append(fp)
        if time.monotonic() > deadline:
            complete = False
            break
    groups: list[tuple[int, list[Path]]] = []
    for size, files in by_size.items():
        if len(files) < 2:
            continue
        by_hash: dict[str, list[Path]] = {}
        for fp in files:
            if time.monotonic() > deadline + 10:
                complete = False
                break
            h = hashlib.blake2b(digest_size=16)
            try:
                with open(fp, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
            except OSError:
                continue
            by_hash.setdefault(h.hexdigest(), []).append(fp)
        groups += [(size, g) for g in by_hash.values() if len(g) > 1]
    if not groups:
        return Spoken(f"No duplicate files in {p.name or p}." + ("" if complete else " I stopped early, though."))
    groups.sort(key=lambda g: -g[0] * (len(g[1]) - 1))
    wasted = sum(size * (len(g) - 1) for size, g in groups)
    lines = [f"{len(groups)} sets of duplicates in {p}, wasting {human_size(wasted)}:"]
    for size, g in groups[:8]:
        lines.append(f"- {human_size(size)} each: " + ", ".join(str(x.relative_to(p)) for x in g[:4]))
    if not complete:
        lines.append("(Stopped early: there are a lot of files.)")
    return "\n".join(lines)


# ------------------------------------------------------------------ creating things (no overwrite)

@tool("Create a new folder.", {"path": {"type": "string", "description": "e.g. 'invoices in documents'"}},
      required=["path"], triggers=("create", "make", "new", "add"))
def create_folder(path: str, ctx=None):
    cfg = ctx.cfg if ctx else None
    p = resolve(path, cfg, must_exist=False)
    if p.exists():
        return Spoken(f"{p.name} already exists.")
    check_writable(p, cfg, "create")
    p.mkdir(parents=True)
    return Spoken(f"Created the folder {p.name} in {p.parent.name or p.parent}.")


@tool("Write text into a new text file (or add to the end of one).",
      {"path": {"type": "string", "description": "e.g. 'shopping list in documents' (.txt is added)"},
       "text": {"type": "string"},
       "append": {"type": "boolean", "description": "true to add to an existing file"}},
      required=["path", "text"], triggers=("write", "create", "make", "save", "new", "add", "append", "note"))
def write_text_file(path: str, text: str, append: bool = False, ctx=None):
    cfg = ctx.cfg if ctx else None
    p = resolve(path, cfg, must_exist=False)
    if not p.suffix:
        p = p.with_suffix(".txt")
    if p.suffix.lower() not in TEXT_EXT:
        raise ToolFailed("I can only write plain text files.")
    check_writable(p, cfg, "write")
    if p.exists() and not append:
        p = _free_name(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    sep = "\n" if append and p.exists() and p.stat().st_size else ""
    with open(p, "a" if append else "w", encoding="utf-8") as f:
        f.write(sep + text)
    return Spoken(f"{'Added to' if append else 'Saved'} {p.name}.")


@tool("Copy a file or folder into another folder (never overwrites: the copy gets a new name).",
      {"path": PATH_PARAM, "destination": {"type": "string", "description": "Folder to copy into"}},
      required=["path", "destination"], triggers=("copy", "duplicate", "backup", "back up"))
def copy_item(path: str, destination: str, ctx=None):
    cfg = ctx.cfg if ctx else None
    src = resolve(path, cfg)
    dest_dir = resolve(destination, cfg, must_exist=False)
    if dest_dir.exists() and not dest_dir.is_dir():
        raise ToolFailed(f"{dest_dir.name} isn't a folder.")
    target = _free_name(dest_dir / src.name)
    check_writable(target, cfg, "copy")
    if src.is_dir() and _inside(target, src):
        raise ToolFailed("I can't copy a folder into itself.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    (shutil.copytree if src.is_dir() else shutil.copy2)(src, target)
    return Spoken(f"Copied {src.name} to {dest_dir.name or dest_dir}" +
                  (f" as {target.name}." if target.name != src.name else "."))


def _confirm_paths(args: dict, verb: str, *keys: str) -> str:
    names = []
    for k in keys:
        try:
            names.append(resolve(args.get(k, ""), None, must_exist=(k == "path")).name or args.get(k))
        except ToolFailed:
            names.append(str(args.get(k, "")))
    return verb.format(*names)


@tool("Rename a file or folder. Asks first.",
      {"path": PATH_PARAM, "new_name": {"type": "string", "description": "New name (keeps the extension if omitted)"}},
      required=["path", "new_name"], dangerous=True, triggers=("rename", "name it", "call it", "change the name"),
      confirm=lambda a: _confirm_paths(a, "Shall I rename {} to " + str(a.get("new_name", "")) + "?", "path"))
def rename_item(path: str, new_name: str, ctx=None):
    cfg = ctx.cfg if ctx else None
    src = resolve(path, cfg)
    new_name = new_name.strip().strip("'\"")
    if not new_name or re.search(r'[\\/:*?"<>|]', new_name):
        raise ToolFailed("That name has characters Windows doesn't allow in file names.")
    if src.is_file() and not Path(new_name).suffix:
        new_name += src.suffix
    target = src.with_name(new_name)
    check_writable(src, cfg, "rename")
    if target.exists() and target != src:
        raise ToolFailed(f"There's already something called {new_name} there.")
    src.rename(target)
    return Spoken(f"Renamed it to {target.name}.")


@tool("Move a file or folder into another folder. Asks first. Never overwrites.",
      {"path": PATH_PARAM, "destination": {"type": "string", "description": "Folder to move it into"}},
      required=["path", "destination"], dangerous=True, triggers=("move", "put", "transfer"),
      confirm=lambda a: _confirm_paths(a, "Shall I move {} to {}?", "path", "destination"))
def move_item(path: str, destination: str, ctx=None):
    cfg = ctx.cfg if ctx else None
    src = resolve(path, cfg)
    dest_dir = resolve(destination, cfg, must_exist=False)
    if dest_dir.exists() and not dest_dir.is_dir():
        raise ToolFailed(f"{dest_dir.name} isn't a folder.")
    check_writable(src, cfg, "move")
    target = _free_name(dest_dir / src.name)
    check_writable(target, cfg, "move")
    if src.is_dir() and _inside(target, src):
        raise ToolFailed("I can't move a folder into itself.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(target))
    return Spoken(f"Moved {src.name} to {dest_dir.name or dest_dir}.")


def _delete_question(args: dict) -> str:
    try:
        p = resolve(args.get("path", ""), None)
    except ToolFailed:
        return f"Shall I move {args.get('path')} to the Recycle Bin?"
    if p.is_dir():
        size, files, done = _tree_size(p, time.monotonic() + 2)
        what = f"the folder {p.name}, with {files}{'' if done else '+'} files ({human_size(size)}),"
    else:
        what = f"{p.name} ({human_size(p.stat().st_size)}) from {p.parent.name}"
    return f"Shall I move {what} to the Recycle Bin?"


@tool("Delete a file or folder by moving it to the Recycle Bin (never permanently). Always asks first.",
      {"path": PATH_PARAM}, required=["path"], dangerous=True,
      triggers=("delete", "remove", "trash", "bin", "get rid", "erase", "throw away"), confirm=_delete_question)
def delete_item(path: str, ctx=None):
    cfg = ctx.cfg if ctx else None
    p = resolve(path, cfg)
    check_writable(p, cfg, "delete")
    try:
        from send2trash import send2trash
    except ImportError:
        raise ToolFailed("Deleting needs the send2trash package; run setup.ps1 again.") from None
    send2trash(str(p))
    return Spoken(f"Moved {p.name} to the Recycle Bin. You can restore it from there.")


def _organize_plan(folder: Path) -> dict[str, list[Path]]:
    plan: dict[str, list[Path]] = {}
    now = time.time()
    for c in folder.iterdir():
        if not c.is_file() or c.name.lower() in ("desktop.ini", "thumbs.db") or c.suffix.lower() in PARTIAL:
            continue
        st = _stat(c)
        if not st or now - st.st_mtime < 60:        # still downloading / just saved
            continue
        group = next((g for g, exts in TYPE_GROUPS.items() if c.suffix.lower() in exts), "Other")
        plan.setdefault(group, []).append(c)
    return plan


def _organize_question(args: dict) -> str:
    try:
        folder = resolve(args.get("path", ""), None)
        plan = _organize_plan(folder)
    except (ToolFailed, OSError):
        return f"Shall I sort the files in {args.get('path')} into folders by type?"
    n = sum(map(len, plan.values()))
    return (f"Shall I sort {n} files in {folder.name} into {len(plan)} folders by type "
            f"({', '.join(sorted(plan))})?")


@tool("Tidy a folder (e.g. Downloads) by sorting its loose files into subfolders by type: Documents, Images, "
      "Videos, Music, Archives, Installers, Code, Other. Asks first, saying how many files.",
      {"path": PATH_PARAM}, required=["path"], dangerous=True,
      triggers=("organi", "tidy", "sort", "clean up", "cleanup", "arrange", "declutter"), confirm=_organize_question)
def organize_folder(path: str, ctx=None):
    cfg = ctx.cfg if ctx else None
    folder = resolve(path, cfg)
    if not folder.is_dir():
        raise ToolFailed(f"{folder.name} isn't a folder.")
    plan = _organize_plan(folder)
    if not plan:
        return Spoken(f"{folder.name} has no loose files to sort.")
    check_writable(folder / "Documents", cfg, "organize")
    moved = 0
    for group, files in plan.items():
        dest = folder / group
        dest.mkdir(exist_ok=True)
        for f in files:
            try:
                shutil.move(str(f), str(_free_name(dest / f.name)))
                moved += 1
            except OSError as e:
                log.warning("organize: %s: %s", f, e)
    return Spoken(f"Sorted {moved} files in {folder.name} into {len(plan)} folders.")


@tool("Compress a file or folder into a .zip next to it.", {"path": PATH_PARAM}, required=["path"],
      triggers=("zip", "compress", "archive"))
def zip_item(path: str, ctx=None):
    cfg = ctx.cfg if ctx else None
    src = resolve(path, cfg)
    target = _free_name(src.with_name(src.name + ".zip") if src.is_dir() else src.with_suffix(".zip"))
    check_writable(target, cfg, "create")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        if src.is_dir():
            for fp in src.rglob("*"):
                if fp.is_file():
                    z.write(fp, Path(src.name) / fp.relative_to(src))
        else:
            z.write(src, src.name)
    return Spoken(f"Created {target.name}, {human_size(target.stat().st_size)}.")


@tool("Extract a .zip file into a new folder next to it.", {"path": PATH_PARAM}, required=["path"],
      triggers=("unzip", "extract", "decompress", "unpack", "open the zip"))
def unzip_item(path: str, ctx=None):
    cfg = ctx.cfg if ctx else None
    src = resolve(path, cfg)
    if src.suffix.lower() != ".zip":
        raise ToolFailed("I can only extract .zip files.")
    target = _free_name(src.with_suffix(""))
    check_writable(target, cfg, "extract")
    with zipfile.ZipFile(src) as z:
        for member in z.namelist():           # refuse "../" tricks that would write outside the folder
            if not _inside(target / member, target):
                raise ToolFailed(f"{src.name} contains unsafe paths, so I didn't extract it.")
        z.extractall(target)
    return Spoken(f"Extracted {src.name} into {target.name}.")
