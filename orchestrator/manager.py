# Voice Orchestrator - Central Logic Manager
import asyncio
import base64
import json
import logging as std_logging
import time
from orchestrator.brain import Brain
from orchestrator.interfaces import STTProvider, TTSProvider
from crm.client import CRMClient
from contracts.policy import ResponsePolicyEngine
from contracts.schemas import CallContext
from audit_logging.recorder import CallRecorder
from logging import log_conversation_turn, CallLogger

logger = std_logging.getLogger("Orchestrator")

class VoiceOrchestrator:
    """
    The Mediator: Connects STT, LLM, TTS, and CRM.
    Maintains ultra-low latency via asynchronous streaming and parallel workers.
    """
    def __init__(self, stt_provider: STTProvider, tts_provider: TTSProvider, call_logger: CallLogger = None):
        self.brain = Brain(call_logger=call_logger)
        self.synthesizer = tts_provider
        self.crm = CRMClient()
        self.transcriber = stt_provider
        self.call_logger = call_logger
        self.sid = None
        self.websocket = None
        
        # Session State
        self.session_transcript = []
        self.chat_history = self.brain.start_new_session()
        self.response_task = None
        
        # Policy Engine
        self.policy = ResponsePolicyEngine()
        
        # Audio Recorder
        self.recorder = None

    async def handle_audio_stream(self, websocket):
        """
        Main Loop: Coordinates the flow from Twilio (WebSocket) through STT, Brain, and TTS.
        """
        self.websocket = websocket
        
        # Define the callback: What happens when STT hears text?
        async def on_transcript(text):
            if not text.strip(): return
            
            logger.info(f"USER: {text}")
            log_conversation_turn(self.sid, "USER", text)
            self.session_transcript.append(f"User: {text}")
            
            if self.call_logger:
                self.call_logger.log_event("stt", "user_transcript_final", meta={"text": text})
            
            # 1. Barge-In Cancellation
            if self.response_task and not self.response_task.done():
                logger.debug(">>> BARGE-IN: Interrupting current AI response...")
                if self.call_logger:
                    self.call_logger.log_event("orchestrator", "user_interruption")
                self.response_task.cancel()
                await self.send_clear_buffer()

            # 2. Start Parallel Response Generation
            self.response_task = asyncio.create_task(self.generate_and_speak(text))

        # Set the callback and connect
        self.transcriber.set_callback(on_transcript)
        connected = await self.transcriber.connect()
        
        if not connected:
            logger.error("Failed to connect to STT engine.")
            await websocket.close()
            return

        try:
            while True:
                message = await websocket.receive_text()
                data = json.loads(message)

                if data['event'] == 'start':
                    self.sid = data['start']['streamSid']
                    logger.info(f"Telephony Stream Started: {self.sid}")
                    
                    # Start Recording
                    self.recorder = CallRecorder(self.sid)
                    self.recorder.start()

                    logger.debug(f"Telephony Stream Started: {self.sid}")
                    # Initial Greeting - Just introduce, don't ask "how can I help" (user will speak naturally)
                    greeting = "Hello! I am CILA from GD College."
                    self.response_task = asyncio.create_task(self.generate_and_speak(greeting, is_greeting=True))

                elif data['event'] == 'media':
                    payload = base64.b64decode(data['media']['payload'])
                    if self.recorder:
                        self.recorder.write_chunk(payload)
                    await self.transcriber.send_audio(payload)

                elif data['event'] == 'stop':
                    logger.debug("Telephony Stream Stopped.")
                    break
        except Exception as e:
            logger.error(f"Orchestrator Error: {e}")
        finally:
            await self.cleanup()

    async def generate_and_speak(self, text, is_greeting=False):
        """
        Streams AI thoughts into a parallel TTS queue for zero-lag audio.
        """
        try:
            full_ai_text = ""
            audio_queue = asyncio.Queue()

            # Worker: Speaks chunks as they arrive from the brain
            async def tts_worker():
                while True:
                    sentence = await audio_queue.get()
                    if sentence is None: break
                    
                    tts_start_time = time.time()
                    first_chunk_received = False
                    
                    async for chunk in self.synthesizer.speak(sentence):
                        if not first_chunk_received:
                            first_chunk_received = True
                            tts_latency = int((time.time() - tts_start_time) * 1000)
                            if self.call_logger:
                                self.call_logger.log_event("tts", "audio_stream_start", 
                                                           latency_ms=tts_latency, 
                                                           meta={"text": sentence[:50]})
                        await self.send_audio_response(chunk)
                    audio_queue.task_done()

            worker_task = asyncio.create_task(tts_worker())

            # 0. Check for Escalation (Policy)
            escalation = self.policy.check_escalation(text)
            if escalation:
                escalation_msg = "ID 402: Transferring you to a human agent now."
                await audio_queue.put(escalation_msg)
                full_ai_text = escalation_msg
            elif is_greeting:
                await audio_queue.put(text)
                full_ai_text = text
                # Sync hardcoded greeting with chat history so LLM knows it has already introduced itself
                self.chat_history.append({"role": "model", "parts": [text]})
            else:
                # Track LLM Latency
                llm_start_time = time.time()
                if self.call_logger:
                    self.call_logger.log_event("orchestrator", "llm_request_start")
                
                async for sentence in self.brain.generate_stream(text, self.chat_history):
                    # Policy Check per sentence
                    context = CallContext(
                        session_id=self.sid or "unknown",
                        caller_number="unknown",
                        start_time=0.0
                    )
                    if self.policy.validate_response(context, sentence):
                        full_ai_text += sentence + " "
                        await audio_queue.put(sentence)
                    else:
                        logger.warning(f"Policy Blocked Sentence: {sentence}")
                    if not full_ai_text: # First sentence
                        llm_latency = int((time.time() - llm_start_time) * 1000)
                        if self.call_logger:
                            self.call_logger.log_event("orchestrator", "llm_response_start", latency_ms=llm_latency)
                    
                    full_ai_text += sentence + " "
                    await audio_queue.put(sentence)

            await audio_queue.put(None)
            await worker_task
            
            logger.info(f"AI: {full_ai_text.strip()}")
            log_conversation_turn(self.sid, "AI", full_ai_text.strip())
            self.session_transcript.append(f"AI: {full_ai_text.strip()}")
            
            # CRM Background Task (Don't block audio)
            if not is_greeting:
                asyncio.create_task(self.crm.create_ticket(
                    transcript=text,
                    summary=f"Query: {text}",
                    sentiment="Neutral",
                    call_logger=self.call_logger
                ))

        except asyncio.CancelledError:
            logger.info("AI thought-task cancelled by user interruption.")
            if 'worker_task' in locals(): worker_task.cancel()
        except Exception as e:
            logger.error(f"Response Error: {e}")

    async def send_audio_response(self, chunk):
        # Record AI Audio
        if self.recorder:
            self.recorder.write_chunk(chunk)
            
        # Only attempt to send if the websocket is still open (avoid ASGI errors)
        if self.websocket and self.sid:
            try:
                b64_audio = base64.b64encode(chunk).decode('utf-8')
                await self.websocket.send_text(json.dumps({
                    "event": "media",
                    "streamSid": self.sid,
                    "media": {"payload": b64_audio}
                }))
            except Exception as e:
                logger.debug(f"Could not send audio (WS likely closed): {e}")

    async def send_clear_buffer(self):
        if self.websocket and self.sid:
            try:
                await self.websocket.send_text(json.dumps({
                    "event": "clear",
                    "streamSid": self.sid
                }))
            except:
                pass

    async def cleanup(self):
        """Final session archival and resource release."""
        logger.info(f"Cleanup started for session {self.sid}. CallLogger present: {self.call_logger is not None}")
        
        # 1. Cancel background response task if still running
        if self.response_task and not self.response_task.done():
            logger.debug("Cleanup: Cancelling background response task")
            self.response_task.cancel()
            try:
                await self.response_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Error during response task cancellation: {e}")

        if self.transcriber: 
            logger.debug("Cleanup: Closing Transcriber")
            await self.transcriber.close()
            
        logger.debug("Cleanup: Closing Synthesizer")
        await self.synthesizer.close()
        
        if self.session_transcript:
            logger.debug("Cleanup: Logging session to CRM")
            await self.crm.create_ticket(
                transcript="\n".join(self.session_transcript),
                summary="Full Call Session Log",
                sentiment="Final",
                call_logger=self.call_logger
            )
            
        if self.recorder:
            logger.info(">>> CLEANUP: Saving Recording...")
            self.recorder.close()
            
        logger.info("Orchestrator session finalized.")
        logger.info(">>> CLEANUP: Done.")

        # 2. FINAL LOG ARCHIVAL (Moved here for guaranteed execution)
        # CRITICAL: Wrap in broad try/except to prevent logging errors from crashing cleanup
        if self.call_logger:
            try:
                # CHANGED: debug -> info
                logger.info(f"Cleanup: Generating final summary for {self.sid}")
                self.call_logger.generate_summary_line(status="completed", reason="user_hangup")
                self.call_logger.save_log(status="completed")
                
                # CHANGED: already info
                logger.info(f"Cleanup: Successfully saved call log for {self.sid}")
                
            except Exception as e:
                # KEEP THIS: Writing to stderr is valid when the logger itself might be broken.
                import sys
                error_msg = f"CRITICAL: Failed to save call log for {self.sid}: {type(e).__name__}: {str(e)}"
                print(error_msg, file=sys.stderr) 
                
                # But also try to log it if the handler is still alive
                logger.error(f"Failed to finalize call logs in cleanup: {e}", exc_info=True)
        else:
            logger.warning(f"call_logger is None during cleanup for session {self.sid}")

        logger.debug("Orchestrator session finalized.")
