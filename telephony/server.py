# Telephony Server - Twilio Entry Point (Modular v2)
import os
import logging
import uvicorn
from datetime import datetime
from fastapi import FastAPI, WebSocket, Request, Response
from orchestrator.manager import VoiceOrchestrator
from orchestrator.factory import create_default_orchestrator
from dotenv import load_dotenv

load_dotenv()

from logging import bind_call_context, CallLogger

app = FastAPI()

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
    
    logging.getLogger("Server").debug(f"DEBUG: Incoming Voice Webhook. SID: {call_sid}, From: {from_number}")
    
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
    import uuid
    
    await websocket.accept()
    
    # Generate a unique short session ID
    session_id = str(uuid.uuid4())[:8]
    
    # Try to extract from query params first (passed from TwiML)
    query_params = websocket.query_params
    from_number = query_params.get("from", "Unknown")

    # BIND CONTEXT TO LOGGER (using contextvars)
    bind_call_context(session_id, from_number)
    
    # Initialize the Black-Box Call Logger
    call_logger = CallLogger(call_id=session_id, caller_number=from_number)
    call_logger.log_event("telephony", "call_connected")
    
    # Use factory to create orchestrator (decoupled from provider implementations)
    manager = create_default_orchestrator(call_logger=call_logger)
    
    try:
        await manager.handle_audio_stream(websocket)
        call_logger.log_event("telephony", "call_ended", meta={"reason": "websocket_closed"})
    except Exception as e:
        status = "failed"
        call_logger.log_event("telephony", "call_failed", meta={"error": str(e)})
        raise
    finally:
        # Final cleanup is now handled inside manager.cleanup()
        # which is called in manager.handle_audio_stream's own finally block.
        # This provides better guarantees for archival.
        pass

if __name__ == "__main__":
    PORT = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=PORT)
