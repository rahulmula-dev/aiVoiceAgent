# Telephony Server - Twilio Entry Point (Modular v2)
import os
import logging
import uvicorn
from datetime import datetime
from fastapi import FastAPI, WebSocket, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from orchestrator.manager import VoiceOrchestrator
from orchestrator.factory import create_default_orchestrator, create_custom_orchestrator
from orchestrator.mocks import MockSTT, MockTTS
from orchestrator.factory import create_default_orchestrator
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

@app.get("/")
async def root():
    return {"status": "GD College AI Voice Agent [Modular v2] - Active"}

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
    
    # Determine public ngrok URL (from environment variable)
    public_url = os.getenv("NGROK_URL")
    if public_url:
        host = public_url.replace("https://", "").replace("http://", "")
    else:
        host = request.headers.get("host")

    twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Connect><Stream url="wss://{host}/media-stream?sid={call_sid}&amp;from={from_number}" /></Connect></Response>'
    return Response(content=twiml, media_type="application/xml")

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
    from_number = query_params.get("from", "Unknown")

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
    await manager.handle_audio_stream(websocket)
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
        call_logger=call_logger # Logger now passed
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
