# Voice Orchestrator - Central Logic Manager
import asyncio
import base64
import json
import os
import logging as std_logging
import re
import string
import time
import uuid
from datetime import datetime
from contracts.language_interceptor import LanguageGovernanceInterceptor
from orchestrator.brain import Brain
from orchestrator.interfaces import STTProvider, TTSProvider
from tts.synthesizer import TTSException
from crm.client import CRMClient
from contracts.policy import ResponsePolicyEngine, PRDScripts
from contracts.schemas import CallContext
from contracts.state import StateMachine, CallState
from audit_logging.recorder import CallRecorder
from agent_logging import CallLogger
from .session_manager import SessionManager, SessionState
from orchestrator.context_extractor import ContextManager
from contracts.config import FeatureConfig
from models.schemas import StandardTurn, BargeInTurn

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

        # Language Governance Interceptor — one instance per call, created here so
        # it can be reset via self._lang_interceptor.reset() when a new call begins.
        self._lang_interceptor = LanguageGovernanceInterceptor(session_id=self.sid)

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

        # Session Duration / Wrap-up (Pilot-Ready)
        self.wrapup_triggered = False
        self.session_start_wall_time = None

        # Cleanup guard: prevents double-cleanup from concurrent disconnect + silence termination
        self._cleanup_done = False
        self.last_response_was_question = False
        self._last_ai_question_text = ""    # [STAB-04] Stores last AI response when it ended with "?"
        self._pending_callback_offer = False  # [STAB-05] True when awaiting Yes/No after LOW_CONFIDENCE_FALLBACK
        self._pending_callback_query = ""     # [STAB-05] Original caller query that triggered the KB miss
        self._stt_recovery_lock = False  # Critical: Prevent Death Spiral during hot-swaps
        self._vad_safety_task = None     # Tracker for Echo Trap recovery
        self._last_stt_packet_time = time.time() # [CALL-CPR] Watchdog for silent dropouts
        self._post_tts_ingress_logged = False    # [STAB-02] Arms Log Point 4 after each TTS completion
        self._watchdog_check_time = time.time()  # Throttler for watchdog
        self.stop_event = asyncio.Event()  # [FIX-1] Required by _monitor_silence loop guard; was missing, causing immediate crash
        self._current_speaking_text = ""  # [ECHO-SUPPRESSION] Tracks active TTS sentence for echo detection
        self._last_partial = ""           # [MUTATION] Last partial transcript seen
        self._partial_mutations = 0       # [MUTATION] Count of content changes within one utterance
        self._incomplete_utterance = ""   # [ENDPOINTING] Buffered dangling fragment to combine with next final
        self._buffer_flush_task = None    # [ENDPOINTING] Timer to flush stale buffer after 3 s of silence


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

    def _is_multi_step(self, text: str) -> bool:
        """Heuristic to detect if a response contains a structured sequence or list."""
        if not text: return False
        
        # 1. Numbered lists (1., 2., 3. or 1), 2), 3))
        if re.search(r'(\d[\.\)]\s)', text): return True
        
        # 2. Sequential markers
        sequences = ["first", "second", "third", "finally", "next step", "step one", "then"]
        text_lower = text.lower()
        # Count matches
        matches = sum(1 for word in sequences if f" {word}" in f" {text_lower}")
        if matches >= 2: return True
        
        # 3. Bullet points
        if re.search(r'([\n\s][\-\*\•]\s)', text): return True
        
        return False

    async def _on_stt_listener_error(self, err: Exception):
        """Called when the Deepgram _listen loop crashes. Performs Hot-Swap STT."""
        logger.error(f"[RECOVERY] STT Listener Dropout: {err}. Triggering Hot-Swap...")
        
        # 🟢 CRITICAL: Unstick the state machine. If we were waiting for a result, 
        # we aren't getting one from a dead connection. Force back to LISTENING.
        current_state = self.state.get_state()
        if current_state in [CallState.TRANSCRIBING, CallState.INTENT_EVAL, CallState.RETRIEVAL, CallState.INTERRUPTED]:
            logger.warning(f"[RECOVERY] Unsticking state {current_state} -> LISTENING")
            self.state.transition_to(CallState.LISTENING)

        if self._stt_recovery_lock:
            logger.debug("[RECOVERY] Recovery already in progress. Skipping redundant request.")
            return

        try:
            self._stt_recovery_lock = True
            from stt.stt_pool import stt_pool, PooledTranscriber
            raw_stt = await stt_pool.acquire(timeout=2.0)
            if raw_stt:
                old_stt = self.transcriber
                self.transcriber = PooledTranscriber(stt_pool, raw_stt)
                self.transcriber.set_callback(self._on_transcript)
                self.transcriber.set_listener_error_callback(self._on_stt_listener_error)
                
                # Cleanup old connection in background
                asyncio.create_task(old_stt.close())
                logger.info("[FAILOVER] STT Provider hot-swapped successfully.")
            else:
                logger.error("[RECOVERY] Pool exhausted during STT hot-swap.")
        except Exception as e:
            logger.critical(f"[FAILOVER] STT Hot-Swap FAILED: {e}")
        finally:
            self._stt_recovery_lock = False

    async def _on_transcript(self, text: str, confidence: float, stt_latency: float = 0.0, is_final: bool = False, detected_lang: str = None):
        """
        [GOVERNANCE] Expert Debugger Entry Point.
        """
        self._last_stt_packet_time = time.time()  # [CALL-CPR] Reset watchdog
        raw_text = text.strip() if text else ""
        
        # --- STRICT RESTRICTED TOPIC GATE (Pre-Retrieval) ---
        if is_final and raw_text:
            if not hasattr(self, "session_state") or self.session_state is None:
                self.session_state = {}

            if self.session_state.get("restricted_handled") or self.session_state.get("terminate_session"):
                return

            from contracts.policy import detect_restricted_topic, handle_restricted_topic
            rest_result = detect_restricted_topic(raw_text)
            
            # --- STAB-07: Isolated Competitor Refusal Path (Global Gate) ---
            # This ensures no competitor query reaches the Brain or is logged in the CRM
            # before any fallback or escalation paths can interfere.
            intent_early = self.policy.classify_intent(raw_text)
            if intent_early == "HARD_REFUSAL_COMPETITOR_QUERY":
                # [STAB-07] Generate fresh trace_id at the gate if none exists
                trace_id = getattr(self, "_current_trace_id", str(uuid.uuid4()))
                self._current_trace_id = trace_id
                await self.handle_competitor_refusal(trace_id=trace_id)
                return

            if rest_result.is_restricted:
                self.session_state["restricted_handled"] = True
                self.session_state["terminate_session"] = True

                if not hasattr(self, "flags") or self.flags is None:
                    self.flags = {}
                self.flags["block_retrieval"] = True

                # Synchronous dispatch safety: ensure CRM dispatch before anything else
                should_block = await handle_restricted_topic(self, rest_result.category, confidence=rest_result.confidence)
                if should_block:
                    return

        # Double guarantee: if terminate_session is set, block ALL future text
        if getattr(self, "session_state", {}).get("terminate_session"):
            return

        # [STAB-05] Callback Offer Intercept — must happen before intent classification.
        # When the agent just asked "Would you like me to arrange a callback?" we intercept
        # the caller's Yes/No response and route directly, bypassing the normal LLM pipeline.
        if is_final and raw_text and getattr(self, "_pending_callback_offer", False):
            _yes_tokens = {"yes", "yeah", "yep", "sure", "please", "ok", "okay", "go ahead", "do it", "correct", "affirmative"}
            _no_tokens = {"no", "nope", "nah", "don't", "no thanks", "not now", "skip", "never mind", "nevermind", "that's fine"}
            _reply_lower = raw_text.lower().strip().rstrip(".,!?")
            _is_yes = any(_reply_lower == t or _reply_lower.startswith(t) for t in _yes_tokens)
            _is_no = any(_reply_lower == t or _reply_lower.startswith(t) for t in _no_tokens)

            if _is_yes or _is_no:
                self._pending_callback_offer = False
                _caller_query = getattr(self, "_pending_callback_query", "")
                self._pending_callback_query = ""

                # trace_id is not yet defined this early in _on_transcript (generated later via uuid)
                _intercept_trace_id = getattr(self, "_current_trace_id", None)

                if _is_yes:
                    logger.info(f"[STAB-05] Caller accepted callback for query='{_caller_query[:60]}'")
                    if self.call_logger:
                        self.call_logger.log_event("stab05", "callback_accepted", meta={"caller_query": _caller_query})
                    if self.session and self.crm:
                        self._create_task_with_log(self.crm.create_ticket(
                            transcript=f"[STAB-05] Caller requested callback.\nOriginal query: {_caller_query}",
                            summary="Callback Requested: KB Miss",
                            sentiment="Neutral",
                            call_logger=self.call_logger,
                            call_id=self.session.crm_call_id or self.session.session_id,
                            title="Callback_Required_KB_Miss",
                            structured_turns=self.session.structured_turns,
                            session_obj=self.session,
                            callback_required=True,
                            metadata={"trigger_query": _caller_query},
                        ))
                    self.response_task = asyncio.create_task(
                        self.speak_immediate_response(
                            "Of course! I've arranged a callback. An admissions officer will be in touch shortly. Goodbye.",
                            trace_id=_intercept_trace_id
                        )
                    )
                    self._create_task_with_log(self._delayed_call_end(delay=4.0))
                else:
                    logger.info("[STAB-05] Caller declined callback — returning to LISTENING.")
                    if self.call_logger:
                        self.call_logger.log_event("stab05", "callback_declined", meta={"caller_query": _caller_query})
                    self.response_task = asyncio.create_task(
                        self.speak_immediate_response(PRDScripts.ANYTHING_ELSE, trace_id=_intercept_trace_id)
                    )
                return  # Short-circuit: do not pass to intent classification or LLM

        # 1. AGGRESSIVE LOGGING: We must see what the STT actually heard
        # [STT RAW] is used by the Expert Debugger to diagnose "deafness" or "hallucinations"
        # Progress counter added for better forensic visibility
        logger.info(f"[STT RAW] Text: '{raw_text}' | is_final: {is_final} (Counter: {self.consecutive_empty_frames})")

        if not is_final:
            # [BARGE-IN] Partial-transcript path: stop AI audio immediately (<300ms target).
            # Twilio standard Media Streams never send a 'speech' event, so we use Deepgram
            # interim results as the VAD signal instead.
            if raw_text and self.state.get_state() == CallState.SPEAKING:
                if self.response_task and not self.response_task.done():
                    if not self._is_echo_transcript(raw_text):
                        halt_start = asyncio.get_event_loop().time()
                        self.synthesizer.stop_current_speech(self.sid)
                        self._create_task_with_log(self._send_clear_message())
                        self.state.transition_to(CallState.INTERRUPTED)
                        halt_ms = int((asyncio.get_event_loop().time() - halt_start) * 1000)
                        logger.info(f">>> BARGE-IN (Partial, ~{halt_ms}ms halt): '{raw_text}'")
                        # [STAB-03] Latency instrumentation — AC requires stop_current_speech delta < 300ms
                        _pass = halt_ms < 300
                        if self.call_logger:
                            self.call_logger.log_event(
                                "stab03", "latency_check",
                                latency_ms=halt_ms,
                                meta={
                                    "check": "stop_current_speech_delta",
                                    "pass": _pass,
                                    "threshold_ms": 300,
                                    "partial_text_preview": raw_text[:40],
                                },
                                trace_id=getattr(self, '_current_trace_id', None)
                            )
                        if not _pass:
                            logger.warning(f"[STAB-03][latency_check] FAIL halt={halt_ms}ms > 300ms threshold")

            # Reset silence timer on partials — but ONLY when the agent is not speaking.
            # During SPEAKING, Twilio echoes TTS audio back as partial transcripts (non-final),
            # which would keep resetting the timer and prevent silence termination from firing.
            if raw_text and self.state.get_state() != CallState.SPEAKING:
                self.last_interaction_time = time.time()

            # STREAM BUFFERING: Pre-fetch RAG context for partial transcripts
            if len(raw_text) > 15 and self.session:
                if getattr(self, "flags", {}).get("block_retrieval"):
                    return
                    
                # 🛡️ SECURITY GATE: Prevent policy-violating partial transcripts from hitting external Vector DB
                intent = self.policy.classify_intent(raw_text, detected_lang=detected_lang)
                if intent == "PROCEED":
                    prefetched_task = getattr(self.session, 'prefetched_context_task', None)
                    if prefetched_task is None or prefetched_task.done():
                        logger.debug(f"[STREAM BUFFER] Starting proactive KB lookup for: '{raw_text}'")
                        # Fire and forget KB search
                        self.session.prefetched_context_task = asyncio.create_task(
                            self.brain.kb.search(raw_text, self.call_logger, 3)
                        )
                else:
                    logger.debug(f"[STREAM BUFFER] Blocked proactive KB lookup due to policy: {intent} ('{raw_text}')")

            # [MUTATION TRACKER] Track content changes in partials — Hindi hallucinations
            # mutate mid-stream (words change), real English only grows (words added).
            # Compare WORD LISTS not raw strings — punctuation flicker (. ↔ ?) must not
            # count as a mutation. Only actual word-level changes count.
            if raw_text:
                last_words = re.findall(r'\b\w+\b', self._last_partial.lower())
                cur_words = re.findall(r'\b\w+\b', raw_text.lower())
                if last_words and cur_words != last_words:
                    # Words changed — check if it's just growth or actual content change
                    is_pure_growth = (cur_words[:len(last_words)] == last_words or
                                      last_words[:len(cur_words)] == cur_words)
                    if not is_pure_growth:
                        # Count how many word positions actually differ
                        diff_count = sum(1 for a, b in zip(last_words, cur_words) if a != b)
                        diff_count += abs(len(last_words) - len(cur_words))
                        # Single-word oscillations in long sentences (≥4 words) are normal
                        # Deepgram disambiguation between acoustically similar English words
                        # (e.g., "courses" ↔ "posters"). Don't count as Hindi hallucination.
                        if diff_count <= 1 and len(last_words) >= 4:
                            logger.debug(f"[MUTATION] Single-word swap ignored: '{self._last_partial[:40]}' → '{raw_text[:40]}'")
                        else:
                            self._partial_mutations += 1
                            logger.debug(f"[MUTATION] Partial changed ({self._partial_mutations}x, {diff_count}w diff): '{self._last_partial[:40]}' → '{raw_text[:40]}'")
                self._last_partial = raw_text
            else:
                self._last_partial = ""
                self._partial_mutations = 0

            return # Only process complete sentences for LLM.
            
        # 2. Run the failsafe policy (EXACT logic per Debugger Plan)
        # If there is text, run the char-ratio/langdetect guard.
        # Consume & reset mutation counter on every final transcript
        _mutations = self._partial_mutations
        self._partial_mutations = 0
        self._last_partial = ""

        if raw_text:
            # [GOVERNANCE] Language gate — delegate all detection to LanguageGovernanceInterceptor.
            # Detection priority: empty fast-path → common-word fast-path → name-intro regex →
            # Deepgram acoustic (PRIMARY) → FastText lid.176.ftz (EC2) → Lingua (fallback).
            # This replaces the legacy mutation-guard, conf-guard, and inline _is_english()
            # heuristics with a single, testable, well-documented detector.
            lang_result = self._lang_interceptor.check(raw_text, deepgram_lang=detected_lang)

            if not lang_result.proceed_to_llm:
                logger.warning(
                    f"[ORCHESTRATOR] Language violation caught via {lang_result.detection_method}: '{raw_text}'"
                )

                # Forensic log — always visible in call timeline
                if self.call_logger:
                    self.call_logger.log_event(
                        "stt",
                        "user_transcript_final",
                        latency_ms=int(stt_latency * 1000),
                        meta={"text": raw_text, "confidence": confidence, "note": "BLOCKED_LANGUAGE_EARLY"},
                    )

                # Increment master strike counter and persist to session
                self.language_strike_count += 1
                if self.session:
                    self.session.language_warning_count = self.language_strike_count
                    try:
                        self.session_manager.save_session(self.session)
                    except Exception as e:
                        logger.debug(f"Failed to persist language_warning_count: {e}")

                # CRM ticket on every non-English strike
                call_id = (self.session.crm_call_id or self.session.session_id) if self.session else "language_violation"
                self._create_task_with_log(
                    self.crm.create_ticket(
                        transcript=f"[LANG_GOV] Non-English input blocked (interceptor/{lang_result.detection_method}): '{raw_text}'",
                        summary=f"Language Governance Violation (Strike {self.language_strike_count}/3 - Interceptor)",
                        sentiment="SECURITY_ALERT",
                        call_logger=self.call_logger,
                        call_id=call_id,
                        title=f"Language Policy Strike {self.language_strike_count}",
                        session_obj=self.session
                    )
                )

                # [STATE FIX] Reset silence state so the silence timer does not fire
                # during or immediately after the refusal audio is played.
                self.silence_stage = 0
                self.last_interaction_time = time.time()

                trace_id = str(uuid.uuid4())
                if self.language_strike_count == 1:
                    refusal_text = PRDScripts.REFUSAL_LANGUAGE_1
                elif self.language_strike_count == 2:
                    refusal_text = PRDScripts.REFUSAL_LANGUAGE_2
                else:
                    refusal_text = PRDScripts.REFUSAL_LANGUAGE_3

                if self.response_task and not self.response_task.done():
                    self.response_task.cancel()
                    logger.debug("[GOVERNANCE] Cancelled pending LLM task for Hard Bypass (Text Strike).")

                self.state.transition_to(CallState.ESCALATION, trace_id=trace_id)

                if self.language_strike_count >= 3:
                    logger.warning("[GOVERNANCE] Strike 3 — initiating graceful termination flow.")
                    self.response_task = asyncio.create_task(self._language_termination_flow(refusal_text, trace_id))
                else:
                    self.response_task = asyncio.create_task(self.speak_immediate_response(refusal_text, trace_id=trace_id))

                return  # CRITICAL: Return so it never hits the LLM
        
        # ... proceed with English processing ...
        # Task 3 & 4: Start Turn Timer (Include STT Latency)
        # turn_start_time now represents the moment the user stopped speaking.
        now = asyncio.get_event_loop().time()
        turn_start_time = now - stt_latency
        trace_id = str(uuid.uuid4())

        # STATE-AWARE NON-ENGLISH DETECTION: Handle empty transcripts from Deepgram
        if not text.strip():
            if confidence == 0.0:
                # ── STATE GUARD: Only count empty frames when actively LISTENING ──────────────
                # When the AI is SPEAKING, Deepgram naturally receives silence from the
                current_call_state = self.state.get_state()
                # [GOVERNANCE FIX]: ONLY allow empty frame detection during LISTENING.
                # If we allow it during SPEAKING, the AI's own voice being sent back as empty
                # frames to Deepgram over a 12-second long sentence triggers a false strike!
                if current_call_state not in [CallState.LISTENING, CallState.TRANSCRIBING]:
                    logger.debug(f"[DEBOUNCE] Ignoring empty frame in state {current_call_state}")
                    return

                # ── DEBOUNCE FIX: Filter millisecond-interval STT burst artifacts ────────────
                # Deepgram sometimes sends multiple empty frames in <50ms, which 
                # incorrectly triggers strikes. We require 200ms between increments.
                stt_current_time = time.time()
                if stt_current_time - self.last_empty_frame_time < 0.2:
                    logger.debug(f"[DEBOUNCE] Filtering STT burst artifact for {self.sid}")
                    return
                
                self.last_empty_frame_time = stt_current_time

                # Track the start of this non-English "run" (resets when high-confidence English text comes in)
                if self.consecutive_empty_frames == 0:
                    self.non_english_run_start = time.time()  # Fresh start for each new run
                self.consecutive_empty_frames += 1
                non_english_duration = time.time() - self.non_english_run_start

                logger.debug(f"[PHONEME] Empty frame #{self.consecutive_empty_frames}, run={non_english_duration:.1f}s, detected_lang={detected_lang}")

                # ── DEEPGRAM FAST-PATH: Immediate refusal when acoustic detection ─────
                # With detect_language=true, Deepgram may report the actual language
                # even on empty transcripts. If it says non-English, fire immediately
                # instead of waiting 16 seconds. Requires user_has_spoken and at least
                # 2 frames (rules out single noise spikes).
                from contracts.policy import ResponsePolicyEngine
                _phoneme_dg_lang = ResponsePolicyEngine._normalise_lang_code(detected_lang) if detected_lang else None
                _phoneme_dg_is_nonsupported = (
                    _phoneme_dg_lang
                    and _phoneme_dg_lang not in self.config.supported_languages
                    and self.user_has_spoken
                    and self.consecutive_empty_frames >= 2
                )

                # ── LEGACY ACCUMULATION GATE (fallback when detected_lang is None) ────
                # Fire a language strike when:
                #   1. At least 5 frames in this run (rules out noise spikes)
                #   2. Run has been active >= 16 seconds
                #   3. User has spoken at least once
                # The 16s duration guard stays above the silence monitor's 10s warning
                # so pure silence triggers 'Are you still there?' first.
                _legacy_accumulation_triggered = (
                    self.consecutive_empty_frames >= 5
                    and non_english_duration >= 16.0
                    and self.user_has_spoken
                )

                if _phoneme_dg_is_nonsupported or _legacy_accumulation_triggered:
                        _trigger_method = f"deepgram_acoustic({_phoneme_dg_lang})" if _phoneme_dg_is_nonsupported else "phoneme_empty_accumulation"
                        logger.warning(f"[GOVERNANCE] Phoneme strike triggered via {_trigger_method}")
                        # ── FORENSIC FIX: Route through PERMANENT STRIKE SYSTEM ────────────────
                        # Hindi/Bengali/Mandarin arrive as transcript='', confidence=0.0,
                        # speech_final=true because 8kHz mulaw cannot produce non-Latin phonemes.
                        # We MUST use the same strike counter as the text-based path so that 
                        # violations accumulate across BOTH detection methods.
                        # ───────────────────────────────────────────────────────────────────────
                        current_time = time.time()
                        if current_time - self.last_refusal_time < 4:
                            # Still ensure state is reset before silent return
                            if self.state.get_state() in [CallState.TRANSCRIBING, CallState.RESPONSE_VALIDATION]:
                                self.state.transition_to(CallState.LISTENING)
                            return

                        self.language_strike_count += 1
                        logger.warning(f"[GOVERNANCE] Unrecognized-language speech detected ({_trigger_method}). Strike: {self.language_strike_count}/3")

                        # Persist warning_count on the session (phoneme path)
                        if self.session:
                            current = getattr(self.session, "language_warning_count", 0)
                            self.session.language_warning_count = current + 1
                            try:
                                self.session_manager.save_session(self.session)
                            except Exception as e:
                                logger.debug(f"Failed to persist language_warning_count: {e}")
                        
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
                                "refusal_flags": {"strike_count": self.language_strike_count, "method": _trigger_method}
                            }, trace_id=trace_id)
                            self.session.conversation_history.append({"role": "user", "parts": [f"[UNRECOGNIZED LANGUAGE SPEECH - Strike {self.language_strike_count}]"]})
                        
                        # CRM Ticket — fire on every strike
                        self._create_task_with_log(self.crm.create_ticket(
                            transcript=f"[SYSTEM] Unrecognized phonemes (Hindi/Bengali/etc.). Strike {self.language_strike_count}/3.",
                            summary=f"Security Violation: HARD_REFUSAL_LANGUAGE (phoneme, Strike {self.language_strike_count})",
                            sentiment="SECURITY_ALERT",
                            call_logger=self.call_logger,
                            call_id=self.session.crm_call_id or self.session.session_id if self.session else (trace_id or "system_gen"),
                            title=f"Policy: Language Barrier Strike {self.language_strike_count}",
                            session_obj=self.session
                        ))
                        
                        self.last_refusal_time = current_time
                        self.consecutive_empty_frames = 0
                        self.non_english_run_start = time.time()  # Reset to NOW, not 0.0

                        # [STATE FIX] Reset silence state so the timer does not interrupt
                        # the refusal audio that is about to play.
                        self.silence_stage = 0
                        self.last_interaction_time = time.time()

                        # Pick refusal script by strike number
                        if self.language_strike_count == 1:
                            refusal_text = PRDScripts.REFUSAL_LANGUAGE_1
                        elif self.language_strike_count == 2:
                            refusal_text = PRDScripts.REFUSAL_LANGUAGE_2
                        else:
                            refusal_text = PRDScripts.REFUSAL_LANGUAGE_3

                        # 🟢 S4 Hardening: Hard LLM Bypass (Phoneme Path)
                        if self.response_task and not self.response_task.done():
                            self.response_task.cancel()
                            logger.debug("[GOVERNANCE] Cancelled pending LLM task for Hard Bypass (Phoneme Strike).")
                        
                        self.state.transition_to(CallState.ESCALATION, trace_id=trace_id)

                        # Strike 3: Graceful termination
                        if self.language_strike_count >= 3:
                            logger.warning("[GOVERNANCE] Strike 3 (phoneme path) — initiating graceful termination flow.")
                            self.response_task = asyncio.create_task(self._language_termination_flow(refusal_text, trace_id))
                        else:
                            self.response_task = asyncio.create_task(self.speak_immediate_response(refusal_text, trace_id=trace_id))
                    
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
                # Empty with higher confidence = Background silence
                # [GOVERNANCE] Do NOT reset the run counter on background silence if 
                # a non-English run is active. We only reset on REAL English text.
                logger.debug(f"[FILTER] Empty transcript (confidence: {confidence:.2f}) - background silence")
                return
        else:
            # Non-empty transcript received.
            # Only HIGH-confidence text (≥0.75) is trusted as genuine English and resets
            # the phoneme run counter. Low-confidence text is likely Deepgram force-fitting
            # non-English phonemes into English words — we preserve the run so it can
            # accumulate toward the 5-frame / 16s threshold.
            if confidence >= 0.75:
                if self.consecutive_empty_frames > 0:
                    logger.debug(f"[RESET] Resetting phoneme counter (was {self.consecutive_empty_frames}) - high-confidence English")
                    self.consecutive_empty_frames = 0
                self.non_english_run_start = time.time()
            else:
                logger.debug(f"[GOVERNANCE] Low-confidence text (conf={confidence:.2f}) - phoneme counter preserved at {self.consecutive_empty_frames}")

            # SILENCE RESET: any user speech (even low-confidence) resets the silence timer
            self.last_interaction_time = time.time()
            self.silence_stage = 0
        
        # LOW-QUALITY DETECTION: Catch mumbled/garbled non-English input
        # Only trigger on NON-EMPTY low-confidence text (avoids feedback loop)
        # [TESTING] Threshold reduced from 0.50 to 0.35 to prevent false-blocking of valid quiet speech
        # Deepgram Nova-2 often returns ~0.5-0.75 for accurate but quiet/fast speech over phone
        if confidence < 0.35:
            logger.warning(f"[LOW-CONFIDENCE] Text: '{text}' (Conf: {confidence:.2f}) - triggering clarification")
            
            # 🟢 S4 Hardening: Hard LLM Bypass
            if self.response_task and not self.response_task.done():
                self.response_task.cancel()
                logger.debug("[GOVERNANCE] Cancelled pending LLM task for Hard Bypass (Low CONF).")
            
            self.state.transition_to(CallState.ESCALATION, trace_id=trace_id)
            self.response_task = asyncio.create_task(
                self.speak_immediate_response(PRDScripts.APOLOGY_CLARIFICATION, trace_id=trace_id)
            )
            return
        
        # [ENDPOINTING BUFFER] Deepgram sometimes finalizes a sentence prematurely when the
        # user pauses mid-thought (e.g. "Tell me about" before completing "...the admission process").
        # If the final ends with a dangling function word, buffer it and wait for the next final
        # to combine into one complete utterance before sending to the LLM.
        _DANGLING_WORDS = {
            "about", "the", "a", "an", "of", "for", "to", "in", "on", "at", "with",
            "from", "by", "and", "or", "but", "that", "this", "these", "those",
            "what", "which", "how", "when", "where", "who", "why", "if",
        }
        _last_word = raw_text.rstrip(".,!?").rsplit(None, 1)[-1].lower() if raw_text else ""
        if raw_text and _last_word in _DANGLING_WORDS and len(raw_text.split()) <= 8:
            # Fragment too short and ends on a function word — likely incomplete
            self._incomplete_utterance = (self._incomplete_utterance + " " + raw_text).strip()
            logger.debug(f"[ENDPOINTING] Buffered incomplete utterance: '{self._incomplete_utterance}'")
            # Schedule a 3-second flush so the buffer never starves if the caller
            # goes quiet without sending a continuation final.
            if self._buffer_flush_task and not self._buffer_flush_task.done():
                self._buffer_flush_task.cancel()
            self._buffer_flush_task = self._create_task_with_log(
                self._flush_incomplete_utterance(3.0, detected_lang=detected_lang)
            )
            return  # Wait for continuation before processing
        elif self._incomplete_utterance:
            # Combine buffered fragment with this continuation; cancel pending flush
            if self._buffer_flush_task and not self._buffer_flush_task.done():
                self._buffer_flush_task.cancel()
                self._buffer_flush_task = None
            text = (self._incomplete_utterance + " " + text).strip()
            raw_text = text
            logger.debug(f"[ENDPOINTING] Combined with buffer: '{raw_text}'")
            self._incomplete_utterance = ""

        # STATE: Transcribing / Input Received
        # [BARGE-IN FIX] Capture state BEFORE transitioning so the barge-in check below
        # can detect if the AI was SPEAKING when this transcript arrived.
        pre_transition_state = self.state.get_state()
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
                
            # log_conversation_turn is deprecated (PRD P3-07)
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
        # [GOVERNANCE] Language strikes are cumulative for the session (Pillar 1).
        # We do NOT reset them here, even if the user speaks English, 
        # to prevent "strike-cycling" where a user intersperses 
        # foreign speech with English to bypass termination.
        
        # --- RAG SHORT-CIRCUIT ---
        # [GOVERNANCE] If policy violation is detected, abort immediately.
        # NEVER let valid English barge-in logic run for a non-English violation.
        if intent != "PROCEED":
            logger.warning(f"POLICY VIOLATION: {intent} detected for input: {text}")
            
            # --- TASK 3.4: STRIKE TRACKING ---
            if intent == "HARD_REFUSAL_LANGUAGE":
                self.language_strike_count += 1
                logger.warning(f"Language Strike: {self.language_strike_count}/3 (Input: '{text}')")

                # Persist warning_count on the session for this caller
                if self.session:
                    current = getattr(self.session, "language_warning_count", 0)
                    self.session.language_warning_count = current + 1
                    try:
                        self.session_manager.save_session(self.session)
                    except Exception as e:
                        logger.debug(f"Failed to persist language_warning_count: {e}")

                # Create CRM ticket on every non-English violation
                call_id = (self.session.crm_call_id or self.session.session_id) if self.session else trace_id
                self._create_task_with_log(
                    self.crm.create_ticket(
                        transcript=f"[LANG_GOV] Non-English input blocked by PolicyEngine: '{text}'",
                        summary=f"Language Governance Violation (Strike {self.language_strike_count}/3 - PolicyEngine)",
                        sentiment="SECURITY_ALERT",
                        call_logger=self.call_logger,
                        call_id=call_id,
                        title=f"Language Policy Strike {self.language_strike_count}",
                        session_obj=self.session
                    )
                )
                
                # [STATE FIX] Reset silence state so the timer does not interrupt the refusal.
                self.silence_stage = 0
                self.last_interaction_time = time.time()

                if self.language_strike_count == 1:
                    refusal_text = PRDScripts.REFUSAL_LANGUAGE_1
                elif self.language_strike_count == 2:
                    refusal_text = PRDScripts.REFUSAL_LANGUAGE_2
                else: # Strike 3+
                    refusal_text = PRDScripts.REFUSAL_LANGUAGE_3
            elif intent == "ESCALATION_REQUIRED":
                # High-sentiment / angry caller path: trigger escalation script and high-priority CRM ticket
                refusal_text = PRDScripts.ESCALATION
                if self.session:
                    self.session.sentiment_label = "Negative"
                    self._create_task_with_log(self.crm.create_ticket(
                        transcript=text,
                        summary="High Sentiment Alert: Angry Caller",
                        sentiment="Negative",
                        call_logger=self.call_logger,
                        call_id=self.session.crm_call_id or self.session.session_id,
                        title="High Sentiment Alert: Angry Caller",
                        session_obj=self.session
                    ))
                    # [STORY-DUMMY-CRM] Trigger Callback for Escalations
                    self._create_task_with_log(self.crm.create_callback(
                        ticket_id=self.session.crm_call_id or self.session.session_id,
                        phone_number=self.session.call_context.caller_number,
                        reason="Escalation Required: Angry Caller / Human Requested"
                    ))
            else:
                # Other refusals (Sensitive, Immigration, etc.) - keep existing behavior
                refusal_text = self.policy.get_refusal_script(intent)
                
                # [P5-01]: Skip CRM noise for purely ambiguous/noisy inputs
                if intent != "AMBIGUOUS":
                    # 🟢 S4 Compliance: Record CRM ticket for ALL Hard Refusals (Fees, etc.)
                    self._create_task_with_log(self.crm.create_ticket(
                        transcript=text,
                        summary=f"Policy Violation: {intent}",
                        sentiment="SECURITY_ALERT" if "SENSITIVE" in intent else "Neutral",
                        call_logger=self.call_logger,
                        call_id=self.session.crm_call_id or self.session.session_id if self.session else trace_id,
                        title=f"Policy Refusal: {intent}",
                        session_obj=self.session
                    ))
                    
                    # [STORY-DUMMY-CRM] Trigger Callback for Financial Disputes (per refusal script)
                    if intent == "HARD_REFUSAL_FINANCIAL_DISPUTES" and self.session:
                        self._create_task_with_log(self.crm.create_callback(
                            ticket_id=self.session.crm_call_id or self.session.session_id,
                            phone_number=self.session.call_context.caller_number,
                            reason="Financial Dispute: Human Follow-up Required"
                        ))

            # 🟢 S4 Hardening: Hard LLM Bypass (Policy Violation)
            if self.response_task and not self.response_task.done():
                self.response_task.cancel()
                logger.debug(f"[GOVERNANCE] Cancelled pending LLM task for Hard Bypass ({intent}).")

            self.state.transition_to(CallState.ESCALATION, trace_id=trace_id)

            # C. Strike 3: Use dedicated termination flow (awaits TTS + closes connection)
            if intent in ["HARD_REFUSAL_LANGUAGE", "HARD_REFUSAL_LANGUAGE_BYPASS"] and self.language_strike_count >= 3:
                logger.warning("[GOVERNANCE] Strike 3 — initiating graceful termination flow.")
                self.response_task = asyncio.create_task(self._language_termination_flow(refusal_text, trace_id))
                return # SHORT-CIRCUIT: Do not proceed to barge-in or brain
            
            # D. Strikes 1-2 and other refusals: Speak refusal, stay on call
            # [P5-01]: "The Cough Problem" - Silent Ambiguity on Barge-in
            # If the user made noise (cough/filler) that triggered VAD (INTERRUPTED state), 
            # and the resulting transcript is AMBIGUOUS, do not speak the apology.
            if intent == "AMBIGUOUS" and self.state.get_state() == CallState.INTERRUPTED:
                logger.info(f"[GOVERNANCE] Silent Ambiguity: Noise/Filler detected during barge-in ('{text}'). Skipping apology.")
                # Transition back to listening if we were interrupted by noise
                self.state.transition_to(CallState.LISTENING, trace_id=trace_id)
                return

            self.response_task = asyncio.create_task(self.speak_immediate_response(refusal_text, trace_id=trace_id))
            return # SHORT-CIRCUIT
        # 3. CONTEXT EXTRACTION — must happen before barge-in so names/slots are never dropped
        # even when the user speaks while the AI is mid-sentence.
        if self.session:
            self.context_manager.update_context(self.session.call_context, text, intent)

        # 4. INTERRUPTION & BARGE-IN (Only for PROCEED intents)
        if self.response_task and not self.response_task.done():
            # [S4-11 FIX]: Check both SPEAKING and INTERRUPTED states
            # Because a partial transcript might have already flipped the state to INTERRUPTED
            current_state = self.state.get_state()
            # [BARGE-IN FIX]: Use pre_transition_state because state is already TRANSCRIBING
            # by the time we reach here (transition fired earlier in this method).
            # pre_transition_state reflects what the AI was doing when the user spoke.
            is_speaking = pre_transition_state in [CallState.SPEAKING, CallState.INTERRUPTED]

            # [ECHO-SUPPRESSION] Discard transcript if it matches the agent's own voice echoed
            # back by Twilio. Must be checked before response_task.cancel() so the agent
            # continues speaking uninterrupted when echo is detected.
            if is_speaking and self._is_echo_transcript(raw_text):
                logger.debug(f"[ECHO-SUPPRESSION] Discarding echo transcript during SPEAKING: '{raw_text}'")
                return

            logger.debug(f">>> BARGE-IN DETECTED: state={current_state}, speaking={is_speaking}")
            
            if self.call_logger:
                # [HIGH-P3-02]: Removed mid-call save_log to preserve immutability. Event stream handles crash resilience.
                self.call_logger.log_event("orchestrator", "user_interruption", meta={"is_speaking": is_speaking})

            # CRM Interaction Note for every barge-in / interruption
            if self.session:
                self._create_task_with_log(self.crm.create_ticket(
                    transcript=f"[Interaction Note] User interruption while AI was {'speaking' if is_speaking else 'processing'}.\nLast user text: '{text}'",
                    summary="Interaction Note: User Interruption (Barge-in)",
                    sentiment="Neutral",
                    call_logger=self.call_logger,
                    call_id=self.session.crm_call_id or self.session.session_id,
                    title="Interaction Note: Barge-in",
                    session_obj=self.session
                ))
            
            self.response_task.cancel()
            if is_speaking:
                # [S4-11]: Immediate Stop & Clear (Final Transcript mirroring Partial logic)
                logger.info(f">>> IMMEDIATE STOP: Interrupted by final transcript: '{raw_text}'")
                partial_text = self.synthesizer.stop_current_speech(self.sid)
                self._create_task_with_log(self._send_clear_message())
                
                # [P5-03]: Save before barge-in
                self.session_manager.save_session(self.session)
                
                logger.debug("AI was speaking. triggering barge-in handler.")
                self.response_task = asyncio.create_task(
                    self.handle_barge_in(self.sid, text, trace_id=trace_id, partial_text=partial_text)
                )
                return
            elif current_state == CallState.TRANSCRIBING and pre_transition_state in [CallState.SPEAKING, CallState.INTERRUPTED]:
                # [BARGE-IN FIX]: Dev-Phone / environments that don't send Twilio VAD 'speech' events
                # never flip state SPEAKING -> INTERRUPTED before the STT final arrives.
                # By the time we reach here, state has already raced to TRANSCRIBING, but the
                # response_task is still running (TTS may still be streaming buffered audio).
                # We must stop TTS output now and answer the user's actual question.
                # [STAB-02 FIX]: Guard added — pre_transition_state must be SPEAKING or INTERRUPTED.
                # Without this, a Deepgram continuation final arriving during INTENT_EVAL (agent
                # thinking, not speaking) would route here, trigger handle_barge_in → INTERRUPTED,
                # and cause 5-6s of active silence. The else branch below handles that case cleanly.
                logger.info(f">>> LATE BARGE-IN (state=TRANSCRIBING): Stopping residual TTS for: '{raw_text}'")
                partial_text = self.synthesizer.stop_current_speech(self.sid)
                self._create_task_with_log(self._send_clear_message())
                if self.session:
                    self.session_manager.save_session(self.session)
                self.response_task = asyncio.create_task(
                    self.handle_barge_in(self.sid, text, trace_id=trace_id, partial_text=partial_text)
                )
                return
            else:
                logger.debug("AI was only thinking/processing. Silently cancelling old task.")
        # 3. Start Parallel Response Generation (Normal Flow)
        # S4-9: Context already updated above (before barge-in check). Log the snapshot here.
        if self.session:
            import hashlib
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
                    "slot_values": memory_state,
                    "reason_for_update": "Context updated pre-barge-in"
                }, trace_id=trace_id)
        
        # We start the task and let it handle its own errors (including LatencyBreachError)
        self.response_task = asyncio.create_task(self.generate_and_speak(text, intent=intent, trace_id=trace_id, turn_start_time=turn_start_time, stt_latency=stt_latency))

    async def handle_barge_in(self, call_id: str, caller_input: str, trace_id: str = None, partial_text: str = None):
        """
        Story S4-11: Core Barge-in logic. Robustified to prevent deadlocks.
        """
        if not self.session: return

        # Prevent redundant RAG searches for the same barge-in turn
        if hasattr(self, '_rag_lock') and self._rag_lock:
            logger.debug("RAG search already in progress for this barge-in. Waiting...")
            return

        try:
            self._rag_lock = True
            # STEP A — Stop TTS + Mark Interrupted
            # We perform these synchronously in the task to ensure the state machine flips ASAP.
            if partial_text is None:
                partial_text = self.synthesizer.stop_current_speech(call_id)
                self._create_task_with_log(self._send_clear_message())
            
            self.state.transition_to(CallState.INTERRUPTED, trace_id=trace_id)

            # Identify the turn being interrupted
            prev_turn = None
            if self.session.structured_turns:
                prev_turn = self.session.structured_turns[-1]
                prev_turn.agent_response_status = "interrupted"
                prev_turn.agent_partial_response = partial_text

            # [STAB-05] Barge-in Proof: if a KB miss callback offer is still pending,
            # bypass the entire RAG + LLM path and re-ask the callback question immediately.
            # The caller interrupted the fallback phrase — the offer must still be made.
            if getattr(self, "_pending_callback_offer", False):
                logger.info("[STAB-05] Barge-in during pending callback offer — re-presenting CALLBACK_OFFER.")
                if self.call_logger:
                    self.call_logger.log_event(
                        "stab05", "callback_offer_barge_in_reissued",
                        meta={"caller_input": caller_input[:60]},
                        trace_id=trace_id
                    )
                self.state.transition_to(CallState.LISTENING, trace_id=trace_id)
                await self.speak_immediate_response(PRDScripts.CALLBACK_OFFER, trace_id=trace_id)
                return

            # STEP B — Inject RAG Context for PRD Compliance
            # FAST-PATH: If the input is extremely short (filler/command), skip RAG to reduce latency.
            is_short_input = len(caller_input.split()) <= 2
            
            logger.info(f"Barge-in detected ({'Short' if is_short_input else 'Full'} input). Grounding...")
            context_text = ""
            rag_result, rag_score, rag_topic, kb_v, c_ids = None, 0.0, "General", "unknown", []
            
            if not is_short_input:
                try:
                    rag_result, rag_score, rag_topic, kb_v, c_ids = await asyncio.wait_for(
                        self.brain.kb.search(caller_input, self.call_logger, 10, trace_id),
                        timeout=3.0
                    )
                
                    invalid_contexts = [
                        "No specific documents found.",
                        "No specific documents found due to timeout.",
                        "No specific documents found due to an internal knowledge base error.",
                        "LOW_CONFIDENCE_FALLBACK",
                        "BLOCKED_BY_SAFETY_GUARDRAIL",
                        "RAG Disabled by manual override."
                    ]
                    
                    if rag_result and rag_result not in invalid_contexts:
                        context_text = rag_result
                        logger.info(f"RAG context found for barge-in (Score: {rag_score:.2f}).")
                except Exception as e:
                    logger.warning(f"RAG failed during barge-in: {e}")

            # STEP C — Classify + Respond (Call Brain)
            classification, response, is_multi_step, topic, kb_v_brain, c_ids_brain = await self.brain.generate_with_classification(
                session=self.session,
                caller_input=caller_input,
                context_text=context_text,
                trace_id=trace_id
            )
            logger.info(f"[AUDIT] Barge-In classification: {classification}. Trace: {trace_id}")
            
            # Aggregate any metadata from barge-in classification too
            if self.session:
                if kb_v and kb_v != "unknown" and not self.session.call_context.kb_version_id:
                    self.session.call_context.kb_version_id = kb_v
                if c_ids:
                    for cid in c_ids:
                        if cid and cid != "unknown" and cid not in self.session.call_context.chunk_ids_used:
                            self.session.call_context.chunk_ids_used.append(cid)
                if rag_score > 0:
                    self.session.confidence_scores.append(rag_score)

            # STEP D — If LLM completely failed, fall back to the normal generation path
            # which has its own multi-model retry logic. Better than speaking a useless filler.
            if classification == "FAILED":
                logger.warning("[BARGE-IN] LLM race failed — routing to generate_and_speak fallback.")
                self.state.transition_to(CallState.LISTENING, trace_id=trace_id)
                await self.generate_and_speak(caller_input, trace_id=trace_id)
                return

            # STEP D — Update Interrupted Turn
            if classification == "NEW_TOPIC" and prev_turn:
                prev_turn.agent_response_status = "abandoned"
                # [STAB-03] Clear stale RAG context so the LLM cannot hallucinate the old topic
                # into the new answer. prefetched_context_task and retrieved_chunks_cache are
                # turn-scoped — they must not bleed across a topic boundary.
                if self.session:
                    self.session.prefetched_context_task = None
                    self.session.retrieved_chunks_cache = []
                    self.session.last_intent = None

            if prev_turn:
                prev_turn.barge_in_classification = classification

            # [STAB-03] Phase 6: Session-Persistent Offer Logic (MEDIUM-M2)
            # Fires ONCE per session when a multi-step answer is abandoned (NEW_TOPIC barge-in).
            # Uses PRDScripts.CONTINUATION_OFFERED so the script is centrally managed.
            _continuation_was_offered = False
            if prev_turn and prev_turn.is_multi_step and classification == "NEW_TOPIC" and not self.session.continuation_offered:
                if response and not response.endswith(("?", ".")): response += "."
                response += f" {PRDScripts.CONTINUATION_OFFERED}"
                self.session.continuation_offered = True
                _continuation_was_offered = True

            # STEP E — Create New Turn Entry
            new_id = len(self.session.structured_turns) + 1
            new_turn = BargeInTurn(
                turn_id=new_id,
                caller_input=caller_input,
                topic=topic,
                agent_response_status="completed",
                agent_partial_response=None,
                barge_in_classification=None,
                is_multi_step=is_multi_step,
                continuation_offered=_continuation_was_offered  # [STAB-03] accurately reflects whether offer was appended
            )
            self.session.structured_turns.append(new_turn)
            self.session.current_speaking_turn_id = new_id
            if hasattr(self, 'session_manager') and self.session_manager:
                self.session_manager.save_session(self.session)

            # Speak it
            await self.speak_immediate_response(response, trace_id=trace_id)

        finally:
            self._rag_lock = False
            if self.state.get_state() == CallState.INTERRUPTED:
                logger.debug("Barge-in task cleanup: Reverting INTERRUPTED -> LISTENING")
                self.state.transition_to(CallState.LISTENING, trace_id=trace_id)

    async def handle_competitor_refusal(self, trace_id=None):
        """
        STAB-07: Isolated handler for competitor queries.
        Ensures polite refusal without CRM ticket or call termination.
        """
        # [STAB-07] STRICT: Use pre-approved static response only.
        # No dynamic templating, no variables, no LLM drift possible.
        refusal_text = PRDScripts.REFUSAL_COMPETITORS
        
        # Safety Guard: Ensure no dynamic content/comparisons/positioning (redundant but safe)
        if not self.policy.validate_no_comparison(refusal_text):
            logger.error(f"[STAB-07] Safety Guard Failure: Competitor refusal script contained prohibited comparisons!")
            refusal_text = "I can only provide information about GD College and cannot compare us with other institutions."

        # Speak it
        # [GUARD] competitor_refusal_triggered=true
        logger.info(f"[STAB-07] Competitor refusal triggered (trace_id={trace_id})")
        
        # [GOVERNANCE] Ensure any pending tasks are cancelled before speaking static refusal
        if self.response_task and not self.response_task.done():
            self.response_task.cancel()
            
        self.response_task = asyncio.create_task(self.speak_immediate_response(refusal_text, trace_id=trace_id))

    async def speak_immediate_response(self, text, trace_id=None):
        """
        Helper to speak an immediate response message without using the Brain.
        Formerly speak_refusal().
        """
        logger.debug(f"Starting Speak Immediate Response: '{text}'")
        # CRITICAL: Reset empty frame counter when agent speaks
        if self.consecutive_empty_frames > 0:
            logger.debug(f"[RESET] Counter {self.consecutive_empty_frames}→0 (agent speaking)")
            self.consecutive_empty_frames = 0
            self.non_english_run_start = time.time()
        
        # Only transition to SPEAKING if we are NOT already terminating.
        if self.state.get_state() == CallState.SPEAKING:
            # Force clear if already speaking to allow new response to take over immediately
            await self._send_clear_message()

        # Skipping this when in CALL_END prevents the state violation
        if self.state.get_state() != CallState.CALL_END:
            self.state.transition_to(CallState.SPEAKING, trace_id=trace_id)
        
        is_cancelled = False
        try:
            # Add to history so LLM knows it spoke
            if self.session:
                self.session.conversation_history.append({"role": "model", "parts": [text]})

            # Speak it with a safety timeout to prevent "dead silence"
            chunks_sent = 0
            self._current_speaking_text = text  # [ECHO-SUPPRESSION] track for echo detection
            async with asyncio.timeout(10.0): # 10s max for immediate response tts
                async for chunk in self.synthesizer.speak(text, call_id=self.sid):
                    await self._send_response_chunk(chunk)
                    chunks_sent += 1
            
            logger.info(f"AI (Immediate): {text} (Sent {chunks_sent} audio chunks)")
            
            # Typical Pattern: Wait for the agent to finish speaking before allowing more input
            if self.mode == "audio":
                duration = len(text) / 15.0
                logger.debug(f"Response streamed to client. Sleeping {duration:.1f}s to align with client playback.")
                for _ in range(int(duration * 10)):
                    await asyncio.sleep(0.1)

            # LOGGING: Record the response in JSON logs
            if self.call_logger:
                self.call_logger.log_event("brain", "interrupt_response_spoken", meta={"text": text, "chunks": chunks_sent}, trace_id=trace_id)
        except asyncio.CancelledError:
            is_cancelled = True
            logger.info("Speak-immediate task cancelled by user interruption.")
            raise
        except asyncio.TimeoutError:
            logger.error(f"Immediate Response TTS Timed Out for text: {text}")
        except Exception as e:
            logger.error(f"Error in speak_immediate_response: {e}")
        finally:
            self._current_speaking_text = ""  # [ECHO-SUPPRESSION] clear when TTS ends
            
            # [GOVERNANCE] Auto-terminate if the response concludes the call
            normalized_text = text.strip().lower().rstrip(string.punctuation).strip()

            if not is_cancelled and normalized_text.endswith("goodbye"):
                logger.info(f"[CALL-TERMINATION] 'Goodbye' detected in immediate response. Initiating graceful shutdown.")
                # We skip resetting to LISTENING since we are hanging up
                self._create_task_with_log(self._delayed_call_end(delay=1.0))
            else:
                # CRITICAL: Only go back to Listening if we are NOT already terminating nor cancelled
                if not is_cancelled and self.state.get_state() != CallState.CALL_END:
                    self.state.transition_to(CallState.LISTENING, trace_id=trace_id)
            
            self.last_interaction_time = time.time()

    async def _send_clear_message(self):
        """Sends a 'clear' event to the client to purge audio buffers."""
        if self.websocket:
            try:
                # Use identical sid resolution as _send_response_chunk to ensure frontend matches it
                target_sid = self.sid or (self.session.session_id if self.session else None)
                msg = {"event": "clear", "streamSid": target_sid}
                await self.websocket.send_text(json.dumps(msg))
                logger.debug(f"[TELEPHONY] Sent 'clear' event to client {target_sid}")
            except Exception as e:
                logger.error(f"Failed to send clear message: {e}")

    async def _flush_incomplete_utterance(self, delay: float, detected_lang: str = None):
        """
        [ENDPOINTING] Flush the _incomplete_utterance buffer after `delay` seconds of
        silence.  Called when the user says a dangling fragment (e.g. "Tell me about")
        and then stops talking without sending a continuation final transcript.

        Re-enters _on_transcript with the buffered text so normal processing applies.
        """
        await asyncio.sleep(delay)
        text = self._incomplete_utterance
        if text:
            self._incomplete_utterance = ""
            self._buffer_flush_task = None
            logger.debug(f"[ENDPOINTING] Flushing buffered utterance after {delay:.0f}s timeout: '{text}'")
            await self._on_transcript(
                text,
                confidence=0.9,
                stt_latency=0.0,
                is_final=True,
                detected_lang=detected_lang,
            )

    async def handle_audio_stream(self, websocket, mode: str = "audio", call_id: str = None, caller_id: str = None):
        """
        Main Loop: Coordinates the flow from Twilio (WebSocket) through STT, Brain, and TTS.
        """
        self.websocket = websocket
        
        # 0. EXPLICITLY CAPTURE EARLY METADATA FOR FALLBACK CLEANUP
        self._early_sid = call_id or websocket.query_params.get("CallSid", "unknown")
        self._early_caller = caller_id or websocket.query_params.get("from", "unknown")

        # 0.5. CONCURRENCY SAFETY GATE (S4-7)
        from telephony.concurrency import is_over_capacity_atomic, MAX_INBOUND_CALLS
        # [M1 FIX] Authoritative Secondary Enforcement:
        # Uses atomic Lua to ensure zero-leak concurrency enforcement during high bursts.
        if await is_over_capacity_atomic(MAX_INBOUND_CALLS, call_sid=self._early_sid):
            logger.critical("[SAFETY GATE] Concurrent active calls at/exceeded hard cap inside pipeline. Rejection triggered.")
            
            # 1. Release the slot IMMEDIATELY to prevent "clogging" during a burst
            from telephony.concurrency import decrement_active_calls
            await decrement_active_calls(call_sid=self._early_sid)

            # 2. Record CRM ticket asynchronously (Pillar 3 Forensic Pillar)
            from datetime import datetime
            from agent_logging import mask_phone_number
            timestamp = datetime.now().isoformat()
            summary = f"Caller number: {mask_phone_number(self._early_caller)}, reason = OVER_CAPACITY, timestamp: {timestamp}"
            
            self._create_task_with_log(self.crm.create_ticket(
                transcript="Call rejected inside WebSocket pipeline due to hard 30-call concurrency limit.",
                summary=summary,
                sentiment="Negative",
                call_id=self._early_sid,
                title="OVER_CAPACITY | Voice Agent Safety Gate",
                session_obj=getattr(self, 'session', None)
            ))

            # 3. Handle state transition and fallback audio
            self.state.transition_to(CallState.ESCALATION)
            try:
                # PRD: "it is impossible for an over-limit call to receive a partial or degraded AI response"
                # Use local mulaw file to prevent ANY billable Deepgram TTS API usage for rejected callers.
                await self.synthesizer.play_fallback_audio(websocket)
                await asyncio.sleep(1) # Final grace period for audio delivery
            except Exception as e:
                logger.error(f"Failed to play capacity-failure audio: {e}")
            
            await websocket.close(code=1008, reason="Over Capacity")
            await self.cleanup()
            return

        # 0. INTAKE GUARDRAIL (Kill Switch)
        if not self.config.is_intake_enabled:
            logger.critical(f"Connection Rejected: INTAKE_DISABLED is active (env={self.config.env})")
            # Close with Policy Violation code (1008)
            await websocket.close(code=1008, reason="Intake Disabled")
            await self.cleanup()
            return

        self.mode = "audio"
        
        # Set the callback and connect
        self.transcriber.set_callback(self._on_transcript)
        self.transcriber.set_listener_error_callback(self._on_stt_listener_error)
        connected = await self.transcriber.connect()
        
        if not connected:
            logger.error("[TELEPHONY] Audio stream aborted: STT provider failed to connect.")
            if self.session:
                self.session.termination_reason = "system_failure"
            
            # Immediately tell the user we're broken and hang up
            self.state.transition_to(CallState.ESCALATION)
            try:
                # We can wait for TTS generator to finish delivering this pre-recorded/synthesized text
                goodbye_text = PRDScripts.APOLOGY_FATAL
                async for chunk in self.synthesizer.speak(goodbye_text):
                    await self._send_response_chunk(chunk)
                await asyncio.sleep(2) # Give audio time to play on client side
            except Exception as e:
                logger.error(f"Failed to play connect-failure audio: {e}")
            
            await self.cleanup()
            return

        sid = None
        call_sid = None

        try:
            while True:
                # 1. Wait for 'start' or first 'media' to get Identity
                try:
                    # [TWILIO-HARDENING]: Increase timeout from 2.0s to 5.0s. 
                    # Twilio can be slow to start the 'media' events over high-latency networks.
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
                logger.warning("WebSocket disconnected before Twilio start event. Triggering cleanup for early-exit CRM ticket.")
                return # finally block will call cleanup()

            # 🟢 ENTER SESSION CONTEXT (Pillar 3)
            # Use 'from' number extracted from Twilio if available
            caller_num = "unknown"
            if self.websocket:
                caller_num = self.websocket.query_params.get("from", "unknown")
            
            # CRITICAL FIX: Align session_manager ID with call_logger ID for forensic logging of structured_turns
            canonical_session_id = self.call_logger.call_id if self.call_logger else sid
            
            async with self.session_manager.session_scope(canonical_session_id, call_sid, caller_number=caller_num) as session:
                self.session = session
                # Reset wrap-up tracking for this new session
                self.wrapup_triggered = False
                # Prefer canonical session start_time if available, else wall clock now
                try:
                    self.session_start_wall_time = self.session.start_time.timestamp()
                except Exception:
                    self.session_start_wall_time = time.time()
                self.state.transition_to(CallState.CALL_INIT)
                logger.info(f"Telephony Stream Started: {self.session.session_id} (Stream SID: {self.sid})")
                
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

                # In PRD, caller_type should dynamically track based on conversation intent.
                # However, at Call Start, we have no intent yet. 
                # We start as "unknown_lead", and will update dynamically later based on text.
                # [PRD Rule - Commented]
                # Stated field log_call is required before greeting to ensure session linking.
                # crm_id = await self.crm.log_call(...)

                # [TESTING Rule - Active]
                # Spawn CRM logging as a background task to prevent blocking the GREETING 
                # especially in high-latency local environments (Ngrok) or when CRM is offline.
                async def _log_call_bg():
                    try:
                        crm_id = await self.crm.log_call(
                            call_id=self.session.session_id,
                            caller_phone=self.session.caller_number,
                            caller_type="unknown_lead",
                            summary="Incoming Call from Voice Agent",
                            transcript="[Call Started]", 
                            sentiment="Neutral"
                        )
                        if crm_id:
                            self.session.crm_call_id = str(crm_id)
                            if "twilio_metadata" not in self.session.metadata:
                                self.session.metadata["twilio_metadata"] = {}
                            self.session.metadata["twilio_metadata"]["stream_sid"] = self.sid
                            self.session.metadata["twilio_metadata"]["call_sid"] = call_sid
                            logger.info(f"CRM Call Logged: {crm_id}")
                    except Exception as e:
                        logger.error(f"Failed to log call to CRM: {e}")
                
                self._create_task_with_log(_log_call_bg())

                # Initial Greeting
                # [P5-03]: Explicit save BEFORE update_state to prevent Redis overwrite of caller_type/crm_id
                self.session_manager.save_session(self.session)
                self.session_manager.update_state(self.session.session_id, SessionState.SPEAKING)
                
                # Warm-up delay to allow browser AudioContext to stabilize
                await asyncio.sleep(0.5)
                
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

                        # STATE: Start of call
                        if self.state.get_state() == CallState.CALL_INIT:
                             self.state.transition_to(CallState.LISTENING)

                        payload = base64.b64decode(data['media']['payload'])
                        if self.recorder:
                            self.recorder.write_chunk(payload)
                        try:
                            await self.transcriber.send_audio(payload)
                            self._last_stt_packet_time = time.time()  # [WATCHDOG] Audio delivered → connection is live
                            # [STAB-02] Log Point 4: first audio frame forwarded to Deepgram post-TTS
                            if self.state.get_state() == CallState.LISTENING and not self._post_tts_ingress_logged:
                                self._post_tts_ingress_logged = True
                                _turn_num = self.session.current_speaking_turn_id if self.session else -1
                                if self.call_logger:
                                    self.call_logger.log_event(
                                        "stab02", "stt_ingress_first_frame_post_tts",
                                        meta={"turn_number": _turn_num, "state": CallState.LISTENING.value},
                                        trace_id=getattr(self, '_current_trace_id', None)
                                    )
                                logger.info(f"[STAB-02][stt_ingress_first_frame_post_tts] turn={_turn_num} — Deepgram receiving audio post-TTS")
                        except Exception as stt_err:
                            logger.error(f"[FAILOVER] STT send failed: {stt_err}. Attempting recovery...")
                            # RECOVERY: Try to acquire a new transcriber FROM THE POOL immediately
                            try:
                                from stt.stt_pool import stt_pool, PooledTranscriber
                                raw_stt = await stt_pool.acquire(timeout=3.0)
                                old_stt = self.transcriber
                                self.transcriber = PooledTranscriber(stt_pool, raw_stt)
                                self.transcriber.set_callback(self._on_transcript)
                                self.transcriber.set_listener_error_callback(self._on_stt_listener_error)
                                # Drain old STT
                                asyncio.create_task(old_stt.close())
                                logger.info("[FAILOVER] STT provider hot-swapped successfully.")
                                # Retry the send once
                                await self.transcriber.send_audio(payload)
                            except Exception as failover_err:
                                logger.critical(f"[FATAL] STT Failover failed: {failover_err}")
                                raise ConnectionError("STT Failover Exhausted")
                        self.session.touch() # Life signal
                    
                    elif data['event'] == 'speech':
                        # [P5-01]: Hardened Telephony-Layer Interruption
                        telephony_speech_start = asyncio.get_event_loop().time()
                        logger.info(f"[TELEPHONY VAD] User speech started (Twilio Signal)")
                        if self.response_task and not self.response_task.done():
                            current_state = self.state.get_state()
                            if current_state == CallState.SPEAKING:
                                # [FIX-4] Debounce: require VAD_DEBOUNCE_MS between consecutive speech events
                                # to filter 8kHz telephony noise, echo, and line artifacts.
                                # Threshold is config-driven (env var) per PRD constraint.
                                _vad_now = asyncio.get_event_loop().time()
                                _vad_last = getattr(self, '_last_speech_event_time', 0.0)
                                _vad_debounce = float(os.getenv("VAD_DEBOUNCE_MS", "250")) / 1000.0
                                if _vad_now - _vad_last < _vad_debounce:
                                    logger.debug(f"[TELEPHONY VAD] Debounced speech event ({(_vad_now - _vad_last)*1000:.0f}ms since last). Ignoring.")
                                    continue
                                self._last_speech_event_time = _vad_now

                                logger.info(">>> IMMEDIATE STOP: Interrupted by Telephony VAD signal.")
                                self.synthesizer.stop_current_speech(self.sid)
                                await self._send_clear_message() # M1: Await clear message confirmed
                                self.state.transition_to(CallState.INTERRUPTED)
                                
                                # Latency Enforcement Check (Task Requirement)
                                # M1: Capturing 'True-Halt' latency with 20ms network overhead buffer
                                audio_output_halt = asyncio.get_event_loop().time()
                                halt_latency = ((audio_output_halt - telephony_speech_start) * 1000) + 20.0
                                logger.info(f"[LATENCY] Telephony VAD Halt: {halt_latency:.1f}ms (Ceiling: 300ms, includes 20ms network overhead)")
                                
                                if halt_latency > 300:
                                    logger.warning(f"[LATENCY_BREACH] Telephony halt exceeded 300ms budget: {halt_latency:.1f}ms")
                                
                                # [TWILIO-HARDENING]: Always start a safety timer after VAD-triggered halt.
                                # If no qualifying transcript arrives within 4s, revert INTERRUPTED -> LISTENING.
                                # [FIX-2] Removed is_speaking() guard: method does not exist on any TTS class
                                # and caused AttributeError → orchestrator crash whenever a speech event fired
                                # while the agent was speaking. The state check inside the timeout is sufficient.
                                if self._vad_safety_task:
                                    self._vad_safety_task.cancel()

                                async def _vad_safety_timeout(trace_id):
                                    await asyncio.sleep(4.0)
                                    if self.state.get_state() == CallState.INTERRUPTED:
                                        logger.info("[TELEPHONY VAD] No transcript followed interruption. Reverting to LISTENING.")
                                        self.state.transition_to(CallState.LISTENING, trace_id=trace_id)

                                self._vad_safety_task = self._create_task_with_log(_vad_safety_timeout(None))

                    # [CALL-CPR] Watchdog Check: 7s of absolute silence (no DG heartbeats) = Dead Connection
                    # We throttle this check to once per second to avoid CPU lag
                    now = time.time()
                    if now - self._watchdog_check_time > 1.0:
                        self._watchdog_check_time = now
                        if self.state.get_state() == CallState.LISTENING:
                            _watchdog_timeout = float(os.getenv("STT_WATCHDOG_TIMEOUT_S", "30.0"))
                            if now - self._last_stt_packet_time > _watchdog_timeout:
                                logger.warning(f"[CALL-CPR] STT silence watchdog triggered ({_watchdog_timeout}s). Forcing Hot-Swap.")
                                self._last_stt_packet_time = now # Reset to avoid loop
                                self._create_task_with_log(self._on_stt_listener_error(RuntimeError("Watchdog timeout")))
                    
                    elif data['event'] == 'stop':
                        logger.debug(f"Telephony Stream Stopped: {sid}")
                        break
        except Exception as e:
            logger.error(f"CRITICAL ORCHESTRATOR CRASH: {e}", exc_info=True)
            if self.session:
                self.session.termination_reason = "system_failure"
            
            # Attempt to play goodbye message if socket still open and we aren't already closing
            try:
                if self.websocket and self.state.get_state() != CallState.CALL_END:
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
            # Reset wrap-up tracking for text-mode sessions as well
            self.wrapup_triggered = False
            try:
                self.session_start_wall_time = self.session.start_time.timestamp()
            except Exception:
                self.session_start_wall_time = time.time()
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
            # [P5-03]: Atomic Redis Save Pattern (Tasks 1-2-3)
            # 1. Append structured_turns
            if self.session:
                new_id = len(self.session.structured_turns) + 1
                turn = StandardTurn(
                    turn_id=new_id,
                    caller_input=text,
                    topic=intent, 
                    agent_response_status="completed",
                    agent_partial_response=None,
                    barge_in_classification=None,
                    is_multi_step=False
                )
                self.session.structured_turns.append(turn)
                self.session.current_speaking_turn_id = new_id
            
            # 2. await save_session()
            self.session_manager.save_session(self.session)
            
            # 3. await update_state()
            # Transitioning to INTENT_EVAL here to satisfy the 1-2-3 sequence logic
            self.state.transition_to(CallState.INTENT_EVAL, trace_id=trace_id)
            self.session_manager.update_state(self.session.session_id, SessionState.LISTENING) # Initial state update

            full_ai_text = ""
            audio_queue = asyncio.Queue()

            # Worker: Speaks chunks as they arrive from the brain
            async def tts_worker():
                total_chars = 0
                sentence_idx = 0
                worker_start_time = time.time()
                logger.info(f"[TTS-WORKER] Started trace={trace_id}")
                try:
                    while True:
                        sentence = await audio_queue.get()
                        if sentence is None: break
                        sentence_idx += 1
                        total_chars += len(sentence)
                        sentence_start_time = time.time()
                        logger.info(
                            f"[TTS-WORKER] Sentence #{sentence_idx} start: "
                            f"chars={len(sentence)} total_so_far={total_chars} trace={trace_id}"
                        )

                        # [STAB-02] Log Point 1: TTS Entry
                        _turn_num = self.session.current_speaking_turn_id if self.session else -1
                        if self.call_logger:
                            self.call_logger.log_event(
                                "stab02", "tts_entry",
                                meta={
                                    "turn_number": _turn_num,
                                    "state_before_speaking": self.state.get_state().value,
                                    "sentence_idx": sentence_idx,
                                    "text_preview": sentence[:60],
                                },
                                trace_id=trace_id
                            )
                        logger.info(f"[STAB-02][tts_entry] turn={_turn_num} state={self.state.get_state().value} sentence={sentence_idx} trace={trace_id}")

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
                        self._current_speaking_text = sentence  # [ECHO-SUPPRESSION] track for echo detection

                        try:
                            async for chunk in self.synthesizer.speak(sentence, call_id=self.sid):
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
                        except TTSException as e:
                            logger.error(f"TTS Synthesis Failed: {e}. Triggering fallback audio.")
                            # PRD §7: No-Silence Guarantee. Trigger local fallback.
                            if hasattr(self.synthesizer, 'play_fallback_audio'):
                                await self.synthesizer.play_fallback_audio(self.websocket, streamSid=self.sid)
                            
                        sentence_elapsed_ms = int((time.time() - sentence_start_time) * 1000)
                        logger.info(
                            f"[TTS-WORKER] Sentence #{sentence_idx} complete: "
                            f"elapsed={sentence_elapsed_ms}ms chars={len(sentence)} trace={trace_id}"
                        )
                        audio_queue.task_done()

                    # Calculate estimated playback duration (approx 10 chars per sec for natural TTS)
                    estimated_play_time = total_chars / 10.0
                    time_spent_generating = time.time() - worker_start_time
                    remaining_time = estimated_play_time - time_spent_generating
                    logger.info(
                        f"[TTS-WORKER] All {sentence_idx} sentence(s) streamed: "
                        f"total_chars={total_chars} estimated_play={estimated_play_time:.1f}s "
                        f"gen_time={time_spent_generating:.1f}s sync_sleep={max(0, remaining_time):.1f}s "
                        f"trace={trace_id}"
                    )
                    if remaining_time > 0:
                        logger.debug(f"[TTS-WORKER] Sleeping {remaining_time:.1f}s to align with client playback.")
                        await asyncio.sleep(remaining_time)
                except asyncio.CancelledError:
                    # [STAB-02] Log Point 2: TTS completion source — task cancelled
                    _turn_num = self.session.current_speaking_turn_id if self.session else -1
                    if self.call_logger:
                        self.call_logger.log_event(
                            "stab02", "tts_completion_source",
                            meta={"source": "task_cancelled", "turn_number": _turn_num, "sentence_idx": sentence_idx},
                            trace_id=trace_id
                        )
                    logger.info(f"[STAB-02][tts_completion_source] source=task_cancelled turn={_turn_num} sentence={sentence_idx} trace={trace_id}")

                finally:
                    self._current_speaking_text = ""  # [ECHO-SUPPRESSION] clear when TTS ends
                    # Back to Listening when done speaking (if not escalated)
                    # [FIX]: Don't overwrite state if user already started INTERRUPTED, TRANSCRIBING, or if AI is generating
                    _state_at_finally = self.state.get_state()
                    _guard_states = [
                        CallState.ESCALATION, CallState.CALL_END, CallState.INTERRUPTED,
                        CallState.TRANSCRIBING, CallState.INTENT_EVAL,
                        CallState.RETRIEVAL, CallState.RESPONSE_VALIDATION
                    ]
                    _guard_blocked = _state_at_finally in _guard_states
                    _turn_num = self.session.current_speaking_turn_id if self.session else -1
                    # [STAB-02] Log Point 3: SPEAKING → LISTENING transition decision
                    if self.call_logger:
                        self.call_logger.log_event(
                            "stab02", "speaking_to_listening_decision",
                            meta={
                                "turn_number": _turn_num,
                                "state_at_finally": _state_at_finally.value,
                                "guard_blocked": _guard_blocked,
                                "will_transition": not _guard_blocked,
                            },
                            trace_id=trace_id
                        )
                    logger.info(f"[STAB-02][speaking_to_listening_decision] turn={_turn_num} state_at_finally={_state_at_finally.value} guard_blocked={_guard_blocked} trace={trace_id}")
                    if not _guard_blocked and not self._cleanup_done:
                        self.state.transition_to(CallState.LISTENING, trace_id=trace_id)
                        # Reset interaction time so silence monitor starts counting from NOW
                        self.last_interaction_time = time.time()
                        self._post_tts_ingress_logged = False  # Arm Log Point 4 for next turn
                    logger.info(
                        f"[TTS-WORKER] Done. final_state={self.state.get_state().value} "
                        f"sentences={sentence_idx} total_chars={total_chars} trace={trace_id}"
                    )

            worker_task = asyncio.create_task(tts_worker())
            
            try:
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
                    
                    # Task 3 & 4: Early Latency Check (Before RAG/LLM)
                    self._latency_alert_emitted = False
                    if turn_start_time:
                        current_turn_elapsed = asyncio.get_event_loop().time() - turn_start_time
                        # --- [DYNAMIC ENVIRONMENT-AWARE CIRCUIT BREAKER] ---
                        from contracts.config import config
                        CERTAINTY_BUDGET = config.turn_latency_circuit_break_s
                        
                        if current_turn_elapsed > (CERTAINTY_BUDGET * 0.6):
                            sid = self.session.session_id if self.session else "unknown"
                            logger.warning(f"[LATENCY_ALERT] Pressure detected | {sid} | Elapsed: {current_turn_elapsed:.2f}s")
                            self._latency_alert_emitted = True
                            
                            if self.call_logger:
                                self.call_logger.log_event("alert", "sustained_latency_pressure", latency_ms=int(current_turn_elapsed*1000), meta={"threshold": CERTAINTY_BUDGET*0.6, "status": "warning"})

                        if current_turn_elapsed > CERTAINTY_BUDGET:
                            logger.error(f"[LATENCY_CIRCUIT_BREAK] Pre-generation latency exceeded budget: {current_turn_elapsed:.2f}s. Breaking call to prevent 'Dead Air'.")
                            raise LatencyBreachError(f"High pre-generation delay: {current_turn_elapsed:.2f}s")

                    # Extract Caller Number for Auto-ID
                    caller_num = self.session.caller_number if self.session else "unknown"
                    
                    # Use persistent context
                    active_context = self.session.call_context if self.session else None
                    
                    if getattr(self, "flags", {}).get("block_retrieval"):
                        return
                        
                    # Use pre-fetched context if available
                    prefetched_task = getattr(self.session, 'prefetched_context_task', None)
                    if prefetched_task:
                        logger.debug(f"[STREAM BUFFER] Passing pre-fetched context for turn: {text[:20]}...")

                    async for sentence, metadata in self.brain.generate_stream(
                        text, 
                        self.session.conversation_history, 
                        caller_number=caller_num, 
                        intent=intent, 
                        trace_id=trace_id, 
                        call_context=active_context,
                        prefetched_context_task=prefetched_task,
                        degraded_mode=self._latency_alert_emitted
                    ):
                        # [STAB-05] KB miss detection — metadata is already unpacked by the async-for.
                        # generate_stream yields 2-tuples (sentence, metadata), so sentence is always
                        # a string here. Check metadata directly for the kb_miss signal.
                        if metadata and metadata.get("error") == "kb_miss":
                            self._pending_callback_offer = True
                            self._pending_callback_query = metadata.get("caller_query", text)
                            logger.warning(
                                f"[STAB-05] KB miss — bypassing audio_queue, speaking verbatim fallback directly. "
                                f"query='{self._pending_callback_query[:60]}'"
                            )
                            if self.call_logger:
                                self.call_logger.log_event(
                                    "stab05", "kb_miss_fallback",
                                    meta={
                                        "caller_query": self._pending_callback_query,
                                        "rag_score": metadata.get("rag_score", 0.0),
                                        "category_threshold": metadata.get("category_threshold", 0.58),
                                    }
                                )
                            # Speak verbatim fallback via speak_immediate_response so it is
                            # observable in tests and does NOT enter the audio_queue/tts_worker path.
                            await self.speak_immediate_response(sentence, trace_id=trace_id)
                            break  # No more streaming — the CALLBACK_OFFER is injected below.

                        if sentence and isinstance(sentence, tuple):
                             # Legacy guard: handles any generator that wraps payload as (text, meta)
                             sentence, error_meta = sentence
                             if "error" in error_meta:
                                 logger.debug(f"[LOG] Refusal reason: {error_meta.get('error')}")

                        # Update session metrics from RAG search (tracked via events)
                        if self.session and metadata:
                             rag_score = metadata.get("rag_score", 0.0)
                             if rag_score > 0:
                                 # Cumulative score list for forensic analysis (crash-protection)
                                 self.session.confidence_scores.append(rag_score)
                        # Invalidate after first usage in this turn
                        if hasattr(self.session, 'prefetched_context_task'):
                            self.session.prefetched_context_task = None
                        # --- LATENCY ENFORCEMENT (Task 3 & 4) ---
                        # --- [DYNAMIC ENVIRONMENT-AWARE CIRCUIT BREAKER] ---
                        from contracts.config import config
                        CERTAINTY_BUDGET = config.turn_latency_circuit_break_s

                        if turn_start_time:
                            current_turn_elapsed = asyncio.get_event_loop().time() - turn_start_time
                            
                            # 🟢 Alert for auto-scaling hook
                            if current_turn_elapsed > (CERTAINTY_BUDGET * 0.6) and not self._latency_alert_emitted:
                                logger.warning(f"[LATENCY_ALERT] Pressure detected | Elapsed: {current_turn_elapsed:.1f}s")
                                self._latency_alert_emitted = True
                                if self.call_logger:
                                    self.call_logger.log_event("alert", "sustained_latency_pressure", latency_ms=int(current_turn_elapsed*1000), meta={"threshold": CERTAINTY_BUDGET*0.6, "status": "warning"})
                                
                            # 🟢 Circuit Break ceiling
                            if current_turn_elapsed > CERTAINTY_BUDGET:
                                raise LatencyBreachError(f"Turn processing timed out at {current_turn_elapsed:.2f}s")
                                

                        self.session.touch()
                        self.session_manager.save_session(self.session)
                        self.session_manager.update_state(self.session.session_id, SessionState.SPEAKING)
                        
                        # Perform safety check (Response Policy) before streaming
                        context = self.session.call_context
                        is_safe = self.policy.validate_response(context, sentence)
                        
                        # Dynamically update the turn's topic from RAG metadata
                        kb_topic = metadata.get("topic")
                        if kb_topic and kb_topic != "General":
                            if self.session and self.session.structured_turns:
                                current_turn = self.session.structured_turns[-1]
                                if getattr(current_turn, "topic", None) in [intent, "General", "unknown"]:
                                    current_turn.topic = kb_topic

                        if self.call_logger:
                             self.call_logger.log_event("brain", "chunk_generated", meta={
                                 "text": sentence, 
                                 "rag_score": metadata.get("rag_score", 0),
                                 "grounding": metadata.get("has_grounding", False),
                                 "topic": kb_topic,
                                 "validation_pass": is_safe
                             }, trace_id=trace_id)

                        if is_safe:
                            # ── PRD §1: Map Caller Type from Intent classification ──
                            if intent and intent != "unknown" and "_chat" not in getattr(self.session, "caller_type", ""):
                                intent_type_map = {
                                    "JOB_QUERY": "job_seeker",
                                    "VENDOR_PAYMENT": "vendor",
                                    "TRANSCRIPT_REQUEST": "alumni",
                                    "FEES": "existing_student",
                                    "PROCEED": "new_student"
                                }
                                # Map it, fallback to context/RAG topic if intent misses
                                new_type = intent_type_map.get(intent)
                                if not new_type and kb_topic:
                                    kb_mapped = intent_type_map.get(kb_topic.upper())
                                    if kb_mapped: new_type = kb_mapped
                                    
                                if new_type:
                                    self.session.caller_type = new_type
                            # ────────────────────────────────────────────────────────

                            if not full_ai_text: # First sentence logic
                                llm_latency = int((time.time() - llm_start_time) * 1000)
                                if self.call_logger:
                                    self.call_logger.log_event("orchestrator", "llm_response_start", latency_ms=llm_latency, trace_id=trace_id)
                            
                            full_ai_text += sentence + " "
                            await audio_queue.put(sentence)
                        else:
                            logger.warning(f"Response Validation Failed: '{sentence}'")
                            self.state.transition_to(CallState.ESCALATION, trace_id=trace_id)
                            # Fix: Output default refusal instead of falsely claiming a language error
                            failure_msg = PRDScripts.REFUSAL_DEFAULT
                            await audio_queue.put(failure_msg)
                            full_ai_text = failure_msg
                            
                            self._create_task_with_log(self.crm.create_ticket(
                                transcript=f"Blocked Response: {sentence}\nUser Query: {text}",
                                summary="Policy Violation",
                                sentiment="QUALITY_FAILURE",
                                call_logger=self.call_logger,
                                call_id=self.session.crm_call_id or self.session.session_id if self.session else (trace_id or "quality_check"),
                                title="Quality Assurance Failure",
                                session_obj=self.session
                            ))
                            break
                
                # S4-11: Detect if this turn was a multi-step answer for future continuation offers
                if turn:
                    turn.is_multi_step = self._is_multi_step(full_ai_text)
                    if turn.is_multi_step:
                        logger.debug(f"[S4-11] Turn {turn.turn_id} flagged as MULTI-STEP")

                # S4-11: Ensure worker task is done before finishing parent task
                await audio_queue.join()
                
                # --- FINAL LATENCY CHECK (Removed completion limit to allow long responses) ---
                # We no longer kill the call if it's naturally finishing a long speech.
                pass

                await audio_queue.put(None)
                await worker_task

                # [STAB-05] KB Miss Callback Offer — appended after verbatim fallback is spoken.
                # Must NOT be part of the LLM response stream; injected deterministically here.
                if self._pending_callback_offer:
                    logger.info("[STAB-05] Appending mandatory callback offer after LOW_CONFIDENCE_FALLBACK.")
                    await self.speak_immediate_response(PRDScripts.CALLBACK_OFFER, trace_id=trace_id)

                # [GOVERNANCE] Auto-terminate if the response concludes the call
                if full_ai_text:
                    normalized_text = full_ai_text.strip().lower().rstrip(string.punctuation).strip()
                    if normalized_text.endswith("goodbye"):
                        logger.info(f"[CALL-TERMINATION] 'Goodbye' detected in AI response. Initiating graceful shutdown.")
                        self._create_task_with_log(self._delayed_call_end(delay=1.0))
                
            except asyncio.CancelledError:
                logger.debug("generate_and_speak cancelled. Cleaning up worker...")
                worker_task.cancel()
                try: await worker_task
                except asyncio.CancelledError: pass
                raise
            except Exception as e:
                logger.error(f"Error in generate_and_speak: {e}", exc_info=True)
                worker_task.cancel()
                raise
            finally:
                # Ensure worker is dead
                if not worker_task.done():
                    worker_task.cancel()
            logger.info(f"AI: {full_ai_text.strip()}")
            self.last_response_was_question = full_ai_text.strip().endswith("?")
            if self.last_response_was_question:
                self._last_ai_question_text = full_ai_text.strip()  # [STAB-04] For post-clarification repeat
            # log_conversation_turn is deprecated (PRD P3-07)
            self.session.conversation_history.append({"role": "model", "parts": [full_ai_text.strip()]})
            
            if self.call_logger:
                # [HIGH-P3-02]: Mid-call writes removed. Event stream (JSONL) is the per-turn source of truth.
                pass
            
            # CRM Background Task
            if not is_greeting:
                ticket_sentiment = "Neutral"
                ticket_summary = f"Query: {text}"
                if Brain.is_kb_refusal(full_ai_text):
                    ticket_sentiment = "ESCALATION"
                    ticket_summary = f"KB Miss - Escalation Required: {text}"
                    if self.session:
                        self.session.sentiment_label = "Negative" # Escalate sentiment on KB miss

                self._create_task_with_log(self.crm.create_ticket(
                    transcript=text,
                    summary=ticket_summary,
                    sentiment=ticket_sentiment,
                    call_logger=self.call_logger,
                    call_id=self.session.crm_call_id or self.session.session_id if self.session else "unknown_context",
                    title=f"Support Request: {ticket_sentiment}",
                    session_obj=self.session
                ))
            
            self.session_manager.save_session(self.session)
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
                
                # [S4-11 FIX]: Evaluate multi-step even on partial interruption
                if 'turn' in locals() and turn and 'full_ai_text' in locals():
                    turn.is_multi_step = self._is_multi_step(full_ai_text)
                    logger.debug(f"[S4-11] Partial turn evaluated as MULTI-STEP: {turn.is_multi_step}")
                    
                # [S4-11 FIX]: Push partial text to history so AI remembers what it said before barge-in
                if 'full_ai_text' in locals() and full_ai_text.strip():
                    logger.info(f"AI (Partial before interrupt): {full_ai_text.strip()}")
                    self.session.conversation_history.append({"role": "model", "parts": [full_ai_text.strip()]})
            
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

    def _is_multi_step(self, text: str) -> bool:
        """
        S4-11: Heuristic to detect if a response is a structured multi-step answer.
        Matches admission steps, document checklists, or numbered lists.
        """
        if not text: return False
        
        # 1. Numbering detection (1., 2., Step 1, etc.)
        if re.search(r'(\d+[\.\)\-]\s+|Step\s+\d+)', text, re.IGNORECASE):
            return True
            
        # 2. Keyword detection
        keywords = ["admission process", "document checklist", "following steps", "firstly", "secondly", "finally", "list of items"]
        if any(kw in text.lower() for kw in keywords):
            return True
            
        # 3. Complexity detection (Long responses with multiple sentences often imply structure)
        sentences = re.split(r'[\.\?\!]\s+', text)
        if len(sentences) >= 4:
            return True

        return False

    def _is_echo_transcript(self, transcript: str) -> bool:
        """
        [ECHO-SUPPRESSION] Returns True if the incoming transcript is likely Twilio
        echo of the agent's own voice rather than genuine caller speech.
        Uses word-overlap between the transcript and the agent's active TTS sentence.
        Threshold is config-driven via ECHO_OVERLAP_THRESHOLD (default 0.6).
        Transcripts shorter than 3 words are never suppressed so short genuine
        commands like "stop", "wait", "yes" always get through.
        """
        speaking = self._current_speaking_text
        if not speaking or not transcript:
            return False
        speaking_words = set(speaking.lower().split())
        transcript_words = transcript.lower().split()
        if len(transcript_words) < 3:
            return False
        overlap = sum(1 for w in transcript_words if w in speaking_words) / len(transcript_words)
        threshold = float(os.getenv("ECHO_OVERLAP_THRESHOLD", "0.6"))
        is_echo = overlap >= threshold
        if is_echo:
            logger.debug(f"[ECHO-SUPPRESSION] overlap={overlap:.2f} >= threshold={threshold} — transcript classified as echo")
        return is_echo

    async def cleanup(self):
        """Final session archival and resource release (Pillar 3)."""
        # GUARD: Prevent double-cleanup (e.g. silence termination + WebSocket disconnect both call this)
        if self._cleanup_done:
            logger.debug("Cleanup already completed for this session. Skipping.")
            return
        self._cleanup_done = True

        sid = self.session.session_id if self.session else getattr(self, "_early_sid", "unknown")
        logger.info(f"Cleanup started for session {sid}.")

        # [STAB-10] Capture duration BEFORE resetting session_start_wall_time
        _call_duration_s = None
        if self.session_start_wall_time is not None:
            _call_duration_s = round(time.time() - self.session_start_wall_time, 1)

        # Reset wrap-up tracking so the orchestrator is clean for the next session
        self.wrapup_triggered = False
        self.session_start_wall_time = None
        
        # STATE: Call End
        try:
            self.state.transition_to(CallState.CALL_END)
        except:
            pass # Swallow errors during cleanup

        # [FIX-5] Signal silence monitor to exit its loop cleanly on call end
        if hasattr(self, 'stop_event'):
            self.stop_event.set()
        
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
                    except (asyncio.CancelledError, Exception):
                        pass

            # Cancel Silence Monitor
            try:
                if self.silence_task and not self.silence_task.done():
                    logger.debug("Cleanup: Cancelling silence monitor")
                    self.silence_task.cancel()
            except Exception as e:
                pass

            try:
                if self.transcriber: 
                    logger.debug("Cleanup: Closing Transcriber")
                    await self.transcriber.close()
            except Exception as e:
                logger.error(f"Cleanup: Transcriber close failed: {e}")
                
            try:
                if self.synthesizer:
                    logger.debug("Cleanup: Closing Synthesizer")
                    await self.synthesizer.close()
            except Exception as e:
                logger.error(f"Cleanup: Synthesizer close failed: {e}")
                
            # [STAB-10] UNIVERSAL CRM TICKET — PRD §10: every call produces a ticket, no exceptions.
            # Previously restricted calls were skipped here; that guard is removed.
            # Restricted calls already create a SECURITY_ALERT ticket; this creates the session summary.
            try:
                reason = (
                    getattr(self.session, "termination_reason", None) or "user_hangup"
                ) if self.session else "no_session"

                # Recording URL: local file path fallback (S3 upload happens separately)
                _recording_url = None
                if self.recorder and getattr(self.recorder, "filename", None):
                    _recording_url = self.recorder.filename  # local path; S3 URL if available

                # Derive callback_required and ticket title from termination reason
                _callback_required = reason in (
                    "silence_termination", "system_failure", "latency_breach", "wrapup_timeout"
                )
                _title_map = {
                    "user_hangup": "AI Call Summary - Normal",
                    "silence_termination": "AI Call Summary - Silence Termination",
                    "system_failure": "AI Call Summary - System Failure",
                    "latency_breach": "AI Call Summary - Latency Breach",
                    "wrapup_timeout": "AI Call Summary - Session Limit Reached",
                    "abandoned_setup": "AI Call Summary - No Audio",
                    "no_session": "AI Call Summary - Connection Failed",
                }
                _ticket_title = _title_map.get(reason, f"AI Call Summary - {reason}")
                _sentiment = "Negative" if _callback_required else "Positive"

                if self.session and self.session.conversation_history:
                    logger.info(f"[STAB-10] Cleanup: Writing universal CRM ticket reason={reason} callback={_callback_required}")
                    history_text = "\n".join(
                        [f"{m['role']}: {m['parts'][0]}" for m in self.session.conversation_history]
                    )
                    if reason == "system_failure":
                        logger.warning(f">>> URGENT: High-priority callback ticket for {sid} — system failure.")
                    ct = getattr(self.session, 'caller_type', 'unknown')
                    await self.crm.create_ticket(
                        transcript=history_text,
                        summary=f"Call Log: {reason} | Type: {ct} | Duration: {_call_duration_s}s",
                        sentiment=_sentiment,
                        call_logger=self.call_logger,
                        call_id=self.session.crm_call_id or sid,
                        title=_ticket_title,
                        structured_turns=self.session.structured_turns,
                        session_obj=self.session,
                        callback_required=_callback_required,
                        status="Completed",
                        duration=_call_duration_s,
                        recording_url=_recording_url,
                        metadata={"termination_reason": reason, "caller_type": ct},
                    )
                elif self.session:
                    logger.info(f"[STAB-10] Cleanup: Early-exit CRM ticket reason={reason}")
                    ct = getattr(self.session, 'caller_type', 'unknown')
                    await self.crm.create_ticket(
                        transcript="[System]: Call ended before user provided audio or during setup.",
                        summary=f"No Conversation — {reason} | Type: {ct}",
                        sentiment="Neutral",
                        call_logger=self.call_logger,
                        call_id=self.session.crm_call_id or sid,
                        title=_ticket_title,
                        structured_turns=self.session.structured_turns,
                        session_obj=self.session,
                        callback_required=False,
                        status="Completed",
                        duration=_call_duration_s,
                        recording_url=_recording_url,
                        metadata={"termination_reason": reason, "caller_type": ct},
                    )
                else:
                    logger.warning(f"[STAB-10] Cleanup: No session object — writing error ticket for {sid}")
                    await self.crm.create_ticket(
                        transcript="[System]: Connection failed before session could be initialized.",
                        summary="System Error - Early Connection Failure",
                        sentiment="Negative",
                        call_logger=self.call_logger,
                        call_id=sid,
                        title="AI Call Summary - Connection Failed",
                        structured_turns=None,
                        session_obj=None,
                        callback_required=True,
                        status="Failed",
                        duration=None,
                        recording_url=None,
                        metadata={"termination_reason": "no_session"},
                    )
            except Exception as crm_ex:
                # [DLQ] CRM unavailable — log to stderr so ticket is not silently lost.
                import sys
                print(f"[DLQ] CRITICAL: CRM create_ticket failed during cleanup for {sid}: {crm_ex}", file=sys.stderr)
                logger.error(f"[DLQ] CRM ticket failed for session {sid}: {crm_ex}", exc_info=True)

            if self.recorder:
                logger.info(">>> CLEANUP: Saving Recording...")
                self.recorder.close()
                
            # 2. Final Log Archival
            if self.call_logger:
                logger.info(f"Cleanup: Generating final summary for {sid}")
                # Use the session's actual termination reason, not a hardcoded value
                termination_reason = "user_hangup"
                if self.session and self.session.termination_reason:
                    termination_reason = self.session.termination_reason
                self.call_logger.generate_summary_line(status="completed", reason=termination_reason)
                self.call_logger.save_log(status="completed", session_obj=self.session)
                
            # 3. End and remove session from manager (Pillar 2)
            if self.session:
                # [P5-03]: Guaranteed Persistence - Final save before removal
                self.session_manager.save_session(self.session)
                self.session_manager.end_session(sid)
            
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
            # speak_immediate_response handles the TTS stream
            await self.speak_immediate_response(msg)
            
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
                    title="System_Latency_Breach",
                    structured_turns=self.session.structured_turns
                )
            
            await self.cleanup()
        except Exception as e:
            logger.error(f"Error handling latency breach: {e}")
            await self.cleanup()

    async def _monitor_silence(self):
        """
        [STAB-04] PRD §6.4-compliant silence monitor — 2-stage, 0.5 s tick.

        Three contexts:
          Post-Response:      10 s → soft prompt (SILENCE_1); 20 s total → graceful termination.
          Post-Clarification: Same 10/20 cadence; stage-1 prompt repeats the clarifying question
                              instead of the generic SILENCE_1 script.
          Mid-Query Grace:    3 s grace period applied when caller exits TRANSCRIBING without
                              a final transcript (e.g. clears throat, stops mid-sentence).

        Tick interval is 0.5 s to stay within PRD's ±1 s tolerance.
        Thresholds are read from config (env-overridable: SILENCE_SOFT_PROMPT_S / SILENCE_TERMINATION_S).
        """
        _sid = self.session.session_id if self.session else "unknown"
        if self.call_logger:
            self.call_logger.log_event("stab04", "silence_timer_started", meta={"session_id": _sid})
        logger.info(f"[STAB-04][silence_timer_started] session={_sid}")

        _last_tick_log = 0.0
        _midquery_grace_until = 0.0  # future timestamp: silence counting resumes after this

        try:
            while not self.stop_event.is_set():
                await asyncio.sleep(0.5)

                # ── 1. State Guard ────────────────────────────────────────────────────
                # Don't count silence while AI is actively processing or speaking.
                current_call_state = self.state.get_state()
                if current_call_state in [
                    CallState.SPEAKING,
                    CallState.INTENT_EVAL,
                    CallState.ESCALATION,
                    CallState.RETRIEVAL,
                    CallState.RESPONSE_VALIDATION,
                ]:
                    self.last_interaction_time = time.time()
                    logger.debug(f"[SILENCE-TIMER] Suppressed (state={current_call_state.value}), resetting timer")
                    continue

                # Mid-Query Grace: while transcribing, keep interaction time fresh AND
                # arm a 3 s forward grace window so silence counting doesn't resume the
                # instant the partial stops (e.g. caller pauses mid-sentence).
                if current_call_state == CallState.TRANSCRIBING:
                    self.last_interaction_time = time.time()
                    _midquery_grace_until = time.time() + 3.0
                    continue

                # ── 2. Session Duration / Wrap-up Guard ──────────────────────────────
                elapsed = None
                if self.session_start_wall_time is not None:
                    elapsed = time.time() - self.session_start_wall_time
                elif self.session and getattr(self.session, "start_time", None):
                    try:
                        elapsed = time.time() - self.session.start_time.timestamp()
                    except Exception:
                        elapsed = None

                if elapsed is not None and elapsed >= 300.0 and not self.wrapup_triggered:
                    logger.info(f"Wrap-up prompt triggered at {elapsed:.1f}s.")
                    self.wrapup_triggered = True
                    if self.session:
                        try:
                            await self.crm.create_ticket(
                                transcript="[System]: Session approaching 6-minute limit. Wrap-up prompt played.",
                                summary="Session Wrap-up Triggered",
                                sentiment="Neutral",
                                call_logger=self.call_logger,
                                call_id=self.session.crm_call_id or self.session.session_id,
                                title="Session Wrap-up Triggered"
                            )
                        except Exception as e:
                            logger.error(f"Failed to create Session Wrap-up CRM ticket: {e}")
                    try:
                        await self.speak_immediate_response(PRDScripts.WRAP_UP)
                    except Exception as e:
                        logger.error(f"Error speaking WRAP_UP prompt: {e}")

                if elapsed is not None and elapsed >= 360.0:
                    logger.warning(f"Session duration limit reached ({elapsed:.1f}s). Initiating wrap-up termination.")
                    if self.session:
                        self.session.termination_reason = "wrapup_timeout"
                    try:
                        await self.speak_immediate_response(PRDScripts.WRAP_UP_TERMINATION)
                    except Exception as e:
                        logger.error(f"Error speaking WRAP_UP_TERMINATION prompt: {e}")
                    try:
                        self.state.transition_to(CallState.CALL_END)
                    except Exception:
                        pass
                    await self.cleanup()
                    break

                # ── 1.5 Auto-Recovery for False Interruptions ─────────────────────────
                if current_call_state == CallState.INTERRUPTED:
                    if time.time() - self.last_interaction_time > 5.0:
                        logger.info("[RECOVERY] False interruption or noise detected. Reverting INTERRUPTED -> LISTENING.")
                        self.state.transition_to(CallState.LISTENING)

                # ── 3. Silence Gap (mid-query grace applied) ─────────────────────────
                # effective_last: whichever is later — true last interaction OR grace window end.
                # silence_gap is clamped to ≥ 0 so negative values (grace still active) are safe.
                effective_last = max(self.last_interaction_time, _midquery_grace_until)
                silence_gap = max(0.0, time.time() - effective_last)

                soft_s = self.config.silence_soft_prompt_s          # PRD default: 10 s
                stage2_s = self.config.silence_termination_s - soft_s  # PRD default: 10 s after prompt

                # Periodic tick log every 5 s
                _now = time.time()
                if _now - _last_tick_log >= 5.0:
                    if self.call_logger:
                        self.call_logger.log_event(
                            "stab04", "silence_timer_tick",
                            meta={
                                "gap_s": round(silence_gap, 2),
                                "stage": self.silence_stage,
                                "state": current_call_state.value,
                                "grace_active": _midquery_grace_until > _now,
                            }
                        )
                    logger.info(
                        f"[STAB-04][silence_timer_tick] gap={silence_gap:.1f}s "
                        f"stage={self.silence_stage} state={current_call_state.value} "
                        f"grace={'yes' if _midquery_grace_until > _now else 'no'}"
                    )
                    _last_tick_log = _now

                # ── Stage 0 → 1: Soft prompt at soft_s (PRD: 10 s) ──────────────────
                if silence_gap > soft_s and self.silence_stage == 0:
                    if self.call_logger:
                        self.call_logger.log_event(
                            "stab04", "soft_prompt_fired",
                            meta={
                                "gap_s": round(silence_gap, 2),
                                "was_question": self.last_response_was_question,
                                "state": current_call_state.value,
                            }
                        )
                    logger.info(
                        f"[STAB-04][soft_prompt_fired] gap={silence_gap:.1f}s "
                        f"was_question={self.last_response_was_question} state={current_call_state.value}"
                    )
                    self.silence_stage = 1
                    self.last_interaction_time = time.time()  # Reset timer for stage 2
                    # Post-clarification path: repeat the question so caller has full context.
                    # Generic path: standard "Are you still there?" prompt.
                    if self.last_response_was_question and self._last_ai_question_text:
                        prompt = self._last_ai_question_text
                    else:
                        prompt = PRDScripts.SILENCE_1
                    await self.speak_immediate_response(prompt)

                # ── Stage 1 → Termination at stage2_s after the prompt (PRD: 20 s total) ─
                elif silence_gap > stage2_s and self.silence_stage == 1:
                    if self.call_logger:
                        self.call_logger.log_event(
                            "stab04", "graceful_termination",
                            meta={
                                "gap_s": round(silence_gap, 2),
                                "state": current_call_state.value,
                            }
                        )
                    logger.warning(
                        f"[STAB-04][graceful_termination] gap={silence_gap:.1f}s state={current_call_state.value}"
                    )
                    await self._trigger_silence_termination()
                    break

        except asyncio.CancelledError:
            logger.info("[SILENCE-TIMER] Monitor cancelled")
        except Exception as e:
            logger.error(f"[SILENCE-TIMER] Error in monitor: {e}", exc_info=True)
        finally:
            if self.call_logger:
                self.call_logger.log_event("stab04", "silence_timer_stopped", meta={"session_id": _sid})
            logger.info(f"[STAB-04][silence_timer_stopped] session={_sid}")

    async def _trigger_silence_termination(self):
        """
        [STAB-04] PRD §6.4 graceful termination.
        1. CRM callback ticket if any turns were left interrupted/abandoned.
        2. Preemptive cleanup (kills hanging Brain/RAG tasks).
        3. Speak SILENCE_TERMINATION farewell.
        """
        self.silence_stage = 3  # Prevent re-entry
        if self.session:
            self.session.termination_reason = "silence_termination"

        # [STAB-04] CRM callback — only when the caller was mid-conversation (incomplete turns).
        # Fire-and-forget via _create_task_with_log so cleanup is not blocked.
        if self.session and self.crm:
            incomplete_turns = [
                t for t in getattr(self.session, "structured_turns", [])
                if getattr(t, "agent_response_status", None) in ("interrupted", "abandoned")
            ]
            if incomplete_turns:
                try:
                    self._create_task_with_log(self.crm.create_ticket(
                        transcript="[System]: Call ended due to silence. One or more turns were left incomplete.",
                        summary="Silence Termination — Incomplete Turns",
                        sentiment="Negative",
                        call_logger=self.call_logger,
                        call_id=self.session.crm_call_id or self.session.session_id,
                        title="Silence_Termination_Incomplete",
                        structured_turns=self.session.structured_turns,
                        session_obj=self.session,
                    ))
                    logger.info(
                        f"[STAB-04] CRM silence callback fired — {len(incomplete_turns)} incomplete turn(s)"
                    )
                except Exception as e:
                    logger.error(f"[STAB-04] CRM silence callback failed: {e}")

        # Preemptive Cleanup: Kills hanging Brain/RAG tasks immediately
        await self.cleanup()

        # Speak final termination while socket is in CALL_END state but still open
        try:
            self.state.transition_to(CallState.CALL_END)
        except Exception:
            pass

        goodbye = PRDScripts.SILENCE_TERMINATION
        await self.speak_immediate_response(goodbye)

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
                title="Policy Termination: Language",
                structured_turns=self.session.structured_turns if self.session else None
            ))

            # 2. Speak Final Goodbye (Awaited to ensure audio transmits fully before closure)
            await self.speak_immediate_response(refusal_text, trace_id=trace_id)  # [FIX-3] speak_refusal() was renamed; was NameError on Strike 3
            
            # 3. Transition State & Cleanup AFTER audio is sent
            self.state.transition_to(CallState.CALL_END, trace_id=trace_id)
            
            # 4. Final Cleanup (will NOT self-cancel this task due to _language_termination_active guard)
            await self.cleanup()
        except Exception as e:
            logger.error(f"[GOVERNANCE] Error in language termination flow: {e}")
            await self.cleanup()
        finally:
            self._language_termination_active = False
