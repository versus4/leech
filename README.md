# leech

A small OpenAI-compatible gateway over the **use.ai** free web models, plus a
minimal file-editing **agent** that works reliably even on models that struggle
with tool calls.

It signs up throwaway accounts on demand (kept warm in a pool) and streams
replies over use.ai's WebSocket, so any of the current models are reachable
through a plain HTTP API.

The agent is confined to one folder, deletes and overwrites are recoverable, and
a cut-off reply is continued rather than left mid-sentence — see
[Undo, and asking first](#undo-and-asking-first) and
[Streams that don't cut off](#streams-that-dont-cut-off).

## Run it

```bash
pip install -r requirements.txt
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Then point any OpenAI SDK client at `http://localhost:8000/v1`. No API key is
needed.

## Models

Claude Fable 5, Opus 5 / 4.8, Sonnet 5, GPT-5.6 Sol, GPT-5.5, GPT-5.4, Gemini 3.6 Flash,
DeepSeek V4 Pro, Kimi K2.6, Grok 4.5, GLM 5.2. See `worker/config.py` for the
full list. Default: `gpt-5-6-sol`. The aliases `default`, `fast` and `smart` also
work as model ids.

use.ai publishes no endpoint that lists its models, so this roster is maintained
by hand and goes stale when they change theirs. Two things keep that from failing
silently: at startup `worker/catalog.py` checks that the default and every alias
point at a slug the roster actually contains — repointing and logging a warning if
not — and `resolve_model()` warns whenever it is handed an unknown id instead of
quietly substituting the default. To check the roster against the live gateway,
stream one message per slug and see which answer; there is no list to fetch.

## API

| endpoint | what it does |
|----------|--------------|
| `POST /v1/chat/completions` | OpenAI-compatible chat, streaming and non-streaming |
| `POST /agent` | run a file task in the workspace; returns `{"text", "events"}` |
| `POST /chat` | stateful chat (server keeps the history) |
| `POST /v1/chat` | stateless chat, simplified response shape |
| `GET /models` | list available model ids |
| `GET /health` | liveness |
| `GET /bank` | number of warm accounts in the pool |

## The agent

The agent runs inside a `workspace/` folder — set `LEECH_WORKDIR` to point it
somewhere else. Every path it touches is resolved and checked against that
folder, so it cannot read or write outside it.

It has thirteen tools:

| tool | what it does |
|------|--------------|
| `read_file`    | read a file (with line numbers) |
| `write_file`   | create or overwrite a file |
| `append_file`  | add to the end of a file |
| `edit_file`    | replace exact text (fuzzy-tolerant) |
| `delete_file`  | move a file or folder to the trash |
| `restore_file` | bring something back out of the trash |
| `list_dir`     | list the workspace as a tree |
| `grep_search`  | search file contents (regex), returns `file:line` matches |
| `move_file`    | move or rename a file/folder |
| `copy_file`    | copy a file/folder |
| `make_dir`     | create a directory |
| `glob_files`   | find files by name pattern (`**/*.py`) |
| `run_command`  | run a shell command in the workspace, capture output |

Just ask for what you want, in any language. There is no command syntax to
learn — the model decides which tool to use, so paraphrases and non-English
requests work the same as the phrasings a keyword matcher would have known.

Under the hood it tries three strategies, in order, so that weak models still
get the job done:

1. **Fast path** — a few unambiguous inputs (`ls`, a bare filename, `run
   pytest`) are handled by the app with no model call at all.
2. **Routing call** — one short call asks the model to pick a tool and answer
   in a single line, e.g. `read_file path=config.py`. One line instead of JSON,
   because a flat line can't come back malformed — no nesting to balance, no
   escaping to get wrong. Chatter and code fences around the answer are
   tolerated. Tools whose arguments are file *bodies* (`write_file`,
   `append_file`, `edit_file`) are deliberately excluded: multi-line content
   can't ride a one-line format without being mangled, so those go to step 3.
   `run_command` is excluded too — a routed shell string is not something to
   reconstruct from a loose grammar.
3. **Tool loop** — anything else falls through to a normal tool-calling loop,
   with JSON repair and code-block harvesting as safety nets. Models trained on
   other harnesses often reach for their own dialect, so `<tool_call>`,
   `<function_calls>`, `<invoke>` and bare top-level JSON are all accepted
   alongside `<tool>`. Open-ended edits to a single file take a shortcut here:
   the model rewrites the whole file in one shot and the result is written back,
   after a check that it really is a full rewrite — an ellipsis placeholder, a
   leaked tool tag, or a result that dropped most of the original's lines is
   rejected rather than written.

The routing call is grounded and checked. Its prompt carries the real workspace
listing, so the model can only name paths that exist, and the reply is validated
before anything runs: unknown tool, missing required argument, a `path` that
isn't an exact hit in the listing, or any path escaping the workspace is
rejected and falls through to step 3. The tool menu, the argument names and the
required-argument checks are all derived from the Python signatures in
`worker/tools.py`, so adding a tool there updates the prompt and the validator
automatically — and an unrecognised tool is validated strictly, not waved
through.

## Undo, and asking first

Better routing lowers how often a tool call is wrong; it never lowers what one
wrong call costs. Two mechanisms cover that gap.

**Nothing is destroyed.** `delete_file` moves things to
`workspace/.leech-trash/<timestamp>/`, keeping the original layout, and
`restore_file` moves them back. Overwriting counts too: `write_file` banks a copy
of the previous contents before replacing them — which matters most for the
whole-file rewrite path in step 3, since that overwrites on every edit. The trash
is hidden from listings and searches, and the oldest entries are pruned once
there are more than `LEECH_TRASH_MAX` (50) of them.

**Writes can require confirmation.** Every tool call — from the fast path, the
routing call, or the model's own `<tool>` tag — goes through one gate in
`worker/permissions.py`, so no routing path can skip it. Reads run
unconditionally. Writes and commands depend on the mode:

| mode | behaviour |
|------|-----------|
| `auto` (default for the server) | writes run unattended |
| `ask` | writes call a confirmation handler; without one installed, they are denied |
| `readonly` | reads only |

A short list of catastrophic commands (`rm -rf /`, `format c:`, …) is refused in
every mode, matched per command segment so `sudo rm -rf /` and `x && rm -rf /`
are caught too. Set the mode with `LEECH_PERMISSION_MODE`; the server logs which
one it started with.

Files are also protected against a stale overwrite: if a file changes on disk
between the agent reading it and writing it back, the write is refused with a
message telling the model to re-read and reapply. A long turn can't silently
clobber an edit you made while it was thinking.

| variable | effect |
|----------|--------|
| `LEECH_WORKDIR` | where the agent works (default `workspace/`) |
| `LEECH_PERMISSION_MODE` | `auto`, `ask`, or `readonly` |
| `LEECH_TRASH_MAX` | how many trash snapshots to keep (default 50) |
| `LEECH_CLASSIFY=0` | skip step 2, fall back to the keyword matcher |
| `LEECH_CLASSIFY_MODEL` | model for the routing call (default `fast`) |
| `LEECH_LISTING_TTL` | seconds to cache the workspace listing (default 2) |

## Streams that don't cut off

The free gateway drops sockets, goes quiet, and rate-limits per account. The
gateway client treats those as three different problems:

- **A dropped reply is continued, not restarted.** If a stream ends without a
  completion event, the reply so far is fed back with a request to continue from
  its exact tail, and the overlap between what was already shown and what comes
  back is stripped so nothing repeats. A clean socket close after text has
  streamed is treated as a normal end — some models finish that way — so a
  finished answer is never mistaken for a truncated one.
- **Rate limits get a fresh account immediately.** Waiting doesn't help a
  per-account quota, so there's no backoff on that path.
- **Empty replies fail fast.** They're usually deterministic for a given prompt,
  so retrying burns accounts without changing the outcome; the cap is two, then
  an actionable error.

Everything else backs off exponentially with jitter. Idle timeouts are per model,
with a longer allowance for reasoning models that go quiet mid-thought.

## Layout

```
backend/   FastAPI app + endpoints
worker/    direct.py      use.ai WebSocket gateway (retries, resume)
           account_pool.py warm throwaway accounts
           agent.py       tool loop, routing, whole-file rewrite
           classify.py    one-line routing call
           tools.py       the tools + workspace sandbox + trash
           permissions.py the gate in front of every tool call
           catalog.py     model roster hygiene
frontend/  optional chat UI (Vite)
```

## License

MIT — see [LICENSE](LICENSE).
