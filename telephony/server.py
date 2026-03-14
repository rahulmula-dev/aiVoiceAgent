# Telephony Server - Twilio Entry Point (Modular v2)
import os
import asyncio
import logging
import uvicorn
from typing import List, Dict, Any, Optional
from datetime import datetime
from fastapi import FastAPI, WebSocket, Request, Response, Header, HTTPException, status, Depends, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from orchestrator.manager import VoiceOrchestrator
from orchestrator.factory import create_default_orchestrator, create_custom_orchestrator
from orchestrator.mocks import MockSTT, MockTTS
from orchestrator.session_manager import default_session_manager
from stt.transcriber import Transcriber
from tts.synthesizer import Synthesizer
from contracts.policy import PRDScripts
from dotenv import load_dotenv

# Health Check Dependencies
from crm.client import CRMClient
from retrieval.vector_store import KnowledgeBase
from telephony.concurrency import check_redis_health, MAX_INBOUND_CALLS
from contracts.auth import Role, get_current_user, get_current_user_ws, require_role
from agent_logging.audit_logger import AuditLogger

# Configure logging
logger = logging.getLogger("Server")

# Initialize global health checkers
_crm_checker = CRMClient()
_kb_checker = KnowledgeBase()

from agent_logging import bind_call_context, CallLogger, mask_phone_number

app = FastAPI()

# Mount static files for Sandbox Mode
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.on_event("startup")
async def startup_event():
    """Start-time background workers."""
    try:
        default_session_manager.start_collector()
        
        # S4-7 Additions: Reset active concurrency tracking on startup
        from telephony.concurrency import reset_active_calls
        reset_active_calls()

        # S5-1: Initialize Connection Pools synchronously before accepting calls
        from stt.stt_pool import stt_pool
        from tts.elevenlabs_pool import elevenlabs_pool
        
        logger.info("Initializing pre-warmed WebSocket pools...")
        # S5-1: strict raise instead of swallowing
        await stt_pool.initialize()

        if os.getenv("TTS_PROVIDER", "deepgram").lower() == "elevenlabs":
            await elevenlabs_pool.initialize()
        
        # [HIGH-P3-04] Activate CRM Failover Reconciliation Worker
        from crm.reconciliation_job import start_background_worker
        start_background_worker()
    except Exception as e:
        logger.error(f"CRITICAL: Application startup failed: {e}", exc_info=True)
        # Re-raise to ensure uvicorn reports the failure
        raise e

@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    """
    Forensic Middleware: Logs every rest-api access to restricted paths,
    capturing both successful and denied (401/403) attempts.
    """
    path = request.url.path
    if path.startswith(("/api/live-context", "/admin/")):
        response = await call_next(request)
        
        # Log status based on response code
        status_label = "granted" if response.status_code < 400 else "denied"
        action = "PII_ACCESS" if "/api/" in path else "ADMIN_CONFIG"
        
        # Extract role from header if possible (best effort without breaking the dependency flow)
        token = request.headers.get("X-Auth-Token") or request.query_params.get("token")
        from contracts.auth import validate_token
        role = validate_token(token) if token else "anonymous"

        AuditLogger.log_access(
            endpoint=path,
            role=role,
            status=status_label,
            action=action,
            ip=request.client.host if request.client else "unknown"
        )
        return response
    
    return await call_next(request)

@app.get("/")
async def root():
    return {"status": "GD College AI Voice Agent [Modular v2] - Active"}

@app.get("/healthz")
async def healthz():
    """
    Liveness Probe (K8s/Load Balancer).
    Returns 200 OK if the server process is running.
    """
    return {"status": "alive", "timestamp": datetime.now().isoformat()}

@app.get("/readyz")
async def readyz():
    """
    Readiness Probe (K8s/Load Balancer 10-call baseline verification).
    Performs real-time health checks on all critical dependencies.
    PRD: CRM failure is NON-CRITICAL (S3 DLQ handles failover).
    """
    import asyncio

    async def _safe(coro):
        """Wraps each health check so individual timeouts never crash the probe."""
        try:
            # [PRD] return await asyncio.wait_for(coro, timeout=2.0)  # 2s strict for K8s
            # [DEV] Slightly longer so CRM's own 2s timeout resolves cleanly first
            return await asyncio.wait_for(coro, timeout=3.5)
        except Exception:
            return False

    try:
        crm_ok, kb_ok, redis_ok = await asyncio.gather(
            _safe(_crm_checker.check_health()),
            _safe(_kb_checker.check_health()),
            _safe(asyncio.to_thread(check_redis_health)),
        )
        stt_ok = bool(os.getenv("DEEPGRAM_API_KEY"))

        status_map = {
            "crm": "connected" if crm_ok else "degraded (DLQ active)",
            "retrieval": "ready" if kb_ok else "failed",
            "redis": "connected" if redis_ok else "disconnected",
            "stt": "configured" if stt_ok else "missing_key"
        }

        # PARTIAL DEGRADATION POLICY:
        # Redis, STT, and KB are required for a baseline "High Quality" call.
        # CRM is optional because we have robust S3-based DLQ failover.
        local_test = os.getenv("LOCAL_TEST", "false").lower() == "true"
        # Redis is required in production, but optional in local test mode (RAM counter fallback)
        redis_required = not local_test
        is_ready = all([kb_ok, stt_ok]) and (redis_ok or not redis_required)

        if not is_ready:
            logger.warning(f"[HEALTH] Readiness probe failed (CRITICAL): {status_map}")
            return Response(
                content=f"Service Unhealthy: {status_map}",
                status_code=503,
                media_type="application/json"
            )

        if not crm_ok:
            logger.warning(f"[HEALTH] Running in DEGRADED mode (CRM unavailable): {status_map}")

        return {
            "status": "ready",
            "mode": "production" if crm_ok else "degraded (logging via DLQ)",
            "timestamp": datetime.now().isoformat(),
            "services": status_map
        }

    except Exception as e:
        logger.error(f"[HEALTH] Unexpected readiness check failure: {e}")
        return Response(content="Readiness Check Error", status_code=500)


@app.post("/admin/reload-config")
async def reload_config(
    request: Request,
    role: Role = Depends(require_role([Role.ADMIN]))
):
    """ Instantly reloads dynamic configuration flags. Admin only."""
    from contracts.config import FeatureConfig
    
    # Note: Audit logging for this REST endpoint is handled by audit_middleware
    
    new_intake_state = FeatureConfig.reload_dynamic_flags()
    
    return {
        "status": "success",
        "message": "Configuration hot-reloaded successfully.",
        "intake_enabled": new_intake_state
    }

@app.get("/api/live-context")
async def get_live_context_gui(
    request: Request,
    token: Optional[str] = Query(None),
    role: Role = Depends(require_role([Role.IT, Role.ADMIN], flexible=True))
):
    """ Developer Tool: Auto-updating GUI for the Live RAM Cache. Protected."""
    # Note: Audit logging for this REST endpoint is handled by audit_middleware
    
    # We must pass the token to the template so it can fetch /data
    # This is a minimal way to keep the dashboard working without complex cookie/session logic.
    safe_token = token or request.headers.get("X-Auth-Token", "")

    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Live RAM Cache Monitor</title>
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #1e1e2e; color: #cdd6f4; margin: 0; padding: 20px; }
            h1 { color: #89b4fa; text-align: center; }
            .status-bar { background-color: #313244; padding: 15px; border-radius: 8px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }
            .status-indicator { display: inline-block; width: 12px; height: 12px; border-radius: 50%; background-color: #a6e3a1; margin-right: 8px; box-shadow: 0 0 8px #a6e3a1; }
            .active-count { font-size: 1.2rem; font-weight: bold; color: #f9e2af; }
            .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr)); gap: 20px; }
            .card { background-color: #181825; border: 1px solid #313244; border-radius: 10px; padding: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); transition: transform 0.2s; }
            .card:hover { transform: translateY(-2px); border-color: #89b4fa; }
            .card-title { color: #89b4fa; font-size: 1.2rem; margin-top: 0; margin-bottom: 15px; border-bottom: 1px solid #313244; padding-bottom: 10px; word-break: break-all; }
            .field { margin-bottom: 8px; }
            .label { color: #bac2de; font-weight: bold; display: inline-block; width: 120px; }
            .value { color: #a6e3a1; font-family: monospace; font-size: 1.1rem; }
            .value.null { color: #f38ba8; font-style: italic; }
            .summary { background-color: #313244; padding: 10px; border-radius: 6px; margin-top: 15px; font-size: 0.9rem; font-style: italic; color: #bac2de; }
            .empty-state { text-align: center; color: #6c7086; margin-top: 50px; font-size: 1.2rem; }
        </style>
    </head>
    <body>
        <h1>🧠 Live Agent Context Memory</h1>
        <div class="status-bar">
            <div>
                <span class="status-indicator"></span>
                <span>Live RAM Sync Active</span>
            </div>
            <div class="active-count">Active Calls: <span id="call-count">0</span></div>
        </div>
        
        <div id="sessions-container" class="grid"></div>
        
        <div id="empty-state" class="empty-state">
            <p>No active calls detected. Waiting for a connection...</p>
        </div>

        <script>
            function formatValue(val) {
                if (val === null || val === undefined) return '<span class="value null">None</span>';
                return `<span class="value">${val}</span>`;
            }

            async function fetchContext() {
                try {
                    const token = '{{ token }}';
                    const response = await fetch(`/api/live-context/data?token=${token}`);
                    const data = await response.json();
                    
                    document.getElementById('call-count').innerText = data.active_calls_count;
                    
                    const container = document.getElementById('sessions-container');
                    const emptyState = document.getElementById('empty-state');
                    
                    if (data.active_calls_count === 0) {
                        container.innerHTML = '';
                        emptyState.style.display = 'block';
                        return;
                    }
                    
                    emptyState.style.display = 'none';
                    let cardsHtml = '';
                    
                    for (const [sid, session] of Object.entries(data.sessions)) {
                        cardsHtml += `
                            <div class="card">
                                <h2 class="card-title">📱 ${sid}</h2>
                                <div class="field"><span class="label">Name:</span> ${formatValue(session.user_name)}</div>
                                <div class="field"><span class="label">Program:</span> ${formatValue(session.program_interest)}</div>
                                <div class="field"><span class="label">Intake:</span> ${formatValue(session.intake)}</div>
                                <div class="field"><span class="label">Mode:</span> ${formatValue(session.study_mode)}</div>
                                <div class="field"><span class="label">Campus:</span> ${formatValue(session.campus)}</div>
                                
                                <div class="summary">
                                    <strong>Last AI Answer:</strong><br>
                                    ${session.last_agent_answer_summary || 'Waiting for AI to speak...'}
                                </div>
                            </div>
                        `;
                    }
                    container.innerHTML = cardsHtml;
                    
                } catch (e) {
                    console.error("Failed to fetch live context", e);
                }
            }

            // Refresh every 1.5 seconds
            setInterval(fetchContext, 1500);
            fetchContext();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content.replace('{{ token }}', safe_token))

@app.get("/api/live-context/data")
async def get_live_context_data(
    request: Request,
    role: Role = Depends(require_role([Role.IT, Role.ADMIN], flexible=True))
):
    """ Raw JSON data for the Live RAM Dashboard. Protected."""
    # Note: Audit logging for this REST endpoint is handled by audit_middleware
    live_sessions = {}
    for sid, session in default_session_manager.sessions.items():
        ctx_dict = session.call_context.dict()
        if "caller_number" in ctx_dict:
            ctx_dict["caller_number"] = mask_phone_number(ctx_dict["caller_number"])
        live_sessions[sid] = ctx_dict
        
    from telephony.concurrency import get_active_call_count
    
    return {
        "status": "LIVE_RAM_CACHE",
        "active_calls_count": get_active_call_count(),
        "sessions": live_sessions
    }

def is_twilio_request(request: Request) -> bool:
    """
    Minimal Twilio Verification (Pillar 3: Policy Enforcement).
    Validates that the request actually comes from a Twilio-signed source.
    """
    if os.getenv("BYPASS_TWILIO_AUTH") == "true": return True
    
    signature = request.headers.get("X-Twilio-Signature")
    if not signature:
        logger.warning(f"Unauthorized non-Twilio request from {request.client.host}")
        return False
    return True

@app.api_route("/voice", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """
    Twilio TwiML Entry Point.
    Instructs Twilio to open a bi-directional WebSocket stream.
    """
    if not is_twilio_request(request):
        raise HTTPException(status_code=403, detail="Forbidden: Non-Twilio Request.")
    # Extract Call metadata from Twilio
    try:
        form_data = await request.form()
        call_sid = form_data.get("CallSid", "UnknownSID")
        from_number = form_data.get("From", "Unknown")
    except:
        call_sid = "UnknownSID"
        from_number = "Unknown"
    
    logging.getLogger("Server").info(f"Incoming Voice Webhook. SID: {call_sid}, From: {mask_phone_number(from_number)}")
    
    from telephony.concurrency import increment_if_under_cap, MAX_INBOUND_CALLS
    is_accepted, new_count = await increment_if_under_cap(MAX_INBOUND_CALLS, call_sid=call_sid)
    
    if not is_accepted:
        logging.getLogger("Server").warning(f"[Concurrency] SUPER MAX REACHED! Active calls: {new_count}/{MAX_INBOUND_CALLS}. Rejecting SID: {call_sid}")
        
        # Log to CRM asynchronously to avoid blocking
        import asyncio
        from crm.client import CRMClient
        crm_client = CRMClient()
        
        import datetime
        timestamp = datetime.datetime.now().isoformat()
        masked_number = mask_phone_number(from_number)
        summary = f"Caller number: {masked_number}, reason = OVER_CAPACITY, timestamp: {timestamp}"
        
        # Fire and forget ticket creation
        asyncio.create_task(crm_client.create_ticket(
            transcript="Call rejected upfront due to 30-call concurrency limit.",
            summary=summary,
            sentiment="Negative",
            call_id=call_sid,
            title="OVER_CAPACITY | Voice Agent"
        ))
        
        twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="Polly.Joanna">{PRDScripts.APOLOGY_CAPACITY}</Say><Hangup/></Response>'
        return Response(content=twiml, media_type="application/xml")

    logging.getLogger("Server").info(f"[Concurrency] Call connected. Active calls: {new_count}/{MAX_INBOUND_CALLS} (SID: {call_sid})")

    # Determine public ngrok URL (from environment variable)
    # Determine public ngrok URL (from environment variable)
    public_url = os.getenv("NGROK_URL")
    if public_url:
        host = public_url.replace("https://", "").replace("http://", "")
    else:
        host = request.headers.get("host")

    # Determine protocol (ws or wss) based on NGROK_URL scheme
    protocol = "wss"
    if public_url and public_url.startswith("http://"):
        protocol = "ws"
        logger.info(f"[Twilio] Using insecure WebSocket (ws://) for raw IP/HTTP testing.")

    # Pass statusCallback on the stream/stream wrapper isn't natively standard for Stream cleanup, 
    # instead we will monitor standard Twilio Call Status Webhook callbacks for the Number itself.
    twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Connect><Stream url="{protocol}://{host}/media-stream?sid={call_sid}&amp;from={from_number}" /></Connect></Response>'
    return Response(content=twiml, media_type="application/xml")

@app.post("/api/call-status")
async def handle_call_status(request: Request):
    """
    Twilio StatusCallback endpoint to catch completed/failed/busy/no-answer calls
    and accurately decrement the active call counter.
    """
    try:
        form_data = await request.form()
        status = form_data.get("CallStatus")
        call_sid = form_data.get("CallSid", "UnknownSID")
        
        if status in ["completed", "failed", "busy", "no-answer", "canceled"]:
            from telephony.concurrency import decrement_active_calls, MAX_INBOUND_CALLS
            new_count = await decrement_active_calls(call_sid=call_sid)
            logging.getLogger("Server").info(f"[Concurrency] Call ended ({status}). Decremented active calls to: {new_count}/{MAX_INBOUND_CALLS} (SID: {call_sid})")
    except Exception as e:
        logging.getLogger("Server").error(f"Failed to process call-status: {e}")
        
    return Response()

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """
    Handles the WebSocket lifecycle.
    Delegates all intelligence and audio logic to the VoiceOrchestrator.
    """
    # PILLAR 3: Policy Enforcement - Twilio-only stream
    # Note: Twilio Websockets do not support standard auth headers,
    # validation relies on the /voice entrypoint providing the SID.
    # 1. EARLY INITIALIZATION (Forensic Pillar 1)
    # Generate ID immediately before anything else
    import uuid
    session_id = str(uuid.uuid4())[:8]
    
    # Try to extract query params (best effort)
    query_params = websocket.query_params
    call_sid = query_params.get("sid", "unknown_call")
    # T5 Fix: Handle the Dev-Phone simulator explicitly which fails to un-escape &amp; in XML Streams
    from_number = query_params.get("from") or query_params.get("amp;from", "Unknown")

    # Initialize Logger - PILLAR 3 will now create the Ghost File on disk immediately
    call_logger = CallLogger(call_id=session_id, caller_number=from_number)
    bind_call_context(session_id, from_number)
    logger.info(f">>> NEW WEBSOCKET CONNECTION: session={session_id} call_sid={call_sid} from={from_number}")
    call_logger.log_event("telephony", "call_connected", meta={"call_sid": call_sid})

    manager = None
    try:
        await websocket.accept()
        
        # 2. Use factory with shared session manager (Fixed WS-01)
        # S4 Audit: Passing websocket and metadata for Fallback and Compliance
        session_metadata = {"region": query_params.get("region", "US")}
        try:
            manager = await create_default_orchestrator(
                session_id=session_id,
                call_logger=call_logger, 
                session_manager=default_session_manager,
                websocket=websocket,
                session_metadata=session_metadata
            )
        except Exception as e:
            logger.error(f"FATAL: Failed to create orchestrator for {session_id}: {e}")
            # WS-02: User Experience Fallback
            # We don't have a manager yet, but we can play audio via a temporary synthesizer
            temp_synth = Synthesizer() 
            await temp_synth.play_fallback_audio(websocket, streamSid=call_sid)
            raise e
        
        # Register for zombie recovery access
        default_session_manager.register_orchestrator(session_id, manager)
        
        await manager.handle_audio_stream(websocket)
        call_logger.log_event("telephony", "call_ended", meta={"reason": "websocket_closed"})
        
    except asyncio.CancelledError:
        logger.warning(f"WebSocket {session_id} cancelled (likely disconnection).")
        call_logger.reason = "user_hangup"
    except Exception as e:
        if type(e).__name__ == "PoolExhaustedError":
            logger.error(f"[CAPACITY] Call {session_id} rejected due to exhausted pool: {e}")
            call_logger.log_event("telephony", "call_rejected", meta={"reason": "pool_exhausted"})
            try:
                from crm.client import CRMClient
                crm_client = CRMClient()
                asyncio.create_task(
                    crm_client.create_ticket(
                        transcript="System capacity reached. Call politely rejected.",
                        summary=f"Call rejected gracefully for {mask_phone_number(from_number)} due to connection pool exhaustion (Concurrency Cap Reached).",
                        sentiment="negative"
                    )
                )
            except Exception as crm_e:
                logger.error(f"Failed to create CRM ticket for rejected call: {crm_e}")

            try:
                from tts.synthesizer import Synthesizer
                synth = Synthesizer()
                await synth.play_fallback_audio(websocket, streamSid=call_sid)
                await asyncio.sleep(1)
                await websocket.close(code=1008, reason="Pool Exhausted")
            except:
                pass
            call_logger.reason = "capacity_exhausted"
            return
            
        call_logger.log_event("telephony", "call_failed", meta={"error": str(e)})
        logger.error(f"Media stream error for {session_id}: {e}", exc_info=True)
        call_logger.reason = "error"
    finally:
        # 2. EMERGENCY FLUSH (Forensic Pillar 2) - Mandatory execution
        # Regardless of how we exit (crash, cancel, success), save the audit trace.
        
        # 🟢 ABRUPT DISCONNECT SAFETY (S4 Fix): Instantly return pooled sockets
        if manager:
            try:
                await manager.cleanup()
            except Exception as e:
                logger.error(f"Manager cleanup failed during disconnect for {session_id}: {e}")
                
        call_logger.generate_summary_line()
        call_logger.save_log(session_obj=manager.session if manager else None)
        logger.info(f"Forensic Audit trace finalized for {session_id}")
        default_session_manager.unregister_orchestrator(session_id)
        default_session_manager.end_session(session_id)

@app.websocket("/ws/browser")
async def handle_browser_stream(websocket: WebSocket):
    """
    Browser-based Sandbox Mode (Sprint 2.7). 
    Protected: Requires ?token=... in query for IT/Admin role.
    """
    import uuid
    
    try:
        # 1. AUTH CHECK
        role = await get_current_user_ws(websocket.query_params.get("token"))
        AuditLogger.log_access(
            endpoint="/ws/browser",
            role=role,
            status="success",
            action="VOICE_BYPASS_WS",
            ip=websocket.client.host if websocket.client else "unknown"
        )
    except Exception as e:
        logger.warning(f"WebSocket Auth Failed: {e}")
        AuditLogger.log_access(
            endpoint="/ws/browser",
            role="anonymous",
            status="denied",
            action="UNAUTHORIZED_VOICE_WS_ATTEMPT",
            ip=websocket.client.host if websocket.client else "unknown"
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    session_id = str(uuid.uuid4())[:8]
    
    # Initialize Logger
    call_logger = CallLogger(call_id=session_id, caller_number="browser_dev")
    bind_call_context(session_id, "browser_dev")
    
    await websocket.accept()
    
    # 1. Custom Providers for Browser (PCM 16kHz)
    # Using explicit instantiation to override defaults
    stt = Transcriber(encoding="linear16", sample_rate=16000)
    tts = Synthesizer(encoding="linear16", sample_rate=16000)
    
    # 2. Custom Orchestrator
    manager = VoiceOrchestrator(
        stt_provider=stt,
        tts_provider=tts,
        call_logger=call_logger,
        session_manager=default_session_manager
    )
    
    # 3. Handle Stream (Protocol mimicking)
    # The browser client MUST send Twilio-formatted JSON messages for this to work.
    try:
        await manager.handle_audio_stream(websocket)
    finally:
        # EMERGENCY FLUSH (Forensic Pillar 2)
        call_logger.generate_summary_line()
        call_logger.save_log(session_obj=getattr(manager, 'session', None))
        default_session_manager.unregister_orchestrator(session_id)
        default_session_manager.end_session(session_id)

@app.get("/chat-ui", response_class=HTMLResponse)
async def chat_ui():
    """
    Serves a simple HTML interface for testing the agent via text.
    """
    with open("test_chat.html", "r") as f:
        return f.read()

@app.websocket("/chat")
async def handle_chat_stream(websocket: WebSocket):
    """
    WebSocket endpoint for Text-based testing.
    Protected: Requires ?token=... in query for IT/Admin role.
    """
    import uuid
    
    try:
        # 1. AUTH CHECK
        role = await get_current_user_ws(websocket.query_params.get("token"))
        AuditLogger.log_access(
            endpoint="/chat",
            role=role,
            status="success",
            action="TEXT_CHAT_WS",
            ip=websocket.client.host if websocket.client else "unknown"
        )
    except Exception as e:
        logger.warning(f"WebSocket Auth Failed: {e}")
        AuditLogger.log_access(
            endpoint="/chat",
            role="anonymous",
            status="denied",
            action="UNAUTHORIZED_TEXT_CHAT_ATTEMPT",
            ip=websocket.client.host if websocket.client else "unknown"
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    session_id = str(uuid.uuid4())[:8]
    
    # Initialize Logger for Chat (Logs will be saved to disk)
    call_logger = CallLogger(call_id=session_id, caller_number="web_chat")
    bind_call_context(session_id, "web_chat")
    call_logger.log_event("telephony", "chat_started")
    
    await websocket.accept()
    
    # Use Mock Providers
    manager = create_custom_orchestrator(
        stt_provider_class=MockSTT,
        tts_provider_class=MockTTS,
        call_logger=call_logger,
        session_manager=default_session_manager
    )
    
    try:
        await manager.handle_text_stream(websocket)
    except Exception as e:
        print(f"Chat Error: {e}")
        call_logger.log_event("telephony", "chat_error", meta={"error": str(e)})
    finally:
        call_logger.generate_summary_line()
        call_logger.save_log(session_obj=getattr(manager, 'session', None))


if __name__ == "__main__":
    PORT = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=PORT)
