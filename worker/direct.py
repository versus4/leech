"""
Headless DIRECT path: sign up a throwaway account (HTTP) and stream the reply
over use.ai's budget-agent WebSocket. No browser in the hot path.

Protocol (verified 2026-06-17):
  CONNECT wss://agents.use.ai/agents/budget-agent/<chatId>
            ?userId=<uuid>&userType=regular&userEmail=<email>&planType=free&isTestUser=false
  SEND    one JSON frame: {chatId,userId,userType,planType,selectedModel,
            messages:[{role,parts:[{type:text,text}]}],trigger,source,...}
  RECV    Vercel-AI-SDK frames wrapped as {index,streamId,chunk:{...}}:
            text-delta(delta=..) tokens, terminated by finish / stream-complete.
            Cap -> {"type":"rate-limit-error",...}
"""
import asyncio
import json
import logging
import random
import uuid

from urllib.parse import urlencode

from . import config
from .session_http import create_account, get_ws_tokens

log = logging.getLogger("direct")


class StreamTruncated(RuntimeError):
    """The socket ended before any finish/stream-complete event -- the reply is
    cut off mid-answer and can be continued from the partial."""


class RateLimited(RuntimeError):
    """Per-account quota. A fresh account fixes it; waiting does not."""


def _idle_for(slug):
    """Reasoning models go quiet for long stretches mid-thought. One flat timeout
    either cuts them off or lets a genuinely dead stream hang."""
    base = getattr(config, "WS_IDLE_TIMEOUT", 90)
    for marker in getattr(config, "REASONING_MODEL_MARKERS", ()):
        if marker in slug:
            return getattr(config, "WS_IDLE_TIMEOUT_REASONING", 300)
    return base


def _backoff_delay(attempt):
    cap = getattr(config, "DIRECT_WS_BACKOFF_CAP", 8.0)
    d = getattr(config, "DIRECT_WS_BACKOFF", 0.75) * (2 ** (attempt - 1))
    return min(d * (0.5 + random.random()), cap)

try:
    import websockets
except ImportError:
    websockets = None

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36")


def enabled() -> bool:
    return bool(getattr(config, "DIRECT_WS_ENABLED", False)) and websockets is not None


async def _get_account() -> dict:
    try:
        from .account_pool import POOL
        return await POOL.acquire()
    except Exception:
        return await create_account()


def _model_slug(model: str) -> str:
    return config.resolve_model(model)


def _to_parts(messages: list) -> list:
    """[{role, content}] -> use.ai message-parts. Roles other than user/assistant
    (e.g. system) are relabelled to user. Verified: the WS honors prior turns."""
    out = []
    for m in messages:
        content = (m.get("content") or "").strip()
        if not content:
            continue
        role = m.get("role")
        if role not in ("user", "assistant"):
            role = "user"
        out.append({
            "id": uuid.uuid4().hex[:16], "role": role,
            "parts": [{"type": "text", "text": content}], "metadata": {}})
    if not out:
        out.append({"id": uuid.uuid4().hex[:16], "role": "user",
                    "parts": [{"type": "text", "text": ""}], "metadata": {}})
    return out


def _build_frame(chat_id, user_id, email, model, parts):
    return {
        "chatId": chat_id, "userId": user_id, "email": email,
        "userType": "regular", "userEmail": email, "planType": "free",
        "subscriptionStatus": "inactive", "isFreemium": False, "isTestUser": False,
        "selectedModel": config.MODEL_PREFIX + _model_slug(model), "locale": "en",
        "isWebSearchMode": False, "isDeepResearchMode": False,
        "isImageGenerationMode": False, "agenticMode": False,
        "messages": parts,
        "trigger": "submit-message", "source": "chat_page",
    }


async def _stream_gen(acct: dict, model: str, parts: list):
    """Yield text deltas as they arrive. Uses a per-token IDLE timeout (resets on
    every frame), so a long code generation never trips a total-time cap -- it
    only ends on finish/stream-complete, a closed socket, or `idle`s of silence."""
    chat_id = str(uuid.uuid4())
    token, app_token = await get_ws_tokens(acct)
    q = urlencode({
        "token": token, "app_token": app_token,
        "userId": acct["user_id"], "userType": "regular",
        "userEmail": acct["email"], "planType": "free", "isTestUser": "false",
    })
    uri = f"{config.WS_AGENT_BASE}/{chat_id}?{q}"
    hdrs = {"Cookie": acct["cookie_header"], "Origin": "https://use.ai", "User-Agent": _UA}
    idle = _idle_for(config.resolve_model(model))
    async with websockets.connect(uri, additional_headers=hdrs, max_size=None,
                                  open_timeout=config.WS_OPEN_TIMEOUT,
                                  ping_interval=20, ping_timeout=60) as ws:
        await ws.send(json.dumps(_build_frame(
            chat_id, acct["user_id"], acct["email"], model, parts)))
        done = False
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=idle)
            except asyncio.TimeoutError:
                break
            except websockets.ConnectionClosed as cc:
                code = getattr(cc, "code", None)
                if code is None:
                    code = getattr(getattr(cc, "rcvd", None), "code", None)
                if code in (1000, 1001, 1005):
                    done = True
                break
            try:
                o = json.loads(raw)
            except Exception:
                continue
            if o.get("type") == "rate-limit-error":
                raise RateLimited(o.get("messageMetadata", {}).get("errorType", "?"))
            chunk = o.get("chunk")
            if not isinstance(chunk, dict) and isinstance(o.get("data"), dict):
                chunk = o["data"]
            if isinstance(chunk, dict):
                t = chunk.get("type")
                if t == "text-delta":
                    d = chunk.get("delta") or chunk.get("textDelta") or chunk.get("text") or ""
                    if d:
                        yield d
                elif t in ("finish", "finish-step", "message-stop"):
                    done = True
                    break
            if o.get("type") == "stream-complete":
                done = True
                break
        if not done:
            raise StreamTruncated("stream ended without a completion event")


def _dedupe_overlap(prior, continuation):
    """Strip the longest suffix of what we've shown that the continuation repeats."""
    if not prior or not continuation:
        return continuation
    span = min(len(prior), len(continuation), 200)
    tail = prior[-span:]
    for size in range(span, 0, -1):
        if tail[-size:] == continuation[:size]:
            return continuation[size:]
    return continuation


def _resume_messages(base, partial):
    return [
        *base,
        {"role": "assistant", "content": partial},
        {"role": "user", "content":
            "Continue exactly where you left off. Do not restart, repeat, summarize, "
            "or preface anything. Output only the remaining continuation, beginning "
            "with the next new characters after this exact tail: ```"
            + partial[-400:] + "```"},
    ]


async def _stream_with_resume(base_messages, make_stream, budget):
    """Finish a cut-off reply instead of ending mid-sentence. The first pass streams
    live; each resume is buffered so its overlap with what the caller already saw can
    be stripped before emitting. Resume prompts are rebuilt from the ORIGINAL messages
    every time, so they never stack across several cut-offs."""
    yielded = ""
    messages = base_messages
    resumes = 0
    while True:
        seg = ""
        truncated = False
        try:
            async for delta in make_stream(messages):
                seg += delta
                if resumes == 0:
                    yielded += delta
                    yield delta
        except StreamTruncated:
            truncated = True
        if resumes > 0:
            out = _dedupe_overlap(yielded, seg)
            if out:
                yielded += out
                yield out
        if not truncated:
            return
        if not yielded.strip() or resumes >= max(0, budget):
            raise StreamTruncated("resume budget exhausted")
        resumes += 1
        messages = _resume_messages(base_messages, yielded)


async def stream(model: str, prompt: str | None = None,
                 messages: list | None = None, acct: dict | None = None):
    """Async generator of text deltas. Pass EITHER `prompt` or a role-tagged
    `messages` list. Retries on a FRESH account only while nothing has been
    emitted yet (once tokens start flowing we never restart -- the client already
    has partial output)."""
    if websockets is None:
        raise RuntimeError("websockets not installed")
    base = messages if messages else [{"role": "user", "content": prompt or ""}]
    retries = config.DIRECT_WS_RETRIES
    budget = getattr(config, "USEAI_RESUME_RETRIES", 2)
    empty_max = getattr(config, "EMPTY_REPLY_RETRIES", 2)
    empty_streak = 0
    last = None
    for attempt in range(1, retries + 1):
        a = acct or await _get_account()
        acct = None
        produced = False

        def _make(msgs, _a=a):
            return _stream_gen(_a, model, _to_parts(msgs))

        try:
            async for d in _stream_with_resume(base, _make, budget):
                produced = True
                yield d
            if produced:
                return
            empty_streak += 1
            last = RuntimeError(
                "The model returned an empty response -- often a system prompt too "
                "large for this model on the free tier. Try shortening it or switch "
                "models.")
            log.warning("attempt %d/%d empty (streak %d/%d)",
                        attempt, retries, empty_streak, empty_max)
            if empty_streak >= empty_max:
                break
        except RateLimited as e:
            last = e
            if produced:
                raise
            log.warning("attempt %d/%d rate-limited (%s) -> fresh account",
                        attempt, retries, e)
            continue
        except Exception as e:
            last = e
            if produced:
                log.warning("direct stream broke mid-reply (%r) -> ending with partial", e)
                return
            log.warning("direct attempt %d/%d failed: %r", attempt, retries, e)
            if attempt < retries:
                await asyncio.sleep(_backoff_delay(attempt))
    if last:
        raise last


async def complete(model: str, prompt: str | None = None,
                   messages: list | None = None, acct: dict | None = None) -> str:
    """Buffered variant: collect the whole reply (used by non-streaming callers)."""
    out = []
    async for d in stream(model, prompt=prompt, messages=messages, acct=acct):
        out.append(d)
    reply = "".join(out).strip()
    if not reply:
        raise RuntimeError("empty reply")
    return reply
