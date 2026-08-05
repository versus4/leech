import os
import pathlib
import re
import time

WORKDIR = pathlib.Path(os.environ.get("LEECH_WORKDIR", "workspace")).resolve()
TRASH = WORKDIR / ".leech-trash"
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
              ".leech-trash"}
_TEXT_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".go", ".rs",
              ".rb", ".php", ".c", ".cc", ".cpp", ".h", ".hpp", ".java", ".kt", ".swift",
              ".cs", ".sh", ".ps1", ".html", ".htm", ".css", ".scss", ".json", ".yaml",
              ".yml", ".toml", ".md", ".sql", ".lua", ".txt", ".cfg", ".ini", ".env"}


def _resolve(path):
    WORKDIR.mkdir(parents=True, exist_ok=True)
    p = (WORKDIR / path).resolve()
    if p != WORKDIR and WORKDIR not in p.parents:
        raise ValueError("path escapes the workspace")
    return p


_seen_hashes = {}


def _hash(text):
    import hashlib
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def note_content(path, text):
    _seen_hashes[str(path)] = _hash(text)


def _changed_since_read(path, p):
    """True when the file on disk differs from what the agent last read. A turn
    can span minutes; overwriting an edit the user made in that window is silent
    data loss that no amount of routing accuracy prevents."""
    prior = _seen_hashes.get(str(path))
    if prior is None:
        return False
    try:
        return _hash(p.read_text(encoding="utf-8", errors="replace")) != prior
    except Exception:
        return False


def read_file(path):
    p = _resolve(path)
    if not p.is_file():
        return "Error: file not found: " + path
    text = p.read_text(encoding="utf-8", errors="replace")
    note_content(path, text)
    lines = text.splitlines()
    numbered = "\n".join("%4d | %s" % (i + 1, l) for i, l in enumerate(lines))
    return "File: %s (%d lines)\n\n%s" % (path, len(lines), numbered)


def write_file(path, content):
    p = _resolve(path)
    if _changed_since_read(path, p):
        _seen_hashes.pop(str(path), None)
        return ("Error: %s changed on disk since it was read. Re-read it and reapply "
                "the change so the newer edit is not lost." % path)
    p.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if p.is_file() and p.read_text(encoding="utf-8", errors="replace") != content:
        import shutil
        backup = _trash_dest(p)
        shutil.copy2(str(p), str(backup))
        _evict_trash()
    p.write_text(content, encoding="utf-8")
    note_content(path, content)
    invalidate_listing()
    if backup is not None:
        return "Wrote %d lines to %s (previous version: %s)" % (
            content.count("\n") + 1, path,
            os.path.relpath(backup, WORKDIR).replace("\\", "/"))
    return "Wrote %d lines to %s" % (content.count("\n") + 1, path)


def append_file(path, content):
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(content)
    return "Appended to %s" % path


def edit_file(path, old_string, new_string, replace_all=False):
    p = _resolve(path)
    if not p.is_file():
        return "Error: file not found: " + path
    if _changed_since_read(path, p):
        _seen_hashes.pop(str(path), None)
        return ("Error: %s changed on disk since it was read. Re-read it and reapply "
                "the change so the newer edit is not lost." % path)
    content = p.read_text(encoding="utf-8", errors="replace")
    if old_string == new_string:
        return "Error: old_string and new_string are identical"
    located = old_string
    if old_string not in content:
        located = _fuzzy(content, old_string)
        if located is None:
            return "Error: old_string not found in %s" % path
    count = content.count(located)
    if count > 1 and not replace_all:
        return "Error: old_string matches %d places; add context or set replace_all" % count
    if replace_all:
        content = content.replace(located, new_string)
    else:
        content = content.replace(located, new_string, 1)
    p.write_text(content, encoding="utf-8")
    note_content(path, content)
    return "Edited %s (%d occurrence%s)" % (path, count if replace_all else 1,
                                            "s" if (replace_all and count != 1) else "")


def _fuzzy(content, old):
    if not old:
        return None
    lines = content.splitlines(keepends=True)
    old_lines = old.splitlines()
    n = len(old_lines)
    if n == 0 or n > len(lines):
        return None
    def norm(s):
        return "".join(ch for ch in s if ch.isalnum())
    for tf in (str.rstrip, str.strip, norm):
        tgt = [tf(x) for x in old_lines]
        if tf is norm and not any(tgt):
            continue
        hits = []
        for i in range(len(lines) - n + 1):
            if all(tf(lines[i + j].rstrip("\r\n")) == tgt[j] for j in range(n)):
                start = sum(len(x) for x in lines[:i])
                end = start + sum(len(x) for x in lines[i:i + n]) - (
                    len(lines[i + n - 1]) - len(lines[i + n - 1].rstrip("\r\n")))
                hits.append(content[start:end])
        uniq = set(hits)
        if len(uniq) == 1:
            return hits[0]
        if len(uniq) > 1:
            return None
    return None


def append_only(path, content):
    return append_file(path, content)


TRASH_MAX_ENTRIES = int(os.environ.get("LEECH_TRASH_MAX", "50"))


def _evict_trash():
    """Keep the newest TRASH_MAX_ENTRIES timestamp dirs. Every overwrite banks a
    copy, so without a cap the workspace grows without bound."""
    import shutil
    if not TRASH.is_dir():
        return
    stamps = sorted((d for d in TRASH.iterdir() if d.is_dir()), key=lambda d: d.name)
    for old in stamps[:-TRASH_MAX_ENTRIES] if len(stamps) > TRASH_MAX_ENTRIES else []:
        shutil.rmtree(old, ignore_errors=True)


def _trash_dest(p):
    rel = os.path.relpath(p, WORKDIR).replace("\\", "/")
    dest = TRASH / time.strftime("%Y%m%d-%H%M%S") / rel
    if dest.exists():
        dest = dest.with_name("%s.%d" % (dest.name, int(time.time() * 1000) % 100000))
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def _to_trash(p):
    """Move `p` under .leech-trash/<timestamp>/<original-relative-path>, keeping the
    workspace layout so a restore is a plain move back. Returns the trash path.
    A wrongly-routed delete has to stay recoverable -- this is the whole point."""
    import shutil
    dest = _trash_dest(p)
    shutil.move(str(p), str(dest))
    _evict_trash()
    invalidate_listing()
    return dest


def delete_file(path):
    p = _resolve(path)
    if TRASH == p or TRASH in p.parents:
        return "Error: refusing to delete from the trash: " + path
    if not p.exists():
        return "Error: not found: " + path
    kind = "directory " if p.is_dir() else ""
    dest = _to_trash(p)
    return "Deleted %s%s (recoverable: %s)" % (
        kind, path, os.path.relpath(dest, WORKDIR).replace("\\", "/"))


def restore_file(trash_path):
    """Move something back out of .leech-trash to where it came from."""
    import shutil
    src = _resolve(trash_path)
    if TRASH not in src.parents:
        return "Error: not a trash path: " + trash_path
    rel = os.path.relpath(src, TRASH).replace("\\", "/").split("/", 1)
    if len(rel) != 2:
        return "Error: not a restorable trash entry: " + trash_path
    dst = _resolve(rel[1])
    if dst.exists():
        return "Error: destination already exists: " + rel[1]
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    invalidate_listing()
    return "Restored %s -> %s" % (trash_path, rel[1])


def list_dir(path=".", depth=2):
    base = _resolve(path)
    if not base.exists():
        return "Error: not found: " + path
    out = []
    def walk(d, prefix, level):
        if level > depth:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda e: (e.is_file(), e.name))
        except Exception:
            return
        for e in entries:
            if e.name in _SKIP_DIRS:
                continue
            out.append(prefix + e.name + ("/" if e.is_dir() else ""))
            if e.is_dir():
                walk(e, prefix + "  ", level + 1)
    walk(base, "", 1)
    return "\n".join(out) if out else "(empty)"


_listing_cache = {"key": None, "at": 0.0, "value": []}
LISTING_TTL = float(os.environ.get("LEECH_LISTING_TTL", "2.0"))


def invalidate_listing():
    _listing_cache["key"] = None


def workspace_files(limit=400):
    """Every path in the workspace, directories included (trailing '/') -- used
    to ground the classifier so it can only name things that actually exist.
    Cached briefly: this walks the whole tree and every classify call wants it."""
    now = time.time()
    if _listing_cache["key"] == limit and now - _listing_cache["at"] < LISTING_TTL:
        return _listing_cache["value"]
    out = _walk_workspace(limit)
    _listing_cache.update(key=limit, at=now, value=out)
    return out


def _walk_workspace(limit):
    WORKDIR.mkdir(parents=True, exist_ok=True)
    out = []
    def rel(p, suffix=""):
        return os.path.relpath(p, WORKDIR).replace("\\", "/") + suffix
    for root, dirs, files in os.walk(WORKDIR):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in sorted(dirs):
            out.append(rel(pathlib.Path(root) / name, "/"))
        for name in sorted(files):
            out.append(rel(pathlib.Path(root) / name))
        if len(out) >= limit:
            return out[:limit]
    return out


def move_file(path, new_path):
    src = _resolve(path)
    dst = _resolve(new_path)
    if not src.exists():
        return "Error: not found: " + path
    if dst.exists():
        return "Error: destination exists: " + new_path
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    invalidate_listing()
    return "Moved %s -> %s" % (path, new_path)


def copy_file(path, new_path):
    import shutil
    src = _resolve(path)
    dst = _resolve(new_path)
    if not src.exists():
        return "Error: not found: " + path
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    invalidate_listing()
    return "Copied %s -> %s" % (path, new_path)


def make_dir(path):
    p = _resolve(path)
    p.mkdir(parents=True, exist_ok=True)
    invalidate_listing()
    return "Created directory %s" % path


def glob_files(pattern, path="."):
    base = _resolve(path)
    out = []
    for p in sorted(base.rglob(pattern)):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        out.append(os.path.relpath(p, WORKDIR).replace("\\", "/"))
    return "\n".join(out) if out else "No files matching %r" % pattern


def run_command(command, timeout=120):
    import subprocess
    WORKDIR.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(command, shell=True, cwd=str(WORKDIR), capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "Error: command timed out after %ds" % timeout
    except Exception as e:
        return "Error: %s" % e
    out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    out = out.strip() or "(no output)"
    return "exit %d\n%s" % (r.returncode, out[:8000])


def grep_search(pattern, path=".", case_insensitive=False, max_results=200):
    base = _resolve(path)
    try:
        rx = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
    except re.error:
        rx = re.compile(re.escape(pattern), re.IGNORECASE if case_insensitive else 0)
    hits = []
    targets = [base] if base.is_file() else None
    if targets is None:
        targets = []
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
            for name in files:
                if pathlib.Path(name).suffix.lower() in _TEXT_EXTS:
                    targets.append(pathlib.Path(root) / name)
    for f in targets:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        rel = os.path.relpath(f, WORKDIR).replace("\\", "/")
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append("%s:%d: %s" % (rel, i, line.strip()[:200]))
                if len(hits) >= max_results:
                    return "\n".join(hits) + "\n... (truncated)"
    return "\n".join(hits) if hits else "No matches for %r" % pattern


TOOLS = {
    "read_file": read_file,
    "write_file": write_file,
    "append_file": append_file,
    "edit_file": edit_file,
    "delete_file": delete_file,
    "restore_file": restore_file,
    "list_dir": list_dir,
    "grep_search": grep_search,
    "move_file": move_file,
    "copy_file": copy_file,
    "make_dir": make_dir,
    "glob_files": glob_files,
    "run_command": run_command,
}
