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
from contracts.policy import ResponsePolicyEngine, PRDScripts
from contracts.schemas import CallContext
from contracts.state import StateMachine, CallState
from audit_logging.recorder import CallRecorder
from agent_logging import log_conversation_turn, CallLogger
from .session_manager import SessionManager, SessionState
from orchestrator.context_extractor import ContextManager
from contracts.config import FeatureConfig

# Task 4 & 5: Latency Management
class LatencyBreachError(Exception):
    """Raised when turn processing exceeds the 5.0s hard safety limit."""
    pass

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
        self.last_empty_frame_time = 0 # Debounce tracker for STT burst artifacts
        self.consecutive_empty_frames = 0  # Counter for sustained non-English detection
        self.non_english_run_start = time.time()  # Use time.time() NOT 0.0 — avoids epoch gap bug
        self.user_has_spoken = False  # Track if user has spoken at least once
        self.language_strike_count = 0 # Strike tracker for non-English input (Task 3.4)
        
        # Feature Config
        self.config = FeatureConfig()

        # Context Manager (Story S4-9)
        self.context_manager = ContextManager()

        # Silence Handling State (Story S4-2)
        self.silence_task = None
        self.silence_stage = 0  # 0: Normal, 1: Warned once, 2: Warned twice
        self.last_interaction_time = time.time()

        # Cleanup guard: prevents double-cleanup from concurrent disconnect + silence termination
        self._cleanup_done = False

    def _create_task_with_log(self, coro):
        """
        Safety wrapper for fire-and-forget background tasks.
        Ensures exceptions are logged instead of swallowed.
        """
        task = asyncio.create_task(coro)
        def log_exception(t):
            try:
                t.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Generate/CRM Background Task Failed: {e}", exc_info=True)
        task.add_done_callback(log_exception)
        return task

    async def _on_transcript(self, text: str, confidence: float, stt_latency: float = 0.0, is_final: bool = False, detected_lang: str = None):
        """
        [GOVERNANCE] Expert Debugger Entry Point.
        """
        raw_text = text.strip() if text else ""
        
        # 1. AGGRESSIVE LOGGING: We must see what the STT actually heard
        # [STT RAW] is used by the Expert Debugger to diagnose "deafness" or "hallucinations"
        # Progress counter added for better forensic visibility
        logger.info(f"[STT RAW] Text: '{raw_text}' | is_final: {is_final} (Counter: {self.consecutive_empty_frames})")

        if not is_final:
            return # Only process complete sentences.
            
        # 2. Run the failsafe policy (EXACT logic per Debugger Plan)
        # If there is text, run the char-ratio/langdetect guard.
        if raw_text:
            is_eng = self.policy._is_english(raw_text, detected_lang=detected_lang)
            
            if not is_eng:
                logger.warning(f"[ORCHESTRATOR] Language violation caught: '{raw_text}'")
                # INCREMENT PERMANENT STRIKE COUNTER HERE
                self.language_strike_count += 1
                
                # TRIGGER 3-STRIKE REFUSAL AUDIO (Dynamic based on count)
                trace_id = str(uuid.uuid4())
                if self.language_strike_count == 1:
                    refusal_text = PRDScripts.REFUSAL_LANGUAGE_1
                elif self.language_strike_count == 2:
                    refusal_text = PRDScripts.REFUSAL_LANGUAGE_2
                else: # Strike 3+
                    refusal_text = PRDScripts.REFUSAL_LANGUAGE_3

                if self.language_strike_count >= 3:
                    logger.warning("[GOVERNANCE] Strike 3 — initiating graceful termination flow.")
                    self.response_task = asyncio.create_task(self._language_termination_flow(refusal_text, trace_id))
                else:
                    if self.response_task and not self.response_task.done():
                        self.response_task.cancel()
                    self.response_task = asyncio.create_task(self.speak_refusal(refusal_text, trace_id=trace_id))
                
                return # CRITICAL: Return so it never hits the LLM
        
        # ... proceed with English processing ...
        # Task 3 & 4: Start Turn Timer
        turn_start_time = asyncio.get_event_loop().time()
        trace_id = str(uuid.uuid4())

        # STATE-AWARE NON-ENGLISH DETECTION: Handle empty transcripts from Deepgram
        if not text.strip():
            if confidence == 0.0:
                # ── STATE GUARD: Only count empty frames when actively LISTENING ──────────────
                # When the AI is SPEAKING, Deepgram naturally receives silence from the
                # microphone and sends empty+0.0 frames. These are NOT non-English speech.
                # We must NOT count them or English speakers will get false strikes.
                current_call_state = self.state.get_state()
                if current_call_state != CallState.LISTENING:
                    logger.debug(f"[DEBOUNCE] Ignoring empty frame in state {current_call_state} (not LISTENING)")
                    return

                # ── STATE GUARD: Only start counting AFTER user has spoken once ──────────────
                # Before the user speaks, empty frames are just background/mic noise.
                if not self.user_has_spoken:
                    logger.debug("[DEBOUNCE] Ignoring empty frame before first speech")
                    return

                # ── DEBOUNCE FIX: Filter millisecond-interval STT burst artifacts ────────────
                # Deepgram sometimes sends multiple empty frames in <50ms, which 
                # incorrectly triggers strikes. We require 200ms between increments.
                stt_current_time = time.time()
                if stt_current_time - self.last_empty_frame_time < 0.2:
                    logger.debug(f"[DEBOUNCE] Filtering STT burst artifact for {self.sid}")
                    return
                
                self.last_empty_frame_time = stt_current_time
                
                # Track the start of this non-English "run" (resets when English text comes in)
                if self.consecutive_empty_frames == 0:
                    self.non_english_run_start = time.time()  # Fresh start for each new run
                self.consecutive_empty_frames += 1
                non_english_duration = time.time() - self.non_english_run_start
                
                logger.debug(f"[PHONEME] Empty frame #{self.consecutive_empty_frames}, run={non_english_duration:.1f}s")
                
                # ── DUAL CONDITION GATE ──────────────────────────────────────────────────────
                # Fire a language strike ONLY when BOTH conditions are met:
                #   1. At least 4 frames in this run (rules out a single noise spike)
                #   2. Run has been active ≥ 12 seconds (rules out fast background noise bursts
                #      and normal English thinking pauses which are always < 10s)
                # 12s > silence monitor's 10s threshold → silence fires first for truly silent
                # users; for active non-English speakers the run accumulates across sentences.
                # ─────────────────────────────────────────────────────────────────────────────
                if self.consecutive_empty_frames >= 4 and non_english_duration >= 12.0:
                        # ── FORENSIC FIX: Route through PERMANENT STRIKE SYSTEM ────────────────
                        # Hindi/Bengali/Mandarin arrive as transcript='', confidence=0.0,
                        # speech_final=true because 8kHz mulaw cannot produce non-Latin phonemes.
                        # We MUST use the same strike counter as the text-based path so that 
                        # violations accumulate across BOTH detection methods.
                        # ───────────────────────────────────────────────────────────────────────
                        current_time = time.time()
                        if current_time - self.last_refusal_time < 10:
                            return

                        self.language_strike_count += 1
                        logger.warning(f"[GOVERNANCE] Unrecognized-language speech detected (empty+0.0 frames). Strike: {self.language_strike_count}/3")
                        
                        # LOGGING: Record in call logs
                        if self.call_logger and self.session:
                            self.call_logger.log_event("stt", "user_transcript_final",
                                                     meta={"text": f"[UNRECOGNIZED LANGUAGE SPEECH] Strike {self.language_strike_count}/3"},
                                                     trace_id=trace_id)
                            self.call_logger.log_event("brain", "decision_trace", meta={
                                "intent": "HARD_REFUSAL_LANGUAGE",
                                "confidence_score": 1.0,
                                "chunks_used": [],
                                "crm_hit": False,
                                "governance_decision": "Blocked",
                                "refusal_flags": {"strike_count": self.language_strike_count, "method": "phoneme_empty"}
                            }, trace_id=trace_id)
                            self.session.conversation_history.append({"role": "user", "parts": [f"[UNRECOGNIZED LANGUAGE SPEECH - Strike {self.language_strike_count}]"]})
                        
                        # CRM Ticket — fire on every strike
                        self._create_task_with_log(self.crm.create_ticket(
                            transcript=f"[SYSTEM] Unrecognized phonemes (Hindi/Bengali/etc.). Strike {self.language_strike_count}/3.",
                            summary=f"Security Violation: HARD_REFUSAL_LANGUAGE (phoneme, Strike {self.language_strike_count})",
                            sentiment="SECURITY_ALERT",
                            call_logger=self.call_logger,
                            call_id=self.session.crm_call_id or self.session.session_id if self.session else trace_id,
                            title=f"Policy: Language Barrier Strike {self.language_strike_count}"
                        ))
                        
                        self.last_refusal_time = current_time
                        self.consecutive_empty_frames = 0
                        self.non_english_run_start = time.time()  # Reset to NOW, not 0.0

                        # Pick refusal script by strike number
                        if self.language_strike_count == 1:
                            refusal_text = PRDScripts.REFUSAL_LANGUAGE_1
                        elif self.language_strike_count == 2:
                            refusal_text = PRDScripts.REFUSAL_LANGUAGE_2
                        else:
                            refusal_text = PRDScripts.REFUSAL_LANGUAGE_3

                        # Strike 3: Graceful termination
                        if self.language_strike_count >= 3:
                            logger.warning("[GOVERNANCE] Strike 3 (phoneme path) — initiating graceful termination flow.")
                            self.response_task = asyncio.create_task(self._language_termination_flow(refusal_text, trace_id))
                        else:
                            self.response_task = asyncio.create_task(self.speak_refusal(refusal_text, trace_id=trace_id))
                    
                # COORDINATION: Do NOT update last_interaction_time for empty frames.
                # 
                # If we reset it here, the silence monitor can never fire for a user who
                # goes silent (because empty frames keep resetting the timer).
                # 
                # For non-English speakers: last_interaction_time is reset by speak_refusal
                # after each language warning, giving them time to respond in English.
                # For truly silent users: last_interaction_time was last set by AI speaking,
                # so the silence monitor fires correctly at 10s.
                #
                # NOTE: last_interaction_time intentionally NOT reset here.
                # NOTE: silence_stage intentionally NOT reset here.
                    
                return # 🟢 FIXED: Return here to prevent fall-through to clarification check

            else:
                # Empty with higher confidence = Background silence, reset counter
                if self.consecutive_empty_frames > 0:
                    logger.debug(f"[RESET] Resetting empty frame counter (was {self.consecutive_empty_frames})")
                    self.consecutive_empty_frames = 0
                    self.non_english_run_start = time.time()  # Anchor to now, never epoch
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
                self.non_english_run_start = time.time()  # Reset run timer — English resets everything
            
            # SILENCE RESET: legitimate user text
            self.last_interaction_time = time.time()
            self.silence_stage = 0
        
        # LOW-QUALITY DETECTION: Catch mumbled/garbled non-English input
        # Only trigger on NON-EMPTY low-confidence text (avoids feedback loop)
        if confidence < 0.4:
            logger.warning(f"[LOW-CONFIDENCE] Text: '{text}' (Conf: {confidence:.2f}) - triggering clarification")
            self.response_task = asyncio.create_task(
                self.speak_refusal(PRDScripts.APOLOGY_CLARIFICATION)
            )
            return
        
        # STATE: Transcribing / Input Received
        if self.mode == "audio":
            self.state.transition_to(CallState.TRANSCRIBING, trace_id=trace_id)
        else:
            self.state.transition_to(CallState.LISTENING, trace_id=trace_id) # Text chat is instant
            
        logger.info(f"USER: {text}")
        if self.session:
            # [FORENSIC FIX]: Mark that user has spoken to enable stricter empty-frame governance
            if not self.user_has_spoken:
                logger.debug("[GOVERNANCE] First valid transcript received. Enabling strict empty-frame checks.")
                self.user_has_spoken = True
                
            log_conversation_turn(self.session.session_id, "USER", text)
            self.session.conversation_history.append({"role": "user", "parts": [text]})
            self.session.touch()
        
        if self.call_logger:
            self.call_logger.log_event("stt", "user_transcript_final", 
                                     latency_ms=int(stt_latency * 1000), 
                                     meta={"text": text, "confidence": confidence}, 
                                     trace_id=trace_id)
        
        # 2. SECURITY & POLICY CHECK (Pre-Brain & Pre-Barge-in)
        # [GOVERNANCE] CRITICAL: Policy check MUST execute before barge-in handling.
        # This prevents users from bypassing the gate by interrupting the AI.
        intent = self.policy.classify_intent(text, detected_lang=detected_lang)
        logger.debug(f"[GOVERNANCE] Input: '{text}', Intent: {intent}, Strike: {self.language_strike_count} | Detected Lang: {detected_lang}")
        
        # [FORENSIC FIX]: If the user speaks valid English (Anything EXCEPT a language refusal),
        # we reset the language strike counter. This ensures that one-off language mistakes
        # or STT glitches don't lead to unfair terminations if the user is otherwise speaking English.
        if intent != "HARD_REFUSAL_LANGUAGE":
            if self.language_strike_count > 0:
                logger.info(f"[RESET] Resetting language strike counter (was {self.language_strike_count}) - valid English received")
                self.language_strike_count = 0
        
        # --- RAG SHORT-CIRCUIT ---
        # [GOVERNANCE] If policy violation is detected, abort immediately.
        # NEVER let valid English barge-in logic run for a non-English violation.
        if intent != "PROCEED":
            logger.warning(f"POLICY VIOLATION: {intent} detected for input: {text}")
            
            # --- TASK 3.4: STRIKE TRACKING ---
            if intent == "HARD_REFUSAL_LANGUAGE":
                self.language_strike_count += 1
                logger.warning(f"Language Strike: {self.language_strike_count}/3 (Input: '{text}')")
                
                if self.language_strike_count == 1:
                    refusal_text = PRDScripts.REFUSAL_LANGUAGE_1
                elif self.language_strike_count == 2:
                    refusal_text = PRDScripts.REFUSAL_LANGUAGE_2
                else: # Strike 3+
                    refusal_text = PRDScripts.REFUSAL_LANGUAGE_3
            else:
                # Other refusals (Sensitive, Immigration, etc.) - keep existing behavior
                refusal_text = self.policy.get_refusal_script(intent)

            # C. Strike 3: Use dedicated termination flow (awaits TTS + closes connection)
            if intent == "HARD_REFUSAL_LANGUAGE" and self.language_strike_count >= 3:
                logger.warning("[GOVERNANCE] Strike 3 — initiating graceful termination flow.")
                self.response_task = asyncio.create_task(self._language_termination_flow(refusal_text, trace_id))
                return # SHORT-CIRCUIT: Do not proceed to barge-in or brain
            
            # D. Strikes 1-2 and other refusals: Speak refusal, stay on call
            # Cancel any thinking/speaking first so refusal can be heard
            if self.response_task and not self.response_task.done():
                self.response_task.cancel()
                
        # 3. INTERRUPTION & BARGE-IN (Only for PROCEED intents)
        if self.response_task and not self.response_task.done():
            is_speaking = self.state.get_state() == CallState.SPEAKING
            logger.debug(f">>> BARGE-IN DETECTED: state={self.state.get_state()}, speaking={is_speaking}")
            
            if self.call_logger:
                # Checkpoint: Save logs immediately to prevent data loss
                self.call_logger.save_log(status="in-progress")
                self.call_logger.log_event("orchestrator", "user_interruption", meta={"is_speaking": is_speaking})
            
            self.response_task.cancel()
            
            # B. Only speak interruption prompt IF the AI was actually talking
            if is_speaking:
                logger.debug("AI was speaking. triggering interruption prompt.")
                self.response_task = asyncio.create_task(
                    self.speak_refusal(PRDScripts.INTERRUPTION, trace_id=trace_id)
                )
                # CRITICAL: If we are speaking the interruption prompt, stop processing THIS turn
                # to allow the user to respond to the prompt.
                return
            else:
                logger.debug("AI was only thinking/transcribing. Silently cancelling old task.")
        # 3. Start Parallel Response Generation (Normal Flow)
        # S4-9: Update Context (Deterministic)
        if self.session:
            changes = self.context_manager.update_context(self.session.call_context, text, intent)
            
            # 5. Logging + Audit (Context Snapshot)
            import hashlib
            # Create a deterministic snapshot of just the persistent memory fields
            memory_state = {
                "program": self.session.call_context.program_interest,
                "intake": self.session.call_context.intake,
                "name": self.session.call_context.user_name,
                "mode": self.session.call_context.study_mode,
                "campus": self.session.call_context.campus
            }
            context_hash = hashlib.md5(json.dumps(memory_state, sort_keys=True).encode()).hexdigest()
            
            if self.call_logger:
                 self.call_logger.log_event("context", "audit_snapshot", meta={
                     "snapshot_hash": context_hash,
                     "updated_fields": list(changes.keys()),
                     "slot_extraction_result": changes,
                     "reason_for_update": "Deterministic phrase match" if changes else "No new slots detected"
                 }, trace_id=trace_id)
        
        # We start the task and let it handle its own errors (including LatencyBreachError)
        self.response_task = asyncio.create_task(self.generate_and_speak(text, intent=intent, trace_id=trace_id, turn_start_time=turn_start_time, stt_latency=stt_latency))

    async def speak_refusal(self, text, trace_id=None):
        """
        Helper to speak a static refusal message without using the Brain.
        """
        logger.debug(f"Starting Speak Refusal: '{text}'")
        # CRITICAL: Reset empty frame counter when agent speaks
        if self.consecutive_empty_frames > 0:
            logger.debug(f"[RESET] Counter {self.consecutive_empty_frames}→0 (agent speaking)")
            self.consecutive_empty_frames = 0
        # Only transition to SPEAKING if we are NOT already terminating.
        # Skipping this when in CALL_END prevents the state violation that would
        # trigger a SYSTEM_ERROR CRM ticket and block the goodbye message.
        if self.state.get_state() != CallState.CALL_END:
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
            
            # SYNC FIX: Wait for the agent to finish speaking before allowing more input
            if self.mode == "audio":
                # Estimate how long it will take to speak the text (Approx 15 chars/sec)
                duration = len(text) / 15.0
                logger.debug(f"Refusal streamed to client. Sleeping {duration:.1f}s to align with client playback.")
                await asyncio.sleep(duration)
            
            # LOGGING: Record the refusal in JSON logs
            if self.call_logger:
                self.call_logger.log_event("brain", "refusal_spoken", meta={"text": text, "chunks": chunks_sent}, trace_id=trace_id)
        except asyncio.TimeoutError:
            logger.error(f"Refusal TTS Timed Out for text: {text}")
        except Exception as e:
            logger.error(f"Error in speak_refusal: {e}")
        finally:
            # CRITICAL: Only go back to Listening if we are NOT already terminating
            # This prevents the CALL_END -> LISTENING state violation during silence termination
            if self.state.get_state() != CallState.CALL_END:
                self.state.transition_to(CallState.LISTENING, trace_id=trace_id)
            # Reset silence timer after speaking refusal (so we start counting silence from NOW)
            self.last_interaction_time = time.time()

    async def handle_audio_stream(self, websocket):
        """
        Main Loop: Coordinates the flow from Twilio (WebSocket) through STT, Brain, and TTS.
        """
        self.websocket = websocket
        
        # 0. INTAKE GUARDRAIL (Kill Switch)
        if not self.config.is_intake_enabled:
            logger.critical(f"Connection Rejected: INTAKE_DISABLED is active (env={self.config.env})")
            # Close with Policy Violation code (1008)
            await websocket.close(code=1008, reason="Intake Disabled")
            return

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

                # Start Silence Monitor (Story S4-2)
                self.silence_task = asyncio.create_task(self._monitor_silence())

                # LOG CALL TO CRM (New)
                try:
                    crm_id = await self.crm.log_call(
                        call_id=self.session.session_id,
                        caller_phone=self.session.caller_number,
                        caller_type="new_student", # Default for now, could be dynamic
                        summary="Incoming Call from Voice Agent",
                        transcript="[Call Started]", 
                        sentiment="Neutral"
                    )
                    if crm_id:
                        self.session.crm_call_id = str(crm_id)
                        logger.info(f"CRM Call Logged: {crm_id}")
                except Exception as e:
                    logger.error(f"Failed to log call to CRM: {e}")

                # Initial Greeting
                self.session_manager.update_state(self.session.session_id, SessionState.SPEAKING)
                greeting = PRDScripts.GREETING
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
                        # Break out if session has been terminated by silence monitor
                        if self.state.get_state() == CallState.CALL_END:
                            logger.debug("Media loop: session ended, stopping audio processing.")
                            break

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
                    goodbye_text = PRDScripts.APOLOGY_FATAL
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
            
            # LOG CALL TO CRM (Text Mode)
            try:
                crm_id = await self.crm.log_call(
                    call_id=self.session.session_id,
                    caller_phone="web_chat",
                    caller_type="prospect_chat",
                    summary="Text Chat Session",
                    transcript="[Chat Started]", 
                    sentiment="Neutral"
                )
                if crm_id:
                    self.session.crm_call_id = str(crm_id)
                    logger.info(f"CRM Call Logged (Text Mode): {crm_id}")
            except Exception as e:
                logger.error(f"Failed to log text chat to CRM: {e}")

            # Initial Greeting
            greeting = PRDScripts.GREETING_TEXT
            self.response_task = asyncio.create_task(self.generate_and_speak(greeting, is_greeting=True))

            # Enable Silence Monitor for Text Mode testing
            self.silence_task = asyncio.create_task(self._monitor_silence())

            from starlette.websockets import WebSocketDisconnect
            try:
                while True:
                    text = await websocket.receive_text()
                    await self._on_transcript(text, confidence=1.0, is_final=True)
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


    async def generate_and_speak(self, text, is_greeting=False, intent="unknown", trace_id=None, turn_start_time: float = None, stt_latency: float = 0.0):
        """
        Streams AI thoughts into a parallel TTS queue for zero-lag audio.
        Enforces 3s warning and 5s circuit breaker (Task 3 & 4).
        """
        if not self.session: return
        
        try:
            full_ai_text = ""
            audio_queue = asyncio.Queue()

            # Worker: Speaks chunks as they arrive from the brain
            async def tts_worker():
                total_chars = 0
                worker_start_time = time.time()
                try:
                    while True:
                        sentence = await audio_queue.get()
                        if sentence is None: break
                        total_chars += len(sentence)
                        
                        # STATE: Speaking
                        self.state.transition_to(CallState.SPEAKING, trace_id=trace_id)
                        
                        # CRITICAL: Reset non-English frame counter when AI starts speaking.
                        # Without this, frames accumulated during the "LISTENING while generating"
                        # window carry over and falsely extend the non_english_run duration,
                        # causing spurious language strikes after every normal AI reply.
                        if self.consecutive_empty_frames > 0:
                            logger.debug(f"[RESET] Non-English counter {self.consecutive_empty_frames}→0 (AI speaking)")
                            self.consecutive_empty_frames = 0
                            self.non_english_run_start = time.time()  # Anchor to now, never epoch
                        
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
                    
                    # Calculate estimated playback duration (approx 10 chars per sec for natural TTS)
                    estimated_play_time = total_chars / 10.0
                    time_spent_generating = time.time() - worker_start_time
                    remaining_time = estimated_play_time - time_spent_generating
                    
                    if remaining_time > 0:
                        logger.debug(f"Audio streamed to client. Sleeping {remaining_time:.1f}s to align with client playback.")
                        await asyncio.sleep(remaining_time)
                except asyncio.CancelledError:
                    logger.debug("TTS Worker Cancelled.")
                    
                # Back to Listening when done speaking (if not escalated)
                if self.state.get_state() not in [CallState.ESCALATION, CallState.CALL_END]:
                    self.state.transition_to(CallState.LISTENING, trace_id=trace_id)

            worker_task = asyncio.create_task(tts_worker())

            # 0. Check for Escalation (Policy)
            # STATE: Intent Eval
            self.state.transition_to(CallState.INTENT_EVAL, trace_id=trace_id)
            
            escalation = self.policy.check_escalation(text)
            
            # OVERRIDE: Force Escalation
            if self.config.override_escalation:
                logger.warning(f"[OVERRIDE] Force Escalation Triggered (env={self.config.env})")
                escalation = True
                
            if escalation:
                self.state.transition_to(CallState.ESCALATION, trace_id=trace_id)
                escalation_msg = PRDScripts.ESCALATION
                await audio_queue.put(escalation_msg)
                full_ai_text = escalation_msg
                
                # S4-5: End Call Gracefully on Escalation (No Live Transfer)
                # Let it finish speaking, the worker handles state, then end call
                asyncio.create_task(self._delayed_call_end())

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
                
                # Use persistent context
                active_context = self.session.call_context if self.session else None
                
                async for sentence, metadata in self.brain.generate_stream(text, self.session.conversation_history, caller_number=caller_num, intent=intent, trace_id=trace_id, call_context=active_context):
                    # --- LATENCY ENFORCEMENT (Task 3 & 4) ---
                    if turn_start_time:
                        current_turn_elapsed = asyncio.get_event_loop().time() - turn_start_time
                        
                        # 12s Circuit Breaker (Hard Failure - Increased from 5s to handle RAG/LLM spikes)
                        if current_turn_elapsed > 12.0:
                            raise LatencyBreachError(f"Turn processing timed out at {current_turn_elapsed:.2f}s")
                            
                        # 3s Warning (Soft Logging)
                        if not full_ai_text and current_turn_elapsed > 3.0:
                             logger.warning(f"LATENCY_WARNING: Turn reached {current_turn_elapsed:.2f}s! Breakdown: STT={stt_latency:.2f}s, Process={current_turn_elapsed - stt_latency:.2f}s")

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
                        failure_msg = PRDScripts.REFUSAL_LANGUAGE
                        await audio_queue.put(failure_msg)
                        full_ai_text = failure_msg
                        
                        self._create_task_with_log(self.crm.create_ticket(
                            transcript=f"Blocked Response (Non-English/Speculative): {sentence}\nUser Query: {text}",
                            summary="English-Only Policy/Speculation Violation",
                            sentiment="QUALITY_FAILURE",
                            call_logger=self.call_logger,
                            call_id=self.session.crm_call_id or self.session.session_id if self.session else trace_id,
                            title="Quality Assurance Failure"
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

                self._create_task_with_log(self.crm.create_ticket(
                    transcript=text,
                    summary=ticket_summary,
                    sentiment=ticket_sentiment,
                    call_logger=self.call_logger,
                    call_id=self.session.crm_call_id or self.session.session_id,
                    title=f"Support Request: {ticket_sentiment}"
                ))
            
            self.session_manager.update_state(self.session.session_id, SessionState.LISTENING)

        except LatencyBreachError as lbe:
            # Task 5: Handle breach internally
            sid = self.session.session_id if self.session else "unknown"
            logger.error(f"[{sid}] Latency circuit breaker triggered: {lbe}")
            await self._handle_latency_breach(sid)
        except asyncio.CancelledError:
            logger.info("AI thought-task cancelled by user interruption.")
            # Pillar 1: Identity snapshot 
            if self.session:
                self.session.interruption_snapshot = {"text": text, "timestamp": time.time()}
            if 'worker_task' in locals(): worker_task.cancel()
        except Exception as e:
            logger.error(f"Response Error: {e}")
        finally:
            # RESET SILENCE TIMER after AI finishes speaking
            # This ensures we don't count the time the AI was talking as user silence
            self.last_interaction_time = time.time()
            if self.session and self.state.get_state() != CallState.ESCALATION:
                self.silence_stage = 0

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

    async def _delayed_call_end(self, delay=5.0):
        """Helper to let TTS finish speaking before terminating"""
        await asyncio.sleep(delay)
        if self.state.get_state() != CallState.CALL_END:
             await self.cleanup()

    async def cleanup(self):
        """Final session archival and resource release (Pillar 3)."""
        # GUARD: Prevent double-cleanup (e.g. silence termination + WebSocket disconnect both call this)
        if self._cleanup_done:
            logger.debug("Cleanup already completed for this session. Skipping.")
            return
        self._cleanup_done = True

        sid = self.session.session_id if self.session else "unknown"
        logger.info(f"Cleanup started for session {sid}.")
        
        # STATE: Call End
        try:
            self.state.transition_to(CallState.CALL_END)
        except:
            pass # Swallow errors during cleanup
        
        # 🟢 CRITICAL: Wrap in try/except (Pillar 3)
        try:
            # 1. Cancel background response task
            # GUARD: Do NOT cancel if the task is _language_termination_flow (it calls us, we must not self-destruct)
            if self.response_task and not self.response_task.done():
                # Check if we are being called FROM the termination flow (stack guard)
                if not getattr(self, '_language_termination_active', False):
                    logger.debug("Cleanup: Cancelling background response task")
                    self.response_task.cancel()
                    try:
                        await self.response_task
                    except asyncio.CancelledError:
                        pass

            # Cancel Silence Monitor
            if self.silence_task and not self.silence_task.done():
                logger.debug("Cleanup: Cancelling silence monitor")
                self.silence_task.cancel()

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
                    summary=f"Call Log: {reason} (Session: {sid})",
                    sentiment="Positive", # Default to positive for successful logs
                    call_logger=self.call_logger,
                    call_id=self.session.crm_call_id or sid,
                    title=f"Completed Session Log ({reason})"
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
                # Use the session's actual termination reason, not a hardcoded value
                termination_reason = "user_hangup"
                if self.session and self.session.termination_reason:
                    termination_reason = self.session.termination_reason
                self.call_logger.generate_summary_line(status="completed", reason=termination_reason)
                self.call_logger.save_log(status="completed")
            
            # Force close websocket to break any receive loops
            # Wait briefly first to let any in-flight audio (e.g. goodbye TTS) finish transmitting
            if self.websocket:
                logger.debug("Cleanup: Waiting for in-flight audio before closing WebSocket...")
                await asyncio.sleep(2.0)
                logger.debug("Cleanup: Closing WebSocket connection")
                try:
                    await self.websocket.close()
                except Exception as e:
                    logger.debug(f"WebSocket close ignored (likely already closed): {e}")

        except Exception as e:
            # Fallback to sys.stderr (Pillar 3)
            import sys
            print(f"CRITICAL CLEANUP ERROR for {sid}: {e}", file=sys.stderr)
        finally:
            logger.info(f"Orchestrator session {sid} finalized.")

    async def _handle_latency_breach(self, sid: str):
        """
        Task 5: Fallback & CRM Ticketing for Latency Breach.
        """
        try:
            # 1. Trigger Fallback TTS FIRST (Ensures user hears it before closure)
            msg = PRDScripts.LATENCY_FALLBACK
            # Speak refusal handles the TTS stream
            await self.speak_refusal(msg)
            
            # 2. Transition State AFTER audio is sent
            self.state.transition_to(CallState.CALL_END)
            
            # 3. Create CRM Callback Ticket (High Priority)
            if self.session:
                self.session.termination_reason = "latency_breach"
                logger.warning(f"Creating Latency Breach CRM Ticket for {sid}")
                
                # Create ticket (The method itself handles internal backgrounding if needed, but we'll await)
                await self.crm.create_ticket(
                    transcript="[SYSTEM_EVENT] Call terminated due to sustained latency (>5.0s)",
                    summary="High Latency detected (>5.0s) causing system circuit break.",
                    sentiment="Negative",
                    call_id=self.session.crm_call_id or sid,
                    title="System_Latency_Breach"
                )
            
            await self.cleanup()
        except Exception as e:
            logger.error(f"Error handling latency breach: {e}")
            await self.cleanup()

    async def _monitor_silence(self):
        """
        Background task to monitor user silence and trigger re-engagement prompts.
        Implements Story S4-2 logic:
        10-20s silence -> prompt #1
        next 10-20s silence -> prompt #2
        continued silence -> termination
        """
        logger.debug("Starting Silence Monitor")
        try:
            while self.session and self.session.current_state != SessionState.ENDED:
                await asyncio.sleep(1.0)
                
                # Check session health - stop if session is gone
                if not self.session:
                    break

                # DEBUG: Trace silence monitor (Disabled for production)
                # state = self.state.get_state()
                # logger.debug(f"Silencer: Gap={time.time() - self.last_interaction_time:.1f}s State={state}")

                # 1. Don't count silence while AI is speaking, buffering, or transcribing
                if self.state.get_state() in [CallState.SPEAKING, CallState.INTENT_EVAL, CallState.ESCALATION, CallState.TRANSCRIBING]:
                    self.last_interaction_time = time.time()
                    continue

                # 2. Check Silence Duration
                gap = time.time() - self.last_interaction_time
                
                # Verify we haven't just reset (race condition check)
                if gap < 1.0:
                    continue

                # Stage 1: Initial Warning (10s+)
                if gap > 10.0 and self.silence_stage == 0:
                    logger.info(f"Silence Warning 1 triggered (Gap: {gap:.1f}s)")
                    self.silence_stage = 1
                    self.last_interaction_time = time.time() # CRITICAL: Reset timer immediately
                    
                    # Prompt #1 - await so it fully completes before the loop continues
                    msg = PRDScripts.SILENCE_1
                    await self.speak_refusal(msg)
                    
                # Stage 2: Secondary Warning (Another 10s passed since Prompt 1)
                elif gap > 10.0 and self.silence_stage == 1:
                    logger.info(f"Silence Warning 2 triggered (Gap: {gap:.1f}s)")
                    self.silence_stage = 2
                    self.last_interaction_time = time.time() # CRITICAL: Reset timer immediately
                    
                    # Prompt #2 - await so it fully completes before the loop continues
                    msg = PRDScripts.SILENCE_2
                    await self.speak_refusal(msg)
                    
                # Stage 3: Termination (Another 10s passed since Prompt 2)
                elif gap > 10.0 and self.silence_stage == 2:
                    logger.warning(f"Silence Termination triggered (Gap: {gap:.1f}s)")
                    self.silence_stage = 3 # Prevent loops

                    # Set the real termination reason BEFORE cleanup so the summary log is correct
                    if self.session:
                        self.session.termination_reason = "silence_termination"

                    # STEP 1: Speak goodbye FIRST, while WebSocket is still fully open.
                    # We must NOT set CALL_END yet — that would break the media loop and
                    # close the WebSocket before the audio chunks are delivered.
                    goodbye = PRDScripts.SILENCE_TERMINATION
                    await self.speak_refusal(goodbye)

                    # STEP 2: NOW transition to CALL_END.
                    # speak_refusal's finally block tried to go to LISTENING, but we
                    # set CALL_END here immediately after so the media loop will break
                    # on the next iteration.
                    self.state.transition_to(CallState.CALL_END)

                    # STEP 3: Create CRM ticket (awaited so it's in the log before save)
                    try:
                        await self.crm.create_ticket(
                            transcript="[System]: Call terminated due to extended user silence (30s+).",
                            summary="Silence Termination",
                            sentiment="Neutral",
                            call_logger=self.call_logger,
                            call_id=self.session.session_id
                        )
                    except Exception as e:
                        logger.error(f"Failed to create Silence Termination CRM ticket: {e}")

                    # STEP 4: Cleanup and exit monitor loop
                    await self.cleanup()
                    break
                    
        except asyncio.CancelledError:
            logger.debug("Silence Monitor cancelled")
        except Exception as e:
            logger.error(f"Error in Silence Monitor: {e}", exc_info=True)

    async def _language_termination_flow(self, refusal_text, trace_id):
        """
        [GOVERNANCE] Architect-Grade Strike 3 Termination.
        1. Async CRM Ticket (SECURITY_ALERT)
        2. Final Goodbye TTS
        3. Sever Connection (Cleanup)
        """
        self._language_termination_active = True
        logger.warning(f"[GOVERNANCE] Initiating Final Termination for trace {trace_id}")
        try:
            # 1. CRM Ticket (Architect Rule: High Priority SECURITY_ALERT Triggered FIRST)
            self._create_task_with_log(self.crm.create_ticket(
                transcript="[GOVERNANCE] Call terminated due to language policy violation (3 strikes).",
                summary="Language Barrier Termination (3 Strikes)",
                sentiment="SECURITY_ALERT",
                call_logger=self.call_logger,
                call_id=self.session.crm_call_id or self.session.session_id if self.session else trace_id,
                title="Policy Termination: Language"
            ))

            # 2. Speak Final Goodbye (Awaited to ensure audio transmits fully before closure)
            await self.speak_refusal(refusal_text, trace_id=trace_id)
            
            # 3. Transition State & Cleanup AFTER audio is sent
            self.state.transition_to(CallState.CALL_END, trace_id=trace_id)
            
            # 4. Final Cleanup (will NOT self-cancel this task due to _language_termination_active guard)
            await self.cleanup()
        except Exception as e:
            logger.error(f"[GOVERNANCE] Error in language termination flow: {e}")
            await self.cleanup()
        finally:
            self._language_termination_active = False
