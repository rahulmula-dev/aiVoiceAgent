"""
telephony/server.py — FastAPI app + Twilio routes.

Module-level `app` so the route decorators register on import. run_server.py
imports `app` and runs it via uvicorn.

Routes:
  - GET / POST /voice         -> return TwiML telling Twilio to open the Media Stream
                                 WebSocket back to this server, then <Hangup/>
  - WebSocket /               -> Twilio Media Stream lands here. We spin up a
                                 fresh VoiceOrchestrator and hand it the socket.
  - GET /healthz              -> liveness probe (returns 200 if the process is up)
  - POST /api/call-status     -> Twilio status callback (logs terminal call states;
                                 placeholder hook for the Step 9 concurrency cap)

The WebSocketAdapter shim lets the orchestrator iterate the FastAPI WebSocket
with the same interface as the `websockets` library — so existing STT/TTS code
that catches `websockets.exceptions.ConnectionClosed` still works.
"""

import time
import traceback

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
import websockets

from orchestrator.factory import create_default_orchestrator, get_gate
from agent_logging.audit_logger import log_access
from agent_logging.voice_logger import mask_phone

_TERMINAL_CALL_STATUSES = {"completed", "failed", "busy", "no-answer", "canceled"}

_BUSY_TWIML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Response>'
    '<Say>We are currently assisting other callers. Please try again in a few minutes.</Say>'
    '<Hangup/>'
    '</Response>'
)


app = FastAPI(
    title="CILA Voice Agent",
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)


# ─────────────────────────────────────────────────────────────────────────────
# WebSocketAdapter — wraps FastAPI's WebSocket so existing twilio_handler-style
# code (which iterates `async for msg in ws` and calls `await ws.send(text)`)
# keeps working unchanged. On disconnect, raises websockets.ConnectionClosed
# so TTS's existing exception handler still fires.
#
# Twilio Media Streams sends only TEXT frames (JSON containing base64-encoded
# audio), so iter_text() is the right choice. send() still dispatches to
# send_bytes() for binary outbound just in case a future caller needs it.
# ─────────────────────────────────────────────────────────────────────────────


class WebSocketAdapter:
    def __init__(self, ws: WebSocket):
        self._ws = ws

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        try:
            async for text in self._ws.iter_text():
                yield text
        except (WebSocketDisconnect, RuntimeError):
            # WebSocketDisconnect: Twilio closed first (caller hung up).
            # RuntimeError:        we closed the WS server-side (orchestrator
            #   <Hangup/> path) and starlette raises "WebSocket is not
            #   connected" on the next receive. Both are clean ends.
            return

    async def send(self, data):
        try:
            if isinstance(data, (bytes, bytearray)):
                await self._ws.send_bytes(bytes(data))
            else:
                await self._ws.send_text(str(data))
        except (ConnectionResetError, RuntimeError, WebSocketDisconnect) as e:
            raise websockets.exceptions.ConnectionClosed(None, None) from e

    async def close(self):
        try:
            await self._ws.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Twilio voice webhook — returns TwiML
# ─────────────────────────────────────────────────────────────────────────────


@app.api_route("/voice", methods=["GET", "POST"])
async def voice_handler(request: Request) -> Response:
    """
    Twilio hits this endpoint on every inbound call. We:
      1. Check the Redis concurrency gate (Step 9) — reject with busy TwiML if at cap.
      2. Return TwiML telling Twilio to open a Media Stream WebSocket, then <Hangup/>.
    """
    # Twilio includes CallSid in query params (GET) or form body (POST)
    if request.method == "POST":
        form = await request.form()
        call_sid = form.get("CallSid", "")
    else:
        call_sid = request.query_params.get("CallSid", "")

    # Concurrency gate — atomic check+increment via Lua CAS
    gate = get_gate()
    if gate is not None and call_sid:
        try:
            admitted = await gate.acquire(call_sid)
        except Exception as e:
            print(f"[GATE] acquire error ({type(e).__name__}: {e}) — admitting call")
            admitted = True  # fail open: a Redis hiccup should not drop live calls
        if not admitted:
            count = await gate.current_count()
            print(f"[GATE] Rejecting {call_sid} — at capacity ({count} active)")
            return Response(content=_BUSY_TWIML, media_type="text/xml")

    host = (
        request.headers.get("X-Forwarded-Host")
        or request.headers.get("Host")
        or (request.url.hostname or "")
    )
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Connect><Stream url="wss://{host}/" /></Connect>'
        '<Hangup/>'
        '</Response>'
    )
    print(f"[TWIML] /voice hit  -> wss://{host}/  call_sid={call_sid or '?'}")
    return Response(content=twiml, media_type="text/xml")


# ─────────────────────────────────────────────────────────────────────────────
# Twilio Media Stream WebSocket — hands off to a fresh VoiceOrchestrator
# ─────────────────────────────────────────────────────────────────────────────


@app.websocket("/")
async def ws_handler(websocket: WebSocket) -> None:
    await websocket.accept()
    print("[WS] Twilio Media Stream connected")
    try:
        orchestrator = await create_default_orchestrator()
        await orchestrator.handle_audio_stream(WebSocketAdapter(websocket))
    except WebSocketDisconnect:
        print("[WS] Twilio disconnected")
    except Exception as e:
        print(f"[WS] handler error: {type(e).__name__}: {e}")
        traceback.print_exc()


# ─────────────────────────────────────────────────────────────────────────────
# Liveness probe
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/healthz")
async def healthz() -> dict:
    """
    Liveness probe — used by Kubernetes / Docker-compose / monitoring to know
    the process is alive. Cheap: returns 200 immediately. Does NOT check
    Deepgram/Groq/ElevenLabs reachability — that's /readyz's job (added when
    we have actual dependencies to check, e.g. Step 7 Postgres + Step 9 Redis).
    """
    return {"status": "ok", "service": "cila-voice-agent"}


# ─────────────────────────────────────────────────────────────────────────────
# Twilio status callback — call-lifecycle webhook
# ─────────────────────────────────────────────────────────────────────────────


@app.post("/api/call-status")
async def call_status(request: Request) -> dict:
    """
    Twilio's per-call status callback. Twilio POSTs here on terminal call
    states: `completed`, `failed`, `busy`, `no-answer`, `canceled`. The
    payload is form-encoded with at least CallSid + CallStatus.

    For now we just log the event. When Step 9 lands (Redis concurrency cap)
    this is where `decrement_active_calls(CallSid)` will fire.

    Wire-up: this endpoint URL must be configured on the Twilio number's
    "Status callback URL" field — that's separate from the voice URL. The
    daily-runbook CLI command to set both is:

        twilio api:core:incoming-phone-numbers:update --sid <SID> \\
            --voice-url=https://<ngrok>/voice \\
            --status-callback=https://<ngrok>/api/call-status \\
            --status-callback-method=POST
    """
    form = await request.form()
    call_sid = form.get("CallSid", "<unknown>")
    status = form.get("CallStatus", "<unknown>")
    duration = form.get("CallDuration", "0")
    from_number = form.get("From", "")
    client_ip = (request.client.host if request.client else "unknown")
    print(f"[STATUS] CallSid={call_sid}  status={status}  duration={duration}s  ts={int(time.time())}")

    # Release the concurrency slot for terminal call states
    if status in _TERMINAL_CALL_STATUSES:
        gate = get_gate()
        if gate is not None and call_sid != "<unknown>":
            try:
                remaining = await gate.release(call_sid)
                print(f"[GATE] Released slot for {call_sid} ({status}), active={remaining}")
            except Exception as e:
                print(f"[GATE] release error ({type(e).__name__}: {e})")

    # Tamper-evident access audit. The phone number (if Twilio sent one) is
    # masked before it lands in the audit file.
    log_access(
        endpoint="/api/call-status",
        status="received",
        action="call_status_callback",
        role="twilio",
        ip=client_ip,
        CallSid=call_sid,
        CallStatus=status,
        CallDuration=duration,
        From=mask_phone(from_number) if from_number else "<unknown>",
    )
    return {"received": True}
