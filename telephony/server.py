# Telephony Server - Twilio Entry Point (Modular v2)
import os
import logging
import uvicorn
from datetime import datetime
from fastapi import FastAPI, WebSocket, Request, Response, Header, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from orchestrator.manager import VoiceOrchestrator
from orchestrator.factory import create_default_orchestrator, create_custom_orchestrator
from orchestrator.mocks import MockSTT, MockTTS
from orchestrator.session_manager import default_session_manager
from stt.transcriber import Transcriber
from tts.synthesizer import Synthesizer
from dotenv import load_dotenv

load_dotenv()

# Configure logging
logger = logging.getLogger("Server")

from agent_logging import bind_call_context, CallLogger

app = FastAPI()

# Mount static files for Sandbox Mode
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.on_event("startup")
async def startup_event():
    """Start-time background workers."""
    default_session_manager.start_collector()
    
    # S4-7 Additions: Reset active concurrency tracking on startup
    from telephony.concurrency import reset_active_calls
    reset_active_calls()

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
    Readiness Probe.
    Checks if critical dependencies (CRM, Transcriber config) are ready.
    """
    # MOCK DEPENDENCY CHECK (Expand this as needed)
    # 1. Check CRM API Reachability (Simulated)
    # 2. Check Deepgram API Key presence
    if not os.getenv("DEEPGRAM_API_KEY"):
        return Response(content="Missing DEEPGRAM_API_KEY", status_code=503)
        
    return {"status": "ready", "services": {"crm": "mock_connected", "stt": "configured"}}

@app.post("/admin/reload-config")
async def reload_config(x_admin_token: str = Header(...)):
    """
    Instantly reloads dynamic configuration flags. 
    Protected by a simple admin token check.
    """
    from contracts.config import FeatureConfig
    expected_token = os.getenv("ADMIN_RELOAD_TOKEN", "default-staging-token")
    
    if x_admin_token != expected_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="Unauthorized configuration reload attempt."
        )
    
    new_intake_state = FeatureConfig.reload_dynamic_flags()
    
    return {
        "status": "success",
        "message": "Configuration hot-reloaded successfully.",
        "intake_enabled": new_intake_state
    }

@app.get("/api/live-context")
async def get_live_context_gui():
    """
    Developer Tool: Auto-updating GUI for the Live RAM Cache.
    """
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
                    const response = await fetch('/api/live-context/data');
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
    return HTMLResponse(content=html_content)

@app.get("/api/live-context/data")
async def get_live_context_data():
    """
    Raw JSON data for the Live RAM Dashboard.
    """
    live_sessions = {}
    for sid, session in default_session_manager.sessions.items():
        live_sessions[sid] = session.call_context.dict()
        
    from telephony.concurrency import get_active_call_count
    
    return {
        "status": "LIVE_RAM_CACHE",
        "active_calls_count": get_active_call_count(),
        "sessions": live_sessions
    }

@app.api_route("/voice", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """
    Twilio TwiML Entry Point.
    Instructs Twilio to open a bi-directional WebSocket stream.
    """
    # Extract Call metadata from Twilio
    try:
        form_data = await request.form()
        call_sid = form_data.get("CallSid", "UnknownSID")
        from_number = form_data.get("From", "Unknown")
    except:
        call_sid = "UnknownSID"
        from_number = "Unknown"
    
    logging.getLogger("Server").info(f"Incoming Voice Webhook. SID: {call_sid}, From: {from_number}")
    
    from telephony.concurrency import increment_if_under_cap, MAX_INBOUND_CALLS
    is_accepted, new_count = increment_if_under_cap(MAX_INBOUND_CALLS)
    
    if not is_accepted:
        logging.getLogger("Server").warning(f"[Concurrency] SUPER MAX REACHED! Active calls: {new_count}/{MAX_INBOUND_CALLS}. Rejecting SID: {call_sid}")
        
        # Log to CRM asynchronously to avoid blocking
        import asyncio
        from crm.client import CRMClient
        crm_client = CRMClient()
        
        import datetime
        timestamp = datetime.datetime.now().isoformat()
        summary = f"Caller number: {from_number}, reason = OVER_CAPACITY, timestamp: {timestamp}"
        
        # Fire and forget ticket creation
        asyncio.create_task(crm_client.create_ticket(
            transcript="Call rejected upfront due to 30-call concurrency limit.",
            summary=summary,
            sentiment="Negative",
            call_id=call_sid,
            title="OVER_CAPACITY | Voice Agent"
        ))
        
        twiml = '<?xml version="1.0" encoding="UTF-8"?><Response><Say>All our lines are busy at the moment, but I will arrange a callback.</Say><Hangup/></Response>'
        return Response(content=twiml, media_type="application/xml")

    logging.getLogger("Server").info(f"[Concurrency] Call connected. Active calls: {new_count}/{MAX_INBOUND_CALLS} (SID: {call_sid})")

    # Determine public ngrok URL (from environment variable)
    public_url = os.getenv("NGROK_URL")
    if public_url:
        host = public_url.replace("https://", "").replace("http://", "")
    else:
        host = request.headers.get("host")

    # Pass statusCallback on the stream/stream wrapper isn't natively standard for Stream cleanup, 
    # instead we will monitor standard Twilio Call Status Webhook callbacks for the Number itself.
    twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Connect><Stream url="wss://{host}/media-stream?sid={call_sid}&amp;from={from_number}" /></Connect></Response>'
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
            new_count = decrement_active_calls()
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
    call_logger.log_event("telephony", "call_connected", meta={"call_sid": call_sid})

    try:
        await websocket.accept()
        
        # Use factory with shared session manager
        manager = create_default_orchestrator(call_logger=call_logger, session_manager=default_session_manager)
        
        await manager.handle_audio_stream(websocket)
        call_logger.log_event("telephony", "call_ended", meta={"reason": "websocket_closed"})
        
    except asyncio.CancelledError:
        logger.warning(f"WebSocket {session_id} cancelled (likely disconnection).")
        call_logger.reason = "user_hangup"
    except Exception as e:
        call_logger.log_event("telephony", "call_failed", meta={"error": str(e)})
        logger.error(f"Media stream error for {session_id}: {e}", exc_info=True)
        call_logger.reason = "error"
    finally:
        # 2. EMERGENCY FLUSH (Forensic Pillar 2) - Mandatory execution
        # Regardless of how we exit (crash, cancel, success), save the audit trace.
        call_logger.generate_summary_line()
        call_logger.save_log()
        logger.info(f"Forensic Audit trace finalized for {session_id}")
        default_session_manager.end_session(session_id)

@app.websocket("/ws/browser")
async def handle_browser_stream(websocket: WebSocket):
    """
    Browser-based Sandbox Mode (Sprint 2.7).
    Bypasses Twilio and uses raw PCM (linear16) at 16kHz for low latency.
    """
    import uuid
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
    WebSocket endpoint for Text-based testing (Mock STT/TTS).
    """
    import uuid
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
        call_logger.save_log()


if __name__ == "__main__":
    PORT = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=PORT)
