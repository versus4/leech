"""Blast-radius gate in front of tool execution.

Routing decides WHAT to run; this decides whether running it unattended is
acceptable. Better routing lowers the misfire rate but never the cost of one
misfire, so the destructive tools ask before they act.
"""
import logging
import os
import re

log = logging.getLogger("permissions")

ALLOW = "allow"
DENY = "deny"

MODE_ASK = "ask"
MODE_AUTO = "auto"
MODE_READONLY = "readonly"

READ_TOOLS = frozenset({"read_file", "list_dir", "grep_search", "glob_files"})
WRITE_TOOLS = frozenset({"write_file", "append_file", "edit_file", "delete_file",
                         "move_file", "copy_file", "make_dir", "restore_file"})
EXEC_TOOLS = frozenset({"run_command"})

HARD_DENY = [
    "rm -rf /", "rm -rf /*", "rm -fr /", "rm -rf ~", "rm -rf ~/*",
    ":(){ :|:& };:", "mkfs", "dd if=/dev/zero of=/dev/", "> /dev/sda",
    "del /f /s /q c:\\", "del /s /q c:\\", "del /q /s c:\\",
    "del /f /s /q %systemroot%", "del /f /s /q %windir%", "del /f /s /q %userprofile%",
    "rd /s /q c:\\", "rmdir /s /q c:\\", "rd /s /q %systemroot%",
    "rd /s /q %windir%", "rd /s /q %userprofile%",
    "format c:", "format /y", "cipher /w:c",
]

_SEG = re.compile(r"&&|\|\||[;|&\n]")
_WRAPPER = re.compile(r"^\s*(?:sudo|doas|env|nohup|time|command|builtin)\s+", re.I)

_mode = os.environ.get("LEECH_PERMISSION_MODE", MODE_ASK)
_handler = None


def set_mode(mode):
    global _mode
    if mode in (MODE_ASK, MODE_AUTO, MODE_READONLY):
        _mode = mode
    return _mode


def get_mode():
    return _mode


def set_handler(fn):
    """Install the confirmation prompt. `fn(tool_name, args, preview) -> bool`.
    Without one, MODE_ASK denies rather than blocking on a console nobody is
    watching -- a server has no keyboard."""
    global _handler
    _handler = fn


def category(tool_name):
    if tool_name in READ_TOOLS:
        return "read"
    if tool_name in EXEC_TOOLS:
        return "exec"
    if tool_name in WRITE_TOOLS:
        return "write"
    return "write"


def _segments(command):
    out = []
    for raw in _SEG.split(command or ""):
        seg = _WRAPPER.sub("", raw.strip())
        while _WRAPPER.match(seg):
            seg = _WRAPPER.sub("", seg)
        if seg:
            out.append(seg.lower())
    return out


def is_hard_denied(tool_name, args):
    if tool_name not in EXEC_TOOLS:
        return False
    for seg in _segments((args or {}).get("command", "")):
        norm = " ".join(seg.split())
        for pattern in HARD_DENY:
            if norm.startswith(pattern) or pattern in norm:
                return True
    return False


def describe(tool_name, args):
    """One line the user can actually judge. A confirmation prompt that only names
    the tool trains people to click yes; showing the target and its size does not."""
    args = args or {}
    path = args.get("path")
    if tool_name == "run_command":
        return "run: %s" % args.get("command", "")
    if tool_name in ("move_file", "copy_file"):
        return "%s: %s -> %s" % (tool_name, path, args.get("new_path"))
    if tool_name == "delete_file" and path:
        return "delete: %s%s (recoverable from .leech-trash)" % (path, _size_of(path))
    if tool_name == "write_file" and path:
        return "overwrite: %s%s" % (path, _size_of(path))
    if path:
        return "%s: %s" % (tool_name, path)
    return tool_name


def _size_of(path):
    try:
        from .tools import _resolve
        p = _resolve(path)
        if p.is_file():
            return " (%d bytes)" % p.stat().st_size
        if p.is_dir():
            return " (directory, %d entries)" % sum(1 for _ in p.rglob("*"))
    except Exception:
        pass
    return ""


def check(tool_name, args):
    """ALLOW or DENY. The only question asked before a tool runs."""
    if is_hard_denied(tool_name, args):
        return DENY
    cat = category(tool_name)
    if cat == "read":
        return ALLOW
    if _mode == MODE_READONLY:
        return DENY
    if _mode == MODE_AUTO:
        return ALLOW
    if _handler is None:
        log.error(
            "DENIED %s: mode is %r but no confirmation handler is installed, so every "
            "write is refused. Call permissions.set_handler(fn) to confirm interactively, "
            "or set_mode(MODE_AUTO) / LEECH_PERMISSION_MODE=auto to run unattended.",
            tool_name, _mode)
        return DENY
    try:
        return ALLOW if _handler(tool_name, args or {}, describe(tool_name, args)) else DENY
    except Exception:
        return DENY
