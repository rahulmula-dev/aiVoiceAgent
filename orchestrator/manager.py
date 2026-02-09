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
from agent_logging import log_conversation_turn, CallLogger
from .session_manager import SessionManager, SessionState

logger = std_logging.getLogger("Orchestrator")

class VoiceOrchestrator:
    """
    The Mediator: Connects STT, LLM, TTS, and CRM.
    Maintains ultra-low latency via asynchronous streaming and parallel workers.
    """
    def __init__(self, stt_provider: STTProvider, tts_provider: TTSProvider, 
                 call_logger: CallLogger = None, session_manager: SessionManager = None):
        self.brain = Brain(call_logger=call_logger)
        self.synthesizer = tts_provider
        self.crm = CRMClient()
        self.transcriber = stt_provider
        self.call_logger = call_logger
        
        from .session_manager import default_session_manager
        self.session_manager = session_manager or default_session_manager
        # Note: Collector should be started by the server/app level, not per call.
        
        self.session = None
        self.websocket = None
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
            if not text.strip() or not self.session: return
            
            logger.info(f"USER: {text}")
            log_conversation_turn(self.session.session_id, "USER", text)
            self.session.conversation_history.append({"role": "user", "parts": [text]})
            self.session.touch()
            
            if self.call_logger:
                self.call_logger.log_event("stt", "user_transcript_final", meta={"text": text})
            
            # 1. Barge-In Cancellation
            if self.response_task and not self.response_task.done():
                logger.debug(">>> BARGE-IN: Interrupting current AI response...")
                self.session_manager.update_state(self.session.session_id, SessionState.INTERRUPTED)
                if self.call_logger:
                    self.call_logger.log_event("orchestrator", "user_interruption")
                self.response_task.cancel()
                await self.send_clear_buffer()

            # 2. Start Parallel Response Generation
            self.session_manager.update_state(self.session.session_id, SessionState.THINKING)
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
                    # Extract IDs
                    sid = data['start']['streamSid']
                    call_sid = data['start'].get('callSid', sid) # Pillar 1: Identity
                    
                    # 🟢 ENTER SESSION CONTEXT (Pillar 3)
                    async with self.session_manager.session_scope(sid, call_sid) as session:
                        self.session = session
                        logger.info(f"Telephony Stream Started: {self.session.session_id}")
                        
                        # Start Recording
                        self.recorder = CallRecorder(self.session.session_id)
                        self.recorder.start()

                        # Initial Greeting
                        self.session_manager.update_state(self.session.session_id, SessionState.SPEAKING)
                        greeting = "Hello! I am CILA from GD College."
                        self.response_task = asyncio.create_task(self.generate_and_speak(greeting, is_greeting=True))
                        
                        # Sub-loop to handle subsequent media packets while session is active
                        while True:
                            inner_msg = await websocket.receive_text()
                            inner_data = json.loads(inner_msg)
                            
                            if inner_data['event'] == 'media':
                                payload = base64.b64decode(inner_data['media']['payload'])
                                if self.recorder:
                                    self.recorder.write_chunk(payload)
                                await self.transcriber.send_audio(payload)
                                self.session.touch() # Pillar 3: Life signal
                            
                            elif inner_data['event'] == 'stop':
                                logger.debug("Telephony Stream Stopped.")
                                break
                        break # Exit the outer loop after the stop event

                elif data['event'] == 'stop':
                    logger.debug("Telephony Stream Stopped before start event?")
                    break
        except Exception as e:
            logger.error(f"Orchestrator Error: {e}")
        finally:
            await self.cleanup()

    async def generate_and_speak(self, text, is_greeting=False):
        """
        Streams AI thoughts into a parallel TTS queue for zero-lag audio.
        """
        if not self.session: return
        
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
                # Sync hardcoded greeting with session history
                self.session.conversation_history.append({"role": "model", "parts": [text]})
            else:
                # Track LLM Latency
                llm_start_time = time.time()
                if self.call_logger:
                    self.call_logger.log_event("orchestrator", "llm_request_start")
                
                async for sentence in self.brain.generate_stream(text, self.session.conversation_history):
                    self.session.touch()
                    self.session_manager.update_state(self.session.session_id, SessionState.SPEAKING)
                    
                    # Policy Check per sentence
                    context = CallContext(
                        session_id=self.session.session_id,
                        caller_number="unknown",
                        start_time=self.session.start_time.timestamp()
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

            await audio_queue.put(None)
            await worker_task
            
            logger.info(f"AI: {full_ai_text.strip()}")
            log_conversation_turn(self.session.session_id, "AI", full_ai_text.strip())
            
            # CRM Background Task (Don't block audio)
            if not is_greeting:
                asyncio.create_task(self.crm.create_ticket(
                    transcript=text,
                    summary=f"Query: {text}",
                    sentiment="Neutral",
                    call_logger=self.call_logger
                ))
            
            self.session_manager.update_state(self.session.session_id, SessionState.LISTENING)

        except asyncio.CancelledError:
            logger.info("AI thought-task cancelled by user interruption.")
            # Pillar 1: Identity snapshot 
            if self.session:
                self.session.interruption_snapshot = {"text": text, "timestamp": time.time()}
            if 'worker_task' in locals(): worker_task.cancel()
        except Exception as e:
            logger.error(f"Response Error: {e}")

    async def send_audio_response(self, chunk):
        # Record AI Audio
        if self.recorder:
            self.recorder.write_chunk(chunk)
            
        # Only attempt to send if the websocket is still open (avoid ASGI errors)
        if self.websocket and self.session:
            try:
                b64_audio = base64.b64encode(chunk).decode('utf-8')
                await self.websocket.send_text(json.dumps({
                    "event": "media",
                    "streamSid": self.session.session_id,
                    "media": {"payload": b64_audio}
                }))
            except Exception as e:
                logger.debug(f"Could not send audio (WS likely closed): {e}")

    async def send_clear_buffer(self):
        if self.websocket and self.session:
            try:
                await self.websocket.send_text(json.dumps({
                    "event": "clear",
                    "streamSid": self.session.session_id
                }))
            except:
                pass

    async def cleanup(self):
        """Final session archival and resource release (Pillar 3)."""
        sid = self.session.session_id if self.session else "unknown"
        logger.info(f"Cleanup started for session {sid}.")
        
        # 🟢 CRITICAL: Wrap in try/except (Pillar 3)
        try:
            # 1. Cancel background response task
            if self.response_task and not self.response_task.done():
                logger.debug("Cleanup: Cancelling background response task")
                self.response_task.cancel()
                try:
                    await self.response_task
                except asyncio.CancelledError:
                    pass

            if self.transcriber: 
                logger.debug("Cleanup: Closing Transcriber")
                await self.transcriber.close()
                
            logger.debug("Cleanup: Closing Synthesizer")
            await self.synthesizer.close()
            
            if self.session and self.session.conversation_history:
                logger.debug("Cleanup: Logging full session to CRM")
                history_text = "\n".join([f"{m['role']}: {m['parts'][0]}" for m in self.session.conversation_history])
                await self.crm.create_ticket(
                    transcript=history_text,
                    summary="Full Call Session Log (V2 Session Manager)",
                    sentiment="Final",
                    call_logger=self.call_logger
                )
                
            if self.recorder:
                logger.info(">>> CLEANUP: Saving Recording...")
                self.recorder.close()
                
            # 2. End and remove session from manager (Pillar 2)
            if self.session:
                self.session_manager.end_session(sid)
                
            # 3. Final Log Archival
            if self.call_logger:
                logger.info(f"Cleanup: Generating final summary for {sid}")
                self.call_logger.generate_summary_line(status="completed", reason="user_hangup")
                self.call_logger.save_log(status="completed")
            
        except Exception as e:
            # Fallback to sys.stderr (Pillar 3)
            import sys
            print(f"CRITICAL CLEANUP ERROR for {sid}: {e}", file=sys.stderr)
        finally:
            logger.info(f"Orchestrator session {sid} finalized.")
