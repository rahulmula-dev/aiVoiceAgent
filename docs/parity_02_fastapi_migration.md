# Step 2 — Web migration: aiohttp → FastAPI + uvicorn

**Status:** ✅ done
**Risk to working pipeline:** medium (framework swap, isolated to `run_server.py`)
**Time:** ~2 hours

## Why this before orchestrator extraction

The original plan had orchestrator extraction (Step 3) before the FastAPI
migration. We swapped the order on user instinct, because:

1. **One concern per step.** Doing framework swap + orchestrator extraction
   in a single step means a failure mode that's ambiguous (did the
   refactor break it, or the framework swap?). Splitting them lets each
   step's verification be sharp.
2. **No double work on the orchestrator.** If we extract the orchestrator
   first on aiohttp, the FastAPI migration that follows still has to touch
   the orchestrator's WebSocket interface — we'd refactor it twice. With
   FastAPI first, the orchestrator we extract in Step 3 is FastAPI-shaped
   from day one.
3. **Smaller surface for the framework swap.** Pre-swap, `run_server.py`
   was ~250 lines but only ~50 of those were framework primitives (routes,
   server bootstrap, WS API). The migration touched exactly those 50.

## Bootstrap context (what got us here)

Before Step 2, the clean build had already been migrated **once**: from
the original `websockets`-library-only server (with broken `process_request`
HTTP routing) to **aiohttp**. That earlier migration solved a real
problem — `websockets 16.0`'s `process_request` hook couldn't reliably
short-circuit non-WS HTTP requests; it kept returning `426 Upgrade
Required` to Twilio's `/voice` POST.

The aiohttp swap fixed it, but aiohttp isn't the target stack. Step 2 is
the migration to the **target** stack: FastAPI + uvicorn, matching the
company project.

## What changed

A single file: `run_server.py`. Three surgical edits.

### Edit 1 — imports

Removed:
```python
from aiohttp import web
```
Added:
```python
import uvicorn
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
```
Kept:
```python
import websockets  # still used as a CLIENT by STT/TTS for outbound connections
```

### Edit 2 — `WebSocketAdapter`, `voice_handler`, `ws_handler`

The adapter wraps FastAPI's `WebSocket` so the existing pipeline code
(which expects the `websockets` library API) keeps working unchanged.
Iteration uses `iter_text()` since Twilio Media Streams only sends text
frames (JSON containing base64-encoded audio).

```python
class WebSocketAdapter:
    def __init__(self, ws: WebSocket):
        self._ws = ws

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        try:
            async for text in self._ws.iter_text():
                yield text
        except WebSocketDisconnect:
            return

    async def send(self, data):
        try:
            if isinstance(data, (bytes, bytearray)):
                await self._ws.send_bytes(bytes(data))
            else:
                await self._ws.send_text(str(data))
        except (ConnectionResetError, RuntimeError, WebSocketDisconnect) as e:
            raise websockets.exceptions.ConnectionClosed(None, None) from e
```

Routes converted to FastAPI decorators. `@app.api_route(..., methods=["GET",
"POST"])` accepts both methods on `/voice`. `@app.websocket("/")` registers
the WS endpoint. `app` is module-level so decorators register at import time.

### Edit 3 — `main()`

Replaced `web.AppRunner` + `web.TCPSite` with `uvicorn.Config` +
`uvicorn.Server`:

```python
cfg = uvicorn.Config(
    app, host="127.0.0.1", port=5000,
    log_level="warning", access_log=False,
)
server = uvicorn.Server(cfg)
await server.serve()
```

## Dependencies added

| Package | Version | Purpose |
|---|---|---|
| `fastapi` | 0.138.0 | Web framework |
| `starlette` | 1.3.1 | ASGI primitives (transitive via fastapi) |
| `uvicorn` | 0.49.0 | ASGI server. Not `uvicorn[standard]` — avoids pulling deps that might conflict with existing `websockets==16.0`. |

## Smoke-test results

| Test | Before (aiohttp) | After (FastAPI) |
|---|---|---|
| `GET /voice` | 200 + TwiML, duplicate `Content-Type` header (cosmetic bug) | **200 + TwiML, clean single header** |
| `POST /voice` | **426 Upgrade Required** (this was the original bug that forced us to set `voice-method=GET` in Twilio) | **200 + TwiML** |
| `Server:` response header | `aiohttp/3.14.1` | `uvicorn` |

## Operational impact

**Daily runbook simplification.** Before Step 2, the daily Twilio webhook
update command had to include `--voice-method=GET`. After Step 2, both
methods work, so the simpler default command suffices:

```powershell
twilio api:core:incoming-phone-numbers:update \
  --sid PNe626f6d9628d06e85a8081058f1e9da5 \
  --voice-url=https://<new-hash>.ngrok-free.dev/voice
```

**Side-effect benefit.** FastAPI returns clean HTTP headers (no duplicate
`Content-Type`), uses standard `application/json` for non-form responses,
and produces the OpenAPI-shaped log messages standard ops tooling expects.

## What this unlocks

Step 3 (orchestrator extraction) can split routes into `telephony/server.py`
cleanly because FastAPI's decorator pattern makes route registration
self-contained. Step 9 (Redis concurrency cap) will sit in front of
`/voice` as a FastAPI dependency or middleware — straightforward with
FastAPI's DI system, would have been awkward with aiohttp.

## Verification

1. **Syntax + import** — `py_compile` + module import both clean.
2. **Routes registered** — `app.routes` shows two entries: `('/voice',
   {'GET', 'POST'})` and `('/', 'WS')`.
3. **Live smoke test** — server starts, `curl GET/POST /voice` returns
   `200 + TwiML` with `Server: uvicorn`.
4. **Existing modules unaffected** — STT/LLM/TTS modules import without
   changes; `websockets` library still works for their outbound client
   connections.
5. **Live phone call** — user dialed the Twilio number from Dev Phone;
   full call flow worked (greeting + multi-turn conversation + transcript
   saved).
