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
from contracts.state import StateMachine, CallState
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
        
        # State Machine
        self.state = StateMachine(call_logger=call_logger, crm_client=self.crm)
        
        # Audio Recorder
        self.recorder = None
        
        # Mode: 'audio' (default) or 'text'
        self.mode = "audio"

    async def _on_transcript(self, text):
        """
        Callback for when user input is received (either via STT or text chat).
        """
        if not text.strip(): return
        
        # STATE: Transcribing / Input Received
        if self.mode == "audio":
            self.state.transition_to(CallState.TRANSCRIBING)
        else:
            self.state.transition_to(CallState.LISTENING) # Text chat is instant
            
        logger.info(f"USER: {text}")
        log_conversation_turn(self.sid, "USER", text)
        self.session_transcript.append(f"User: {text}")
        
        if self.call_logger:
            self.call_logger.log_event("stt", "user_transcript_final", meta={"text": text})
        
        if self.response_task and not self.response_task.done():
            logger.debug(">>> BARGE-IN: Interrupting current AI response...")
            if self.call_logger:
                self.call_logger.log_event("orchestrator", "user_interruption")
            self.response_task.cancel()
            await self.send_clear_buffer()

        # 2. SECURITY & POLICY CHECK (Pre-Brain)
        # Check for hard refusals or sensitive topics BEFORE touching the LLM/DB
        intent = self.policy.classify_intent(text)
        
        if intent != "PROCEED":
            logger.warning(f"POLICY VIOLATION: {intent} detected for input: {text}")
            
            # A. Get Refusal Script
            refusal_text = self.policy.get_refusal_script(intent)
            
            # B. Log to CRM
            asyncio.create_task(self.crm.create_ticket(
                transcript=f"User said: {text}\nPolicy Trigger: {intent}",
                summary=f"Security Violation: {intent}",
                sentiment="SECURITY_ALERT",
                call_logger=self.call_logger
            ))
            
            # C. Speak Refusal directly (Bypass Brain)
            # We wrap this in a task just like generate_and_speak to maintain consistency
            self.response_task = asyncio.create_task(self.speak_refusal(refusal_text))
            return 

        # 3. Start Parallel Response Generation (Normal Flow)
        self.response_task = asyncio.create_task(self.generate_and_speak(text))

    async def speak_refusal(self, text):
        """
        Helper to speak a static refusal message without using the Brain.
        """
        self.state.transition_to(CallState.SPEAKING)
        full_ai_text = text
        
        # Add to history so LLM knows it refused
        self.chat_history.append({"role": "model", "parts": [text]})
        self.session_transcript.append(f"AI: {text}")

        # Speak it
        async for chunk in self.synthesizer.speak(text):
            await self._send_response_chunk(chunk)
            
        logger.info(f"AI (Refusal): {text}")
        log_conversation_turn(self.sid, "AI", text)
        
        # Back to Listening
        self.state.transition_to(CallState.LISTENING)

    async def handle_audio_stream(self, websocket):
        """
        Main Loop: Coordinates the flow from Twilio (WebSocket) through STT, Brain, and TTS.
        """
        self.websocket = websocket
        self.mode = "audio"
        
        # Set the callback and connect
        self.transcriber.set_callback(self._on_transcript)
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
                    
                    # STATE: Init
                    self.state.transition_to(CallState.CALL_INIT)

                    # Start Recording
                    self.recorder = CallRecorder(self.sid)
                    self.recorder.start()

                    logger.debug(f"Telephony Stream Started: {self.sid}")
                    # Initial Greeting
                    greeting = "Hello! I am CILA from GD College."
                    self.response_task = asyncio.create_task(self.generate_and_speak(greeting, is_greeting=True))

                elif data['event'] == 'media':
                    # STATE: Listening (Implicitly, every time we get audio, we are listening)
                    if self.state.get_state() != CallState.SPEAKING:
                         # Don't flap state if speaking
                         self.state.transition_to(CallState.LISTENING)

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

    async def handle_text_stream(self, websocket):
        """
        Text Chat Loop: For testing logic without audio/STT/TTS.
        """
        self.websocket = websocket
        self.mode = "text"
        import uuid
        self.sid = str(uuid.uuid4())[:8] # Generate a temporary session ID

        logger.info(f"Text Chat Started: {self.sid}")
        self.state.transition_to(CallState.CALL_INIT)
        
        # Initial Greeting
        greeting = "Hello! I am CILA from GD College. (Text Mode)"
        self.response_task = asyncio.create_task(self.generate_and_speak(greeting, is_greeting=True))

        try:
            while True:
                text = await websocket.receive_text()
                await self._on_transcript(text)
        except Exception as e:
            logger.error(f"Text Orchestrator Error: {e}")
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
                    
                    # STATE: Speaking
                    self.state.transition_to(CallState.SPEAKING)
                    
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
                        await self._send_response_chunk(chunk)
                    audio_queue.task_done()
                
                # Back to Listening when done speaking
                self.state.transition_to(CallState.LISTENING)

            worker_task = asyncio.create_task(tts_worker())

            # 0. Check for Escalation (Policy)
            # STATE: Intent Eval
            self.state.transition_to(CallState.INTENT_EVAL)
            
            escalation = self.policy.check_escalation(text)
            if escalation:
                self.state.transition_to(CallState.ESCALATION)
                escalation_msg = "ID 402: Transferring you to a human agent now."
                await audio_queue.put(escalation_msg)
                full_ai_text = escalation_msg
            elif is_greeting:
                self.state.transition_to(CallState.INTENT_EVAL) # Re-confirm state logic
                await audio_queue.put(text)
                full_ai_text = text
                # Sync hardcoded greeting with chat history so LLM knows it has already introduced itself
                self.chat_history.append({"role": "model", "parts": [text]})
            else:
                # Track LLM Latency
                llm_start_time = time.time()
                if self.call_logger:
                    self.call_logger.log_event("orchestrator", "llm_request_start")
                
                # We are technically in RAG/Eval state before generating
                # For simplicity, treating "Generating" as INTENT_EVAL -> RESPONSE_VALIDATION flow
                
                async for sentence, metadata in self.brain.generate_stream(text, self.chat_history):
                    # Policy Check per sentence (Story S1-4)
                    self.state.transition_to(CallState.RESPONSE_VALIDATION)
                    
                    context = CallContext(
                        session_id=self.sid or "unknown",
                        caller_number="unknown",
                        start_time=0.0
                    )
                    
                    is_safe = self.policy.validate_response(context, sentence)
                    
                    # Log Brain Performance
                    if self.call_logger:
                         self.call_logger.log_event("brain", "chunk_generated", meta={
                             "text": sentence[:20], 
                             "rag_score": metadata.get("rag_score", 0),
                             "grounding": metadata.get("has_grounding", False),
                             "validation_pass": is_safe
                         })

                    if is_safe:
                        if not full_ai_text: # First sentence logic
                            llm_latency = int((time.time() - llm_start_time) * 1000)
                            if self.call_logger:
                                self.call_logger.log_event("orchestrator", "llm_response_start", latency_ms=llm_latency)
                        
                        full_ai_text += sentence + " "
                        await audio_queue.put(sentence)
                    else:
                        logger.warning(f"Response Validation Failed: '{sentence}'")
                        
                        # FAILURE ACTION: End Call (Story S1-4)
                        # We stop the stream, speak specific failure message, and hang up.
                        failure_msg = "I can only respond in English and provide factual information."
                        await audio_queue.put(failure_msg)
                        full_ai_text += failure_msg
                        
                        asyncio.create_task(self.crm.create_ticket(
                            transcript=f"Blocked Response: {sentence}\nUser Query: {text}",
                            summary="Quality Validation Failure (Speculation/Hallucination)",
                            sentiment="QUALITY_FAILURE",
                            call_logger=self.call_logger
                        ))
                        
                        # Signal loop termination
                        break

            await audio_queue.put(None)
            await worker_task
            
            # If we broke out due to validation failure, trigger Call End
            if "I can only respond in English" in full_ai_text:
                 self.state.transition_to(CallState.CALL_END)
                 # Close connection after speaking is done
                 # In a real scenario, we might wait for the 'speaking' event to finish, 
                 # but for now we let the worker finish the failure message and then close.
                 if self.websocket:
                     await asyncio.sleep(3) # Give time to speak
                     await self.websocket.close()
            
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

    async def _send_response_chunk(self, chunk):
        """
        Sends a chunk of response (audio or text) to the websocket.
        """
        # Only attempt to send if the websocket is still open (avoid ASGI errors)
        if self.websocket:
            try:
                if self.mode == "audio":
                    # Audio Mode (Twilio)
                    if self.sid:
                        b64_audio = base64.b64encode(chunk).decode('utf-8')
                        await self.websocket.send_text(json.dumps({
                            "event": "media",
                            "streamSid": self.sid,
                            "media": {"payload": b64_audio}
                        }))
                else:
                    # Text Mode (Chat) - chunk is bytes(text)
                    text_chunk = chunk.decode('utf-8')
                    await self.websocket.send_text(text_chunk)
            except Exception as e:
                logger.debug(f"Could not send response chunk: {e}")

    async def send_audio_response(self, chunk):
        # Legacy/Public method wrapper
        self.mode = "audio" 
        await self._send_response_chunk(chunk)

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
        
        # STATE: Call End
        try:
            self.state.transition_to(CallState.CALL_END)
        except:
            pass # Swallow errors during cleanup
        
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
