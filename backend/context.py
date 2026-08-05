"""
Conversation memory the target never has. Each account = 1 stateless prompt,
so we stuff prior turns into every new prompt to fake continuity.
"""
from collections import defaultdict
from typing import Dict, List

_STORE: Dict[str, List[dict]] = defaultdict(list)

MAX_HISTORY_CHARS = 6000


def get_history(session_id: str) -> List[dict]:
    return _STORE[session_id]


def append(session_id: str, role: str, content: str) -> None:
    _STORE[session_id].append({"role": role, "content": content})


def reset(session_id: str) -> None:
    _STORE.pop(session_id, None)


def build_messages(session_id: str, new_message: str) -> list:
    """Role-tagged history + the new user turn, as [{role, content}] (OpenAI-style).
    Passed natively to the WS frame so the model gets real conversation structure
    instead of a flattened text blob."""
    return _trim(get_history(session_id)) + [{"role": "user", "content": new_message}]


def build_prompt(session_id: str, new_message: str) -> str:
    """Serialize history + new message into one self-contained prompt (legacy)."""
    history = _trim(get_history(session_id))
    if not history:
        return new_message
    lines = ["[Previous conversation]"]
    for turn in history:
        speaker = "User" if turn["role"] == "user" else "Assistant"
        lines.append(f"{speaker}: {turn['content']}")
    lines.append("\n[Now respond only to this latest message]")
    lines.append(f"User: {new_message}")
    return "\n".join(lines)


def _trim(history: List[dict]) -> List[dict]:
    """Rolling window: drop oldest turns until under the char budget."""
    total = sum(len(t["content"]) for t in history)
    out = list(history)
    while out and total > MAX_HISTORY_CHARS:
        total -= len(out[0]["content"])
        out = out[1:]
    return out
