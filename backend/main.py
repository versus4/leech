"""
FastAPI orchestrator.
  GET  /                    -> chatbox frontend
  GET  /models              -> model list for the dropdown
  GET  /bank                -> bank status (how many warm accounts ready)
  POST /chat                -> stateful chat (we hold context), streams reply
  POST /v1/chat             -> stateless, simple OpenAI-ish
  POST /v1/chat/completions -> OpenAI-compatible (drop-in for OpenAI SDK clients)

On startup a background loop keeps the account bank topped up so signup stays
out of the hot path.
"""
import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from worker import bank, config, health
from worker.harvester import top_up
from worker.leech import run_messages, stream_messages
from . import context
from .pool import run_guarded, run_guarded_gen

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("backend")
app = FastAPI(title="WMan")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
FRONTEND_DIST = FRONTEND_DIR / "dist"
FRONTEND_INDEX = FRONTEND_DIST / "index.html"

if (FRONTEND_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")


@app.on_event("startup")
async def _validate_roster():
    from worker import catalog
    problems = catalog.validate()
    log.info("model roster: %d models, default=%s%s", len(config.MODELS),
             config.DEFAULT_MODEL,
             "" if not problems else " (%d issue(s) fixed, see warnings)" % len(problems))


@app.on_event("startup")
async def _configure_permissions():
    from worker import permissions
    mode = os.environ.get("LEECH_PERMISSION_MODE", permissions.MODE_AUTO)
    permissions.set_mode(mode)
    if permissions.get_mode() == permissions.MODE_AUTO:
        log.warning("permissions: AUTO -- tool calls run unattended (hard-denied "
                    "commands still blocked; deletes/overwrites go to .leech-trash). "
                    "Set LEECH_PERMISSION_MODE=readonly to refuse all writes.")
    else:
        log.info("permissions: %s", permissions.get_mode())


@app.on_event("startup")
async def _start_prewarmer():
    if getattr(config, "DIRECT_WS_ENABLED", False):
        from worker.account_pool import POOL
        POOL.start()
        log.info("DIRECT_WS_ENABLED -> headless path, warm account pool started")
        return

    async def loop():
        while True:
            try:
                n = await top_up()
                if n:
                    log.info("bank +%d (fresh=%d)", n, bank.count_fresh())
            except Exception as e:
                log.warning("prewarm error: %s", e)
            await asyncio.sleep(config.PREWARM_INTERVAL_SEC)
    asyncio.create_task(loop())


@app.get("/", response_class=HTMLResponse)
async def index():
    if FRONTEND_INDEX.exists():
        return FRONTEND_INDEX.read_text(encoding="utf-8")
    return """
    <!doctype html>
    <html lang="en">
      <head><meta charset="utf-8"><title>WMan frontend not built</title></head>
      <body style="font-family: system-ui; max-width: 720px; margin: 48px auto; line-height: 1.5;">
        <h1>Frontend build missing</h1>
        <p>Run these commands from <code>leech\\frontend</code>, then restart the backend:</p>
        <pre>npm install
npm run build</pre>
        <p>For live React development, run <code>npm run dev</code> and open
        <code>http://localhost:5173</code>.</p>
      </body>
    </html>
    """


@app.get("/models")
async def models():
    return {"models": config.MODELS, "default": config.DEFAULT_MODEL}


@app.get("/bank")
async def bank_status():
    if getattr(config, "DIRECT_WS_ENABLED", False):
        from worker.account_pool import POOL
        snap = health.H.snapshot(POOL.ready())
        return {
            "mode": "headless-ws",
            "warm_accounts": POOL.ready(),
            "pool_target": POOL.size,
            "status": snap["status"],
            "reasons": snap["reasons"],
        }
    snap = health.H.snapshot(bank.count_fresh())
    return {
        "fresh": snap["fresh_accounts"],
        "status": snap["status"],
        "reasons": snap["reasons"],
        "stats": bank.stats(),
    }


@app.get("/health")
async def health_status():
    """Full watchdog readout: status, why, rates, counters, recent errors."""
    if getattr(config, "DIRECT_WS_ENABLED", False):
        from worker.account_pool import POOL
        snap = health.H.snapshot(POOL.ready())
        snap["warm_accounts"] = POOL.ready()
        snap["pool_target"] = POOL.size
        return snap
    return health.H.snapshot(bank.count_fresh())


MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
IMAGE_URL_RE = re.compile(
    r"(https?://[^\s<>()\"]+(?:\.(?:png|jpe?g|webp|gif|avif)(?:\?[^\s<>()\"]*)?"
    r"|/[^\s<>()\"]*(?:image|img|generated|output)[^\s<>()\"]*)"
    r"|data:image/[a-zA-Z+.-]+;base64,[a-zA-Z0-9+/=]+)",
    re.IGNORECASE,
)


def _sse_payload(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _sse(token: str) -> str:
    return _sse_payload({"type": "token", "token": token})


def _extract_image(reply: str) -> dict | None:
    markdown = MARKDOWN_IMAGE_RE.search(reply)
    if markdown:
        caption = (reply[:markdown.start()] + reply[markdown.end():]).strip()
        return {
            "url": markdown.group(2),
            "alt": markdown.group(1) or "Generated image",
            "caption": caption,
        }

    direct_url = IMAGE_URL_RE.search(reply)
    if direct_url:
        caption = (reply[:direct_url.start()] + reply[direct_url.end():]).strip()
        return {
            "url": direct_url.group(1),
            "alt": "Generated image",
            "caption": caption,
        }

    return None


async def _stream_text(text: str):
    for i in range(0, len(text), 8):
        yield _sse(text[i:i + 8])
        await asyncio.sleep(0.01)
    yield "data: [DONE]\n\n"


async def _stream_reply(reply: str):
    image = _extract_image(reply)
    if not image:
        async for chunk in _stream_text(reply):
            yield chunk
        return

    caption = image.get("caption") or ""
    if caption:
        for i in range(0, len(caption), 8):
            yield _sse(caption[i:i + 8])
            await asyncio.sleep(0.01)
    yield _sse_payload({"type": "image", "image": image})
    yield "data: [DONE]\n\n"


@app.post("/chat")
async def chat(req: Request):
    body = await req.json()
    message = body.get("message", "")
    model = body.get("model", "default")
    session_id = body.get("sessionId") or str(uuid.uuid4())

    messages = context.build_messages(session_id, message)
    context.append(session_id, "user", message)

    async def gen():
        parts: list[str] = []
        try:
            async for delta in run_guarded_gen(lambda: stream_messages(model, messages)):
                parts.append(delta)
                yield _sse(delta)
        except Exception as exc:
            log.warning("chat stream failed: %s: %s", type(exc).__name__, exc)
            if not parts:
                yield _sse(f"Backend error contacting the model runner ({type(exc).__name__}).")
        reply = "".join(parts).strip()
        context.append(session_id, "assistant", reply)
        image = _extract_image(reply)
        if image:
            yield _sse_payload({"type": "image", "image": image})
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/v1/chat")
async def v1_chat(req: Request):
    body = await req.json()
    model = body.get("model", "default")
    reply = await run_guarded(lambda: run_messages(model, body.get("messages", [])))
    return JSONResponse({
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": reply}}],
    })


@app.post("/agent")
async def agent_run(req: Request):
    from worker import agent as _agent
    body = await req.json()
    message = body.get("message") or body.get("prompt") or ""
    model = body.get("model", "default")
    result = await run_guarded(lambda: _agent.run(message, model))
    return JSONResponse(result)


def _openai_block(reply: str, model: str) -> dict:
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": reply},
            "finish_reason": "stop",
        }],
    }


@app.post("/v1/chat/completions")
async def openai_completions(req: Request):
    body = await req.json()
    model = body.get("model", "default")
    stream = bool(body.get("stream", False))
    msgs = body.get("messages", [])

    if stream:
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())

        async def gen():
            base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model}
            async for delta in run_guarded_gen(lambda: stream_messages(model, msgs)):
                chunk = {**base, "choices": [{"index": 0, "delta": {"content": delta},
                                              "finish_reason": None}]}
                yield f"data: {json.dumps(chunk)}\n\n"
            done = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield f"data: {json.dumps(done)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    reply = await run_guarded(lambda: run_messages(model, msgs))
    return JSONResponse(_openai_block(reply, model))
