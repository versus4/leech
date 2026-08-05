"""Model-based intent detection: one constrained call, one line back.

Replaces keyword regexes. Works in any language because the model does the
understanding; the format is too simple to come back malformed.
"""
import inspect
import os
import re

from . import direct
from .tools import TOOLS, _resolve, workspace_files

ENABLED = os.environ.get("LEECH_CLASSIFY", "1") not in ("0", "false", "")
MODEL = os.environ.get("LEECH_CLASSIFY_MODEL", "fast")

NO_ONE_LINE = {"write_file", "append_file", "edit_file", "run_command"}

CREATES = {"make_dir"}


def _routable(fn):
    """Routing args = required params plus string-valued ones. Numeric/bool
    knobs (depth, timeout, replace_all, max_results) are not worth routing."""
    out = []
    for name, p in inspect.signature(fn).parameters.items():
        if p.default is inspect.Parameter.empty or isinstance(p.default, str):
            out.append(name)
    return tuple(out)


SIGS = {n: _routable(f) for n, f in TOOLS.items() if n not in NO_ONE_LINE}
REQUIRED = {n: tuple(k for k, p in inspect.signature(TOOLS[n]).parameters.items()
                     if p.default is inspect.Parameter.empty) for n in SIGS}
MENU = "\n".join("%s %s" % (n, " ".join(k + "=" for k in ks)) for n, ks in sorted(SIGS.items()))

PROMPT = """You route a user request to one tool. Reply with ONE line, nothing else.

Format:  tool_name key=value key=value
Or:      none

Tools:
%s

Rules:
- Values run to the next ` key=` or end of line. Never quote them. Never use newlines.
- path= must be copied exactly from the file list below, or `.` for the whole workspace.
- If the request is a question, is hypothetical, is negated, needs a file that is not
  listed, or needs writing new file content, reply `none`.

Files in the workspace:
%s

Request (any language): %s"""

_LINE = re.compile(r"^\s*([a-z_]+)\b(.*)$", re.I)
_STRAY_KEY = re.compile(r"\s[a-z_]{2,20}=")


def _pick_line(reply):
    """Models sometimes fence the answer or prefix it with chatter. Find the
    first line that starts with a known tool name, stripping any lead-in."""
    for raw in (reply or "").replace("`", "").splitlines():
        s = raw.strip()
        if not s:
            continue
        m = _LINE.match(s)
        if m and m.group(1).lower() in SIGS:
            return s
        for name in SIGS:
            i = s.find(name + " ")
            if i >= 0:
                return s[i:]
    return ""


def _parse(reply, files):
    line = _pick_line(reply)
    m = _LINE.match(line)
    if not m:
        return None
    name = m.group(1).lower()
    if name not in SIGS or name not in TOOLS:
        return None
    rest = m.group(2)
    keys = SIGS[name]
    parts = re.split(r"\s+(%s)=" % "|".join(keys), " " + rest.strip())
    args = {}
    for i in range(1, len(parts) - 1, 2):
        v = parts[i + 1].strip()
        if v:
            args[parts[i]] = v
    if not all(k in args for k in REQUIRED[name]):
        return None
    for v in args.values():
        if _STRAY_KEY.search(v):
            return None
    p = args.get("path")
    if p and p != "." and name not in CREATES and not _known(p, files):
        return None
    for k in ("path", "new_path"):
        if k in args and not _inside(args[k]):
            return None
    return name, args


def _known(p, files):
    return p in files or p.rstrip("/") + "/" in files


def _inside(p):
    try:
        _resolve(p)
        return True
    except Exception:
        return False


async def detect(message, model=None):
    """Return (tool_name, args) or None. Never raises."""
    if not ENABLED or not (message or "").strip():
        return None
    files = workspace_files()
    prompt = PROMPT % (MENU, "\n".join(files) or "(empty)", message.strip())
    try:
        reply = await direct.complete(MODEL or model or "default", prompt=prompt)
    except Exception:
        return None
    try:
        return _parse(reply, set(files))
    except Exception:
        return None
