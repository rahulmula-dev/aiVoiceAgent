# Voice Orchestrator - Central Logic Manager
import asyncio
import base64
import json
import logging as std_logging
import time
import uuid
from orchestrator.brain import Brain
from orchestrator.interfaces import STTProvider, TTSProvider
from crm.client import CRMClient
from contracts.policy import ResponsePolicyEngine
from contracts.schemas import CallContext
from contracts.state import StateMachine, CallState
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
        self.crm = CRMClient()
        self.synthesizer = tts_provider
        self.brain = Brain(call_logger=call_logger, crm_client=self.crm)
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
        
        # State Machine
        self.state = StateMachine(call_logger=call_logger, crm_client=self.crm)
        
        # Audio Recorder
        self.recorder = None
        
        # Mode: 'audio' (default) or 'text'
        self.mode = "audio"
        self.sid = "unknown" # Default session ID before call starts
        self.last_refusal_time = 0  # Cooldown tracker for non-English refusals
        self.consecutive_empty_frames = 0  # Counter for sustained non-English detection
        self.user_has_spoken = False  # Track if user has spoken at least once

    async def _on_transcript(self, text, confidence=1.0):
        """
        Callback for when user input is received (either via STT or text chat).
        Args:
            text: Transcribed text from user
            confidence: Deepgram confidence score (0.0-1.0), defaults to 1.0 for text chat
        """
        # DEBUG: Log every callback invocation
        trace_id = str(uuid.uuid4())
        logger.debug(f"[CALLBACK TRIGGERED] text='{text}', confidence={confidence:.2f}, state={self.state.current_state}, trace={trace_id}")
        
        # STATE-AWARE NON-ENGLISH DETECTION: Handle empty transcripts from Deepgram
        if not text.strip():
            if confidence == 0.0:
                # We trigger a refusal for SUSTAINED empty frames (likely non-English speech filtered by Deepgram)
                # Threshold increased to 6 to avoid false positives during natural English thinking pauses.
                if self.state.current_state == CallState.LISTENING and self.user_has_spoken:
                    self.consecutive_empty_frames += 1
                    logger.debug(f"[EMPTY FRAME] Count: {self.consecutive_empty_frames} (Pausing or filtered noise)")
                    
                    # Threshold of 6 = approx 4.5 seconds of sustained non-English filtering
                    if self.consecutive_empty_frames >= 6:
                        # RATE LIMITING: Only refuse once every 10 seconds
                        import time
                        current_time = time.time()
                        if current_time - self.last_refusal_time < 10:
                            return
                        
                        logger.warning(f"[NON-ENGLISH DETECTED] {self.consecutive_empty_frames} consecutive empty frames - refusing")
                        
                        # LOGGING: Record the detection and refusal in the call logs
                        if self.call_logger and self.session:
                            self.call_logger.log_event("stt", "user_transcript_final", 
                                                     meta={"text": "[NON-ENGLISH SPEECH DETECTED] (Filtered by Deepgram)"},
                                                     trace_id=trace_id)
                            self.session.conversation_history.append({"role": "user", "parts": ["[NON-ENGLISH SPEECH DETECTED]"]})

                        self.last_refusal_time = current_time
                        self.consecutive_empty_frames = 0
                        
                        refusal_text = "I am currently designed to support English only. Please contact our admission office for assistance."
                        
                        if self.call_logger:
                             self.call_logger.log_event("brain", "chunk_generated", 
                                                      meta={"text": refusal_text, "rag_score": 0.0, "grounding": False, "validation_pass": True},
                                                      trace_id=trace_id)
                        
                        self.response_task = asyncio.create_task(self.speak_refusal(refusal_text, trace_id=trace_id))
                return
            else:
                # Empty with higher confidence = Background silence, reset counter
                if self.consecutive_empty_frames > 0:
                    logger.debug(f"[RESET] Resetting empty frame counter (was {self.consecutive_empty_frames})")
                    self.consecutive_empty_frames = 0
                logger.debug(f"[FILTER] Empty transcript (confidence: {confidence:.2f}) - background silence")
                return
        else:
            # Mark that user has spoken (enable non-English detection)
            if not self.user_has_spoken:
                logger.debug("[FIRST SPEECH] User has spoken - enabling non-English detection")
                self.user_has_spoken = True
            
            # Non-empty transcript received, reset counter
            if self.consecutive_empty_frames > 0:
                logger.debug(f"[RESET] Resetting empty frame counter (was {self.consecutive_empty_frames}) - received text")
                self.consecutive_empty_frames = 0
        
        # LOW-QUALITY DETECTION: Catch mumbled/garbled non-English input
        # Only trigger on NON-EMPTY low-confidence text (avoids feedback loop)
        if confidence < 0.4:
            logger.warning(f"[LOW-CONFIDENCE] Text: '{text}' (Conf: {confidence:.2f}) - triggering clarification")
            self.response_task = asyncio.create_task(
                self.speak_refusal("I didn't quite catch that. Could you please repeat?")
            )
            return
        
        # STATE: Transcribing / Input Received
        if self.mode == "audio":
            self.state.transition_to(CallState.TRANSCRIBING, trace_id=trace_id)
        else:
            self.state.transition_to(CallState.LISTENING, trace_id=trace_id) # Text chat is instant
            
        logger.info(f"USER: {text}")
        if self.session:
            log_conversation_turn(self.session.session_id, "USER", text)
            self.session.conversation_history.append({"role": "user", "parts": [text]})
            self.session.touch()
        
        if self.call_logger:
            self.call_logger.log_event("stt", "user_transcript_final", meta={"text": text}, trace_id=trace_id)
        
        if self.response_task and not self.response_task.done():
            logger.debug(">>> BARGE-IN: Interrupting current AI response...")
            if self.call_logger:
                # Checkpoint: Save logs immediately to prevent data loss
                self.call_logger.save_log(status="in-progress")
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
            # C. Speak Refusal directly (Bypass Brain)
            self.response_task = asyncio.create_task(self.speak_refusal(refusal_text, trace_id=trace_id))
            
            # --- DECISION LOG FOR POLICY REFUSAL ---
            decision_meta = {
                "intent": intent,
                "confidence_score": 1.0, # Checked via deterministic policy
                "chunks_used": [],
                "crm_hit": False,
                "governance_decision": f"Refusal: {intent}",
                "refusal_flags": {"policy_violation": True}
            }
            if self.call_logger:
                self.call_logger.log_event("brain", "decision_trace", meta=decision_meta, trace_id=trace_id)
            logger.info(f"DECISION LOG: [Refusal: {intent}] | Policy Violation")
            # ---------------------------------------
            
            return 

        # 3. Start Parallel Response Generation (Normal Flow)
        self.response_task = asyncio.create_task(self.generate_and_speak(text, intent=intent, trace_id=trace_id))

    async def speak_refusal(self, text, trace_id=None):
        """
        Helper to speak a static refusal message without using the Brain.
        """
        logger.debug(f"Starting Speak Refusal: '{text}'")
        # CRITICAL: Reset empty frame counter when agent speaks
        if self.consecutive_empty_frames > 0:
            logger.debug(f"[RESET] Counter {self.consecutive_empty_frames}→0 (agent speaking)")
            self.consecutive_empty_frames = 0
        # Allow transition to SPEAKING even from ESCALATION
        self.state.transition_to(CallState.SPEAKING, trace_id=trace_id)
        
        try:
            # Add to history so LLM knows it refused
            if self.session:
                self.session.conversation_history.append({"role": "model", "parts": [text]})

            # Speak it with a safety timeout to prevent "dead silence"
            chunks_sent = 0
            async with asyncio.timeout(10.0): # 10s max for refusal tts
                async for chunk in self.synthesizer.speak(text):
                    await self._send_response_chunk(chunk)
                    chunks_sent += 1
            
            logger.info(f"AI (Refusal): {text} (Sent {chunks_sent} audio chunks)")
            if self.session:
                log_conversation_turn(self.session.session_id, "AI", text)
            
            # LOGGING: Record the refusal in JSON logs
            if self.call_logger:
                self.call_logger.log_event("brain", "refusal_spoken", meta={"text": text, "chunks": chunks_sent}, trace_id=trace_id)
        except asyncio.TimeoutError:
            logger.error(f"Refusal TTS Timed Out for text: {text}")
        except Exception as e:
            logger.error(f"Error in speak_refusal: {e}")
        finally:
            # CRITICAL: Always back to Listening so the agent doesn't stay deaf
            self.state.transition_to(CallState.LISTENING, trace_id=trace_id)

    async def handle_audio_stream(self, websocket):
        """
        Main Loop: Coordinates the flow from Twilio (WebSocket) through STT, Brain, and TTS.
        """
        self.websocket = websocket
        self.mode = "audio"
        
        # Set the callback and connect
        self.transcriber.set_callback(self._on_transcript)
        connected = await self.transcriber.connect()
        
        # if not connected: logic removed as we don't await result here
        # We assume connection will succeed or log error in background task

        sid = None
        call_sid = None

        try:
            while True:
                # 1. Wait for 'start' or first 'media' to get Identity
                try:
                    message = await asyncio.wait_for(websocket.receive(), timeout=5.0)
                except asyncio.TimeoutError:
                    if not sid:
                        logger.warning("Identity detection timed out. Using fallback IDs.")
                        import uuid
                        sid = "fallback_" + str(uuid.uuid4())[:12]
                        call_sid = sid
                        self.sid = sid
                    break
                
                if message["type"] == "websocket.disconnect":
                    logger.info("WebSocket disconnected before start.")
                    break
                
                if "text" not in message:
                    continue
                    
                data = json.loads(message["text"])

                if data['event'] == 'start':
                    sid = data['start']['streamSid']
                    call_sid = data['start'].get('callSid', sid)
                    self.sid = sid # Set for _on_transcript
                    break
                
                elif data['event'] == 'media' and not sid:
                    # Rare: media before start. Generate temporary IDs.
                    import uuid
                    sid = "temp_" + str(uuid.uuid4())[:12]
                    call_sid = sid
                    self.sid = sid
                    break

            if not sid:
                return

            # 🟢 ENTER SESSION CONTEXT (Pillar 3)
            # Use 'from' number extracted from Twilio if available
            caller_num = "unknown"
            if self.websocket:
                caller_num = self.websocket.query_params.get("from", "unknown")
            
            async with self.session_manager.session_scope(sid, call_sid, caller_number=caller_num) as session:
                self.session = session
                self.state.transition_to(CallState.CALL_INIT)
                logger.info(f"Telephony Stream Started: {self.session.session_id}")
                
                # Start Recording with provider-specifc settings
                encoding = getattr(self.transcriber, 'encoding', 'mulaw')
                sample_rate = getattr(self.transcriber, 'sample_rate', 8000)
                self.recorder = CallRecorder(
                    self.session.session_id, 
                    encoding=encoding, 
                    sample_rate=sample_rate
                )
                self.recorder.start()

                # Initial Greeting
                self.session_manager.update_state(self.session.session_id, SessionState.SPEAKING)
                greeting = "Hello! I am CILA from GD College."
                self.response_task = asyncio.create_task(self.generate_and_speak(greeting, is_greeting=True))

                # 2. Main Media Loop
                while True:
                    message = await websocket.receive()
                    
                    if message["type"] == "websocket.disconnect":
                        logger.info(f"WebSocket disconnected: {sid}")
                        break
                    
                    if "text" not in message:
                        continue
                        
                    data = json.loads(message["text"])

                    if data['event'] == 'media':
                        # STATE: Listening (Implicitly)
                        if self.state.get_state() != CallState.SPEAKING:
                             self.state.transition_to(CallState.LISTENING)

                        payload = base64.b64decode(data['media']['payload'])
                        if self.recorder:
                            self.recorder.write_chunk(payload)
                        await self.transcriber.send_audio(payload)
                        self.session.touch() # Life signal
                    
                    elif data['event'] == 'stop':
                        logger.debug(f"Telephony Stream Stopped: {sid}")
                        break
        except Exception as e:
            logger.error(f"CRITICAL ORCHESTRATOR CRASH: {e}", exc_info=True)
            if self.session:
                self.session.termination_reason = "system_failure"
            
            # Attempt to play goodbye message if socket still open
            try:
                if self.websocket:
                    goodbye_text = "I am having technical trouble. Please wait while I reconnect you or try calling back later. Goodbye."
                    async for chunk in self.synthesizer.speak(goodbye_text):
                        await self.send_audio_response(chunk)
            except:
                pass
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
        
        # 🟢 ENTER SESSION CONTEXT (Pillar 3) - Fix for Text Mode
        # We need a dummy call_sid for text mode
        call_sid = f"text-{self.sid}"
        
        async with self.session_manager.session_scope(self.sid, call_sid, caller_number="web_chat") as session:
            self.session = session
            self.state.transition_to(CallState.CALL_INIT)
            
            # Initial Greeting
            greeting = "Hello! I am CILA from GD College. (Text Mode)"
            self.response_task = asyncio.create_task(self.generate_and_speak(greeting, is_greeting=True))

            from starlette.websockets import WebSocketDisconnect
            try:
                while True:
                    text = await websocket.receive_text()
                    await self._on_transcript(text)
            except WebSocketDisconnect:
                logger.info(f"Text Chat Disconnected: {self.sid}")
            except Exception as e:
                logger.error(f"Text Orchestrator Error: {e}", exc_info=True)
            # Cleanup is handled by session_scope primarily, but we can keep explicit cleanup if needed for websocket/tasks
            # but session_scope handles the session end.
            # We'll keep manual cleanup for websocket closure if needed, but remove await self.cleanup() if it conflicts.
            # actually manager.py cleanup() does a lot. let's keep it but ensure it doesn't double-close session if scope does it.
            # The session_scope ends the session in session_manager. 
            # cleanup() also calls session_manager.end_session.
            # To be safe and simple: just wrap the loop. 
            finally:
                await self.cleanup()


    async def generate_and_speak(self, text, is_greeting=False, intent="unknown", trace_id=None):
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
                    
                    # STATE: Speaking
                    self.state.transition_to(CallState.SPEAKING, trace_id=trace_id)
                    
                    tts_start_time = time.time()
                    first_chunk_received = False
                    
                    async for chunk in self.synthesizer.speak(sentence):
                        # DEFENSIVE: If task was cancelled during synthesis, stop immediately
                        try:
                            await asyncio.sleep(0) # Yield to let cancellation happen
                        except asyncio.CancelledError:
                            logger.debug("TTS stream interrupted by task cancellation.")
                            raise

                        if not first_chunk_received:
                            first_chunk_received = True
                            tts_latency = int((time.time() - tts_start_time) * 1000)
                            if self.call_logger:
                                self.call_logger.log_event("tts", "audio_stream_start", 
                                                           latency_ms=tts_latency, 
                                                           meta={"text": sentence},
                                                           trace_id=trace_id)
                        await self._send_response_chunk(chunk)
                    audio_queue.task_done()
                
                # Back to Listening when done speaking (if not escalated)
                if self.state.get_state() not in [CallState.ESCALATION, CallState.CALL_END]:
                    self.state.transition_to(CallState.LISTENING, trace_id=trace_id)

            worker_task = asyncio.create_task(tts_worker())

            # 0. Check for Escalation (Policy)
            # STATE: Intent Eval
            self.state.transition_to(CallState.INTENT_EVAL, trace_id=trace_id)
            
            escalation = self.policy.check_escalation(text)
            if escalation:
                self.state.transition_to(CallState.ESCALATION, trace_id=trace_id)
                escalation_msg = "ID 402: Transferring you to a human agent now."
                await audio_queue.put(escalation_msg)
                full_ai_text = escalation_msg
            elif is_greeting:
                self.state.transition_to(CallState.INTENT_EVAL, trace_id=trace_id) # Re-confirm state logic
                await audio_queue.put(text)
                full_ai_text = text
                # Sync hardcoded greeting with session history
                self.session.conversation_history.append({"role": "model", "parts": [text]})
            else:
                # Track LLM Latency
                llm_start_time = time.time()
                if self.call_logger:
                    self.call_logger.log_event("orchestrator", "llm_request_start", trace_id=trace_id)
                
                # We are technically in RAG/Eval state before generating
                # For simplicity, treating "Generating" as INTENT_EVAL -> RESPONSE_VALIDATION flow
                
                # Extract Caller Number for Auto-ID
                caller_num = self.session.caller_number if self.session else "unknown"
                
                async for sentence, metadata in self.brain.generate_stream(text, self.session.conversation_history, caller_number=caller_num, intent=intent, trace_id=trace_id):
                    self.session.touch()
                    self.session_manager.update_state(self.session.session_id, SessionState.SPEAKING)
                    
                    # Policy Check per sentence
                    context = CallContext(
                        session_id=self.session.session_id,
                        caller_number="unknown",
                        start_time=self.session.start_time.timestamp()
                    )
                    
                    is_safe = self.policy.validate_response(context, sentence)
                    
                    # Log Brain Performance
                    if self.call_logger:
                         self.call_logger.log_event("brain", "chunk_generated", meta={
                             "text": sentence, 
                             "rag_score": metadata.get("rag_score", 0),
                             "grounding": metadata.get("has_grounding", False),
                             "validation_pass": is_safe
                         }, trace_id=trace_id)

                    if is_safe:
                        if not full_ai_text: # First sentence logic
                            llm_latency = int((time.time() - llm_start_time) * 1000)
                            if self.call_logger:
                                self.call_logger.log_event("orchestrator", "llm_response_start", latency_ms=llm_latency, trace_id=trace_id)
                        
                        full_ai_text += sentence + " "
                        await audio_queue.put(sentence)
                    else:
                        logger.warning(f"Response Validation Failed (English/Speculation): '{sentence}'")
                        
                        # FAILURE ACTION: English Refusal & Escalation (Policy Rule)
                        self.state.transition_to(CallState.ESCALATION, trace_id=trace_id)
                        failure_msg = "I am currently trained to speak only in English. Please contact our admission office for assistance."
                        await audio_queue.put(failure_msg)
                        full_ai_text = failure_msg
                        
                        asyncio.create_task(self.crm.create_ticket(
                            transcript=f"Blocked Response (Non-English/Speculative): {sentence}\nUser Query: {text}",
                            summary="English-Only Policy/Speculation Violation",
                            sentiment="QUALITY_FAILURE",
                            call_logger=self.call_logger
                        ))
                        
                        # Stop the stream immediately
                        break
            
            await audio_queue.put(None)
            await worker_task
            
            logger.info(f"AI: {full_ai_text.strip()}")
            log_conversation_turn(self.session.session_id, "AI", full_ai_text.strip())
            
            if self.call_logger:
                self.call_logger.save_log(status="in-progress")
            
            # CRM Background Task (Don't block audio)
            if not is_greeting:
                ticket_sentiment = "Neutral"
                ticket_summary = f"Query: {text}"
                
                # Check for KB Miss / Escalation Logic
                # If the AI spoke the mandatory fallback script, we must escalate.
                # Use the Brain's official check (Single Source of Truth)
                if Brain.is_kb_refusal(full_ai_text):
                    ticket_sentiment = "ESCALATION" # Or "High" priority mapping in CRM
                    ticket_summary = f"KB Miss - Escalation Required: {text}"
                    logger.warning(f"KB Miss Detected. Triggering Escalation Ticket for: {text}")

                asyncio.create_task(self.crm.create_ticket(
                    transcript=text,
                    summary=ticket_summary,
                    sentiment=ticket_sentiment,
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

    async def _send_response_chunk(self, chunk):
        """
        Sends a chunk of response (audio or text) to the websocket.
        """
        if not self.websocket:
            return

        try:
            if self.mode == "audio":
                # Pillar 2: Record AI response into the master WAV
                if self.recorder:
                    self.recorder.write_chunk(chunk)

                # Send Media Event
                sid = self.sid or (self.session.session_id if self.session else None)
                if sid:
                    b64_audio = base64.b64encode(chunk).decode('utf-8')
                    await self.websocket.send_text(json.dumps({
                        "event": "media",
                        "streamSid": sid,
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
                
                # Pillar 3: Safety Net - Check for system failure
                reason = self.session.termination_reason
                priority = "normal"
                if reason == "system_failure":
                    priority = "high"
                    logger.warning(f">>> URGENT: Creating high-priority callback ticket for {sid} due to system failure.")

                await self.crm.create_ticket(
                    transcript=history_text,
                    summary=f"Call Session Log ({reason})",
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
