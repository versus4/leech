import inspect
import json
import re

from . import classify, direct, permissions
from .tools import TOOLS, _resolve, note_content

TOOL_DOCS = (
    "You are a coding agent working inside a project folder. You have REAL tools that "
    "execute on disk. To use one, output EXACTLY this and nothing else:\n"
    '<tool>{"name": "<tool>", "args": {...}}</tool>\n'
    "then stop and wait for the result (it comes back as \"Tool result: ...\").\n\n"
    "Tools:\n%s\n\n"
    "Rules: one tool call per message. In JSON strings a newline is \\n and a quote is "
    '\\". Never say you cannot access files -- you can, via a tool call. When the task is '
    "done, reply in plain text with a short summary."
) % "\n".join(
    "%s(%s)" % (n, ", ".join(
        k if p.default is inspect.Parameter.empty else k + "?"
        for k, p in inspect.signature(f).parameters.items()))
    for n, f in TOOLS.items())

_TOOL_TAG = re.compile(
    r"<(?:tool|tool_call|function_calls|invoke)>\s*(\{.*?\})\s*"
    r"</(?:tool|tool_call|function_calls|invoke)>", re.DOTALL)
_BARE_CALL = re.compile(
    r'^\s*(\{\s*"name"\s*:\s*"[a-z_]+"\s*,\s*"args"\s*:\s*\{.*?\}\s*\})\s*$',
    re.DOTALL | re.MULTILINE)
_FENCE = re.compile(r"```([^\n]*)\n(.*?)```", re.DOTALL)
_FILE_TOKEN = re.compile(r"([A-Za-z0-9_.\-/]+\.[A-Za-z][A-Za-z0-9]{0,6})")
_PATH_IN = re.compile(r"(?<![\w@])([A-Za-z0-9_.\-/]+\.[A-Za-z][A-Za-z0-9]{0,6})")
_ASSIGN = re.compile(r"\b([A-Za-z_][\w.\-]{0,40})\s+(?:should\s+be|must\s+be|=)\s+([^\s,;.]+)", re.I)
_SET = re.compile(r"\bset\s+(?:the\s+)?([A-Za-z_][\w.\-]{0,40})\s+to\s+([^\s,;.]+)", re.I)
_REPLACE = re.compile(r"\breplace\s+(?:the\s+)?(?:word|text|string|line)?\s*['\"]?([^'\"]{1,60}?)['\"]?\s+with\s+['\"]?([^'\"]{1,60}?)['\"]?(?=$|[\s,.;])", re.I)
_EDIT_VERB = re.compile(r"\b(?:fix|change|update|edit|rename|replace|refactor|modify|correct|adjust)\b", re.I)
_NEGATION = re.compile(r"\b(?:don['\u2019]?t|do not|never|how to|how do i)\b", re.I)
_QUOTED = re.compile(r"(['\"])(.*?)\1")
_APPEND = re.compile(r"\b(?:append|add)\b\s+(?:a\s+|the\s+|another\s+)?(?:new\s+)?(?:line|row|entry)?\s*(?:with|containing|saying|of)?\s*(?:the\s+)?(?:text|line|content|string)?\s*['\"]?([^'\"]{1,120}?)['\"]?\s+(?:to|onto|into|at\s+the\s+end\s+of|to\s+the\s+end\s+of)\s+", re.I)
_DELETE = re.compile(r"^(?:please\s+)?(?:delete|remove|rm|erase|del)\b(?![\w-])", re.I)
_LINE_WORDISH = re.compile(r"\b(?:line|word|text|string|occurrence|instance)\b", re.I)
_GREP = re.compile(r"\b(?:search|grep|look)\s+(?:for\s+)?['\"]?([^'\"]{1,80}?)['\"]?\s+(?:in|inside|within|across)\s+(.+)$", re.I)
_PROJECTISH = re.compile(r"\b(?:project|codebase|repo|repository|everything|all\s+files|the\s+code|here)\b", re.I)
_MOVE = re.compile(r"^(?:please\s+)?(?:move|rename)\s+(?:the\s+file\s+)?", re.I)
_COPY = re.compile(r"^(?:please\s+)?(?:copy|duplicate)\s+(?:the\s+file\s+)?", re.I)
_MKDIR = re.compile(r"^(?:please\s+)?(?:make|create)\s+(?:a\s+)?(?:new\s+)?(?:directory|folder|dir)\s+(?:called\s+|named\s+)?([A-Za-z0-9_.\-/]+)", re.I)
_GLOB = re.compile(r"\b(?:find|list|show)\s+(?:all\s+)?(?:the\s+)?files?\s+(?:named|called|matching|like|ending\s+in|with)\s+([A-Za-z0-9_.\-*/]+)", re.I)
_RUN = re.compile(r"^(?:please\s+)?(?:run|execute)\s+(?:the\s+command\s+|this\s*:?\s*)?(.+)$", re.I)


def _escape_controls(s):
    out, in_str, esc = [], False, False
    for ch in s:
        if in_str:
            if esc:
                out.append(ch); esc = False; continue
            if ch == "\\":
                out.append(ch); esc = True; continue
            if ch == '"':
                out.append(ch); in_str = False; continue
            if ch == "\n":
                out.append("\\n"); continue
            if ch == "\r":
                out.append("\\r"); continue
            if ch == "\t":
                out.append("\\t"); continue
            if ord(ch) < 0x20:
                out.append("\\u%04x" % ord(ch)); continue
            out.append(ch)
        else:
            if ch == '"':
                in_str = True
            out.append(ch)
    return "".join(out)


def _parse_tool_calls(text):
    calls = []
    seen = set()
    for m in list(_TOOL_TAG.finditer(text)) + list(_BARE_CALL.finditer(text)):
        if m.start() in seen:
            continue
        seen.add(m.start())
        raw = m.group(1)
        obj = None
        try:
            obj = json.loads(raw)
        except Exception:
            try:
                obj = json.loads(_escape_controls(raw))
            except Exception:
                obj = None
        if isinstance(obj, dict) and obj.get("name") in TOOLS:
            calls.append((m.group(0), obj["name"], obj.get("args") or {}))
    return calls


def _fast(msg):
    """Zero-ambiguity shortcuts only -- a latency cache in front of the classifier.
    Everything else is left to classify.detect(), which handles any language."""
    v = (msg or "").strip()
    if not v or "\n" in v:
        return None
    low = v.lower()
    if low in ("ls", "dir", "tree", "list files"):
        return "list_dir", {"path": ".", "depth": 2}
    if _fileexists(v):
        return "read_file", {"path": v}
    rn = _RUN.match(v)
    if rn and re.match(r"^(?:npm|npx|pnpm|yarn|node|python|py|pytest|pip|git|go|cargo|make|ruff|black|eslint|tsc)\b",
                       rn.group(1).strip(), re.I):
        return "run_command", {"command": rn.group(1).strip()}
    return None


_DESTRUCTIVE_FALLBACK = {"delete_file", "move_file", "copy_file", "run_command",
                         "write_file", "append_file", "edit_file"}


def _dispatch(msg):
    out = _dispatch_raw(msg)
    if out and out[0] in _DESTRUCTIVE_FALLBACK:
        return None
    return out


def _dispatch_raw(msg):
    v = (msg or "").strip()
    if not v or _NEGATION.search(v):
        return None
    paths = []
    for m in _PATH_IN.finditer(v):
        p = m.group(1)
        if p not in paths and "://" not in p and not p.startswith("/"):
            paths.append(p)
    quoted = [m.group(2) for m in _QUOTED.finditer(v)]
    low = v.lower()
    if re.match(r"^(?:please\s+)?(?:list|show)\b.*\b(?:files?|dir|directory|folder)", low) or low in ("ls", "list files"):
        return "list_dir", {"path": ".", "depth": 2}
    gm = _GREP.search(v)
    if gm:
        pat = gm.group(1).strip()
        tail = gm.group(2).strip()
        tail_paths = [m.group(1) for m in _PATH_IN.finditer(tail) if "://" not in m.group(1)]
        tgt = tail_paths[0] if tail_paths else "."
        if not tail_paths and not _PROJECTISH.search(tail):
            tgt = "."
        if pat:
            return "grep_search", {"pattern": pat, "path": tgt}
    mk = _MKDIR.match(v)
    if mk:
        return "make_dir", {"path": mk.group(1)}
    gl = _GLOB.search(v)
    if gl:
        name = gl.group(1)
        return "glob_files", {"pattern": name if ("*" in name or "/" in name) else "**/*" + name if name.startswith(".") else "**/" + name}
    if _MOVE.match(v) and len(paths) >= 2:
        return "move_file", {"path": paths[0], "new_path": paths[1]}
    if _COPY.match(v) and len(paths) >= 2:
        return "copy_file", {"path": paths[0], "new_path": paths[1]}
    rn = _RUN.match(v)
    if rn and re.match(r"^(?:npm|npx|pnpm|yarn|node|python|py|pytest|pip|git|go|cargo|make|ls|dir|echo|cat|bash|sh|ruff|black|eslint|tsc)\b", rn.group(1).strip(), re.I):
        return "run_command", {"command": rn.group(1).strip()}
    if len(paths) == 1:
        rep = _REPLACE.search(v)
        if rep and _fileexists(paths[0]):
            return "edit_file", {"path": paths[0], "old_string": rep.group(1), "new_string": rep.group(2)}
        assigns = [(m.group(1), m.group(2)) for m in _ASSIGN.finditer(v)] + \
                  [(m.group(1), m.group(2)) for m in _SET.finditer(v)]
        if assigns and _fileexists(paths[0]):
            edits = _build_assign_edits(paths[0], assigns)
            if len(edits) == 1:
                return "edit_file", {"path": paths[0], **edits[0]}
            if edits:
                return None
        if re.match(r"^(?:please\s+)?(?:read|open|show|view|cat)\b", low) and not _iswrite(low):
            return "read_file", {"path": paths[0]}
        if re.match(r"^(?:please\s+)?(?:create|make|write|save)\b", low) and quoted and not _fileexists(paths[0]):
            return "write_file", {"path": paths[0], "content": quoted[-1]}
        ap = _APPEND.search(v)
        if ap and _fileexists(paths[0]):
            body = ap.group(1).strip()
            if body:
                nl = "\n" if re.search(r"\bline\b", v, re.I) else ""
                return "append_file", {"path": paths[0], "content": nl + body}
        if _DELETE.match(v) and _fileexists(paths[0]) and not _LINE_WORDISH.search(v):
            return "delete_file", {"path": paths[0]}
    return None


def _build_assign_edits(path, assigns):
    try:
        content = _resolve(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    lines = content.splitlines()
    edits = []
    for field, val in assigns:
        rx = re.compile(r"^(\s*" + re.escape(field) + r"\s*[:=]\s*)(\S.*?)(\s*)$", re.I)
        hits = [ln for ln in lines if rx.match(ln)]
        if len(hits) != 1:
            return []
        m = rx.match(hits[0])
        old, new = m.group(0), m.group(1) + val + m.group(3)
        if old != new and content.count(old) == 1:
            edits.append({"old_string": old, "new_string": new})
    return edits


def _fileexists(path):
    try:
        return _resolve(path).is_file()
    except Exception:
        return False


def _iswrite(low):
    return bool(re.search(r"\b(?:create|make|write|save|edit|change|replace|append|delete)\b", low))


def _resolve_edit_target(msg):
    v = (msg or "").strip()
    if not v or _NEGATION.search(v) or v.endswith("?"):
        return None
    if re.search(r"\b(?:and then|then run|; )\b", v, re.I):
        return None
    if not _EDIT_VERB.search(v) and not _ASSIGN.search(v):
        return None
    paths = [m.group(1) for m in _PATH_IN.finditer(v) if "://" not in m.group(1)]
    existing = [p for p in dict.fromkeys(paths) if _fileexists(p)]
    return existing[0] if len(existing) == 1 else None


async def _transform(path, task, model):
    try:
        content = _resolve(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    note_content(path, content)
    lang = path.rsplit(".", 1)[-1] if "." in path else ""
    prompt = (
        "Here is the current content of %s:\n```%s\n%s```\n\n"
        "Apply this change:\n%s\n\n"
        "Output ONLY the complete updated contents of %s in a single ```%s code block. "
        "No explanation, no diff." % (path, lang, content, task.strip(), path, lang))
    acc = ""
    try:
        async for d in direct.stream(model, messages=[{"role": "user", "content": prompt}]):
            acc += d
    except Exception:
        return None
    m = _FENCE.search(acc)
    if not m:
        return None
    new = m.group(2)
    if not _is_full_rewrite(content, new):
        return None
    return new if new.endswith("\n") else new + "\n"


_ELLIPSIS = re.compile(r"^\s*(?:#|//|/\*|<!--|--)?\s*\.\.\.\s*(?:[A-Za-z][^\n]*)?$", re.M)


def _is_full_rewrite(old, new):
    if not new.strip():
        return False
    for tag in ("<tool>", "<tool_call>", "<function_calls>", "<invoke>"):
        if tag in new:
            return False
    if _ELLIPSIS.search(new):
        return False
    if len(new) < max(4, int(len(old) * 0.6)):
        return False
    old_lines = [l.strip() for l in old.splitlines() if l.strip()]
    new_lines = {l.strip() for l in new.splitlines() if l.strip()}
    if len(old_lines) >= 12:
        kept = sum(1 for l in old_lines if l in new_lines)
        if kept < len(old_lines) * 0.5:
            return False
    return True


def _harvest(text, requested):
    blocks = list(_FENCE.finditer(text))
    if not blocks:
        return []
    remaining = list(requested)
    out, used = [], set()
    for m in blocks:
        info, body = m.group(1), m.group(2)
        if len(body.strip()) < 8:
            continue
        name = None
        fm = _FILE_TOKEN.search(info)
        if fm:
            name = fm.group(1)
        if not name:
            pre = text[max(0, m.start() - 120):m.start()].rstrip()
            last = pre.rsplit("\n", 1)[-1] if "\n" in pre else pre
            pm = _FILE_TOKEN.search(last)
            if pm:
                name = pm.group(1)
        if not name and remaining:
            name = remaining[0]
        if not name or name in used:
            continue
        used.add(name)
        if name in remaining:
            remaining.remove(name)
        out.append((name, body if body.endswith("\n") else body + "\n"))
    return out


def _execute(name, args):
    """The single point where a tool actually runs. Routing (regex, classifier, or
    the model's own <tool> call) only ever proposes -- everything is gated here, so
    no new routing path can bypass the check by forgetting to ask."""
    if permissions.check(name, args) == permissions.DENY:
        return "Denied: %s was not run (%s)." % (name, permissions.describe(name, args))
    try:
        return TOOLS[name](**args)
    except Exception as e:
        return "Error: %s" % e


async def run(message, model=None, max_steps=12):
    model = model or "default"
    events = []

    def emit(kind, **kw):
        events.append({"type": kind, **kw})

    pre = _fast(message)
    if not pre and classify.ENABLED:
        pre = await classify.detect(message, model)
    elif not pre:
        pre = _dispatch(message)
    if pre:
        name, args = pre
        result = _execute(name, args)
        emit("tool", name=name, args=args, result=result)
        return {"text": "Done: %s" % result, "events": events}

    target = _resolve_edit_target(message)
    if target:
        new = await _transform(target, message, model)
        if new is not None:
            result = _execute("write_file", {"path": target, "content": new})
            emit("tool", name="write_file", args={"path": target}, result=result)
            return {"text": "Edited %s." % target, "events": events}

    convo = [{"role": "system", "content": TOOL_DOCS}, {"role": "user", "content": message}]
    harvested = False
    for _ in range(max_steps):
        acc = ""
        try:
            async for d in direct.stream(model, messages=convo):
                acc += d
        except Exception as e:
            return {"text": "Error: %s" % e, "events": events}
        calls = _parse_tool_calls(acc)
        if not calls and not harvested:
            got = _harvest(acc, [])
            if got:
                harvested = True
                for p, c in got:
                    calls.append(("", "write_file", {"path": p, "content": c}))
        if not calls:
            visible = _BARE_CALL.sub("", _TOOL_TAG.sub("", acc)).strip()
            return {"text": visible or acc.strip(), "events": events}
        convo.append({"role": "assistant", "content": acc})
        for match, name, args in calls[:1]:
            result = _execute(name, args)
            emit("tool", name=name, args=args, result=result)
            convo.append({"role": "user", "content": "Tool result: [%s]\n%s" % (name, result)})
    return {"text": "Stopped after %d steps." % max_steps, "events": events}
