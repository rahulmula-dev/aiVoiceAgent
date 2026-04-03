"""
STAB-02 Smoke Test — INTENT_EVAL Race Condition (Pattern 3, Confirmed Variant)
===============================================================================
Root cause confirmed from call a6317849:
  A Deepgram continuation final arrives while the orchestrator is in INTENT_EVAL
  (LLM generating, agent NOT speaking). The elif at manager.py:811 matched solely
  on `current_state == TRANSCRIBING`, routed to handle_barge_in() → INTERRUPTED,
  and caused 5–6 s of active silence.

Fix (manager.py:811):
  BEFORE: elif current_state == CallState.TRANSCRIBING:
  AFTER:  elif current_state == CallState.TRANSCRIBING and
                  pre_transition_state in [CallState.SPEAKING, CallState.INTERRUPTED]:

Test structure:
  - test_intent_eval_race_does_not_cause_interrupted  → FAILS without fix, PASSES with fix
  - test_genuine_late_barge_in_still_routes_correctly → Regression guard for the original
                                                        SPEAKING→TRANSCRIBING race path

Run:
  python -m pytest tests/smoke/test_two_turn_stab02.py -v
"""

import sys
import os
import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from orchestrator.manager import VoiceOrchestrator
from contracts.state import CallState
from contracts.language_interceptor import InterceptResult


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_english_intercept_result():
    """InterceptResult that passes all language gates (English, no strike)."""
    r = MagicMock(spec=InterceptResult)
    r.proceed_to_llm = True
    r.terminate_call = False
    r.strike = 0
    r.detection_method = "fast_path"
    r.lang_code = "en"
    r.confidence = 1.0
    return r


def _make_orchestrator():
    """
    Minimal VoiceOrchestrator with all I/O mocked out.
    Returns the manager ready for direct _on_transcript() calls.
    """
    stt = MagicMock()
    stt.connect = AsyncMock(return_value=True)
    stt.send_audio = AsyncMock()
    stt.close = AsyncMock()
    stt.set_callback = MagicMock()
    stt.set_listener_error_callback = MagicMock()

    tts = MagicMock()
    tts.speak = AsyncMock()
    tts.stop_current_speech = MagicMock(return_value="")
    tts.close = AsyncMock()

    call_logger = MagicMock()
    call_logger.call_id = "stab02_smoke"
    call_logger.log_event = MagicMock()

    mgr = VoiceOrchestrator(
        stt_provider=stt,
        tts_provider=tts,
        call_logger=call_logger
    )

    # Minimal session
    session = MagicMock()
    session.session_id = "stab02_session"
    session.crm_call_id = "stab02_crm"
    session.current_speaking_turn_id = 1
    session.conversation_history = []
    session.structured_turns = []
    session.language_warning_count = 0
    session.call_context = MagicMock()
    session.call_context.program_interest = None
    session.call_context.intake = None
    session.call_context.user_name = None
    session.call_context.study_mode = None
    session.call_context.campus = None
    session.prefetched_context_task = None
    mgr.session = session

    mgr.sid = "stab02_sid"
    mgr.websocket = AsyncMock()
    mgr.session_manager = MagicMock()
    mgr.session_manager.save_session = MagicMock()
    mgr.crm = MagicMock()
    mgr.crm.create_ticket = AsyncMock()
    mgr.context_manager = MagicMock()
    mgr.context_manager.update_context = MagicMock()
    mgr._post_tts_ingress_logged = False
    mgr.language_strike_count = 0

    return mgr


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------

class TestStab02IntentEvalRace(unittest.IsolatedAsyncioTestCase):

    # -----------------------------------------------------------------------
    # PRIMARY REGRESSION TEST — This is the confirmed failure path
    # -----------------------------------------------------------------------
    async def test_intent_eval_race_does_not_cause_interrupted(self):
        """
        STAB-02 PRIMARY REGRESSION TEST.

        Reproduces the exact failure from call a6317849:
          Turn N transcript is in INTENT_EVAL (LLM generating, agent NOT speaking).
          Turn N+1 transcript arrives — Deepgram continuation final.

        WITHOUT the fix (manager.py:811 original condition):
          pre_transition_state = INTENT_EVAL
          current_state        = TRANSCRIBING   ← elif matches here
          → handle_barge_in() called
          → state transitions to INTERRUPTED
          → 5-6 s silence (silence monitor 5 s false-interruption recovery)

        WITH the fix:
          elif condition requires pre_transition_state in [SPEAKING, INTERRUPTED]
          INTENT_EVAL does NOT match → falls to else branch
          → generate_and_speak() called normally
          → no INTERRUPTED, no silence
        """
        mgr = _make_orchestrator()

        # 1. Put agent in INTENT_EVAL — LLM is running for Turn N
        mgr.state.transition_to(CallState.LISTENING)
        mgr.state.transition_to(CallState.TRANSCRIBING)
        mgr.state.transition_to(CallState.INTENT_EVAL)
        self.assertEqual(mgr.state.get_state(), CallState.INTENT_EVAL)

        # 2. Simulate a live response_task (still running — LLM not done yet)
        mgr.response_task = asyncio.create_task(asyncio.sleep(100))

        generate_calls = []

        async def mock_generate_and_speak(text, **kwargs):
            generate_calls.append(text)

        with patch.object(mgr, 'generate_and_speak', side_effect=mock_generate_and_speak), \
             patch.object(mgr, 'handle_barge_in', new_callable=AsyncMock) as mock_barge_in, \
             patch.object(mgr, '_send_clear_message', new_callable=AsyncMock), \
             patch.object(mgr, '_create_task_with_log', side_effect=lambda coro: asyncio.create_task(coro)), \
             patch('contracts.policy.detect_restricted_topic', return_value=MagicMock(is_restricted=False)), \
             patch.object(mgr._lang_interceptor, 'check', return_value=_make_english_intercept_result()), \
             patch.object(mgr.policy, 'classify_intent', return_value='PROCEED'), \
             patch.object(mgr.policy, 'check_escalation', return_value=False):

            # 3. Turn N+1 continuation final arrives while still in INTENT_EVAL
            #    (pre_transition_state captured inside _on_transcript = INTENT_EVAL)
            await mgr._on_transcript(
                "Can you tell me about the campus location of GD College?",
                confidence=0.97,
                stt_latency=0.008,
                is_final=True,
                detected_lang="en"
            )

            # Allow any spawned tasks to settle
            await asyncio.sleep(0)

        # 4. ASSERTIONS

        # Core: state must NOT be INTERRUPTED
        self.assertNotEqual(
            mgr.state.get_state(),
            CallState.INTERRUPTED,
            "FAIL (STAB-02 REPRODUCED): State is INTERRUPTED after INTENT_EVAL race. "
            "The fix at manager.py:811 is not applied or was reverted."
        )

        # handle_barge_in must NOT have been called (it's the route to INTERRUPTED)
        mock_barge_in.assert_not_called()

        # generate_and_speak MUST have been called with the new text
        self.assertTrue(
            len(generate_calls) >= 1,
            "FAIL: generate_and_speak was never called — Turn N+1 was silently dropped."
        )
        self.assertIn("campus location", generate_calls[0])

        # Clean up background task
        mgr.response_task.cancel()
        try:
            await mgr.response_task
        except (asyncio.CancelledError, Exception):
            pass

    # -----------------------------------------------------------------------
    # REGRESSION GUARD — Original SPEAKING→TRANSCRIBING late barge-in
    # must still work correctly after the fix
    # -----------------------------------------------------------------------
    async def test_genuine_late_barge_in_still_routes_correctly(self):
        """
        Regression guard for the SPEAKING → TRANSCRIBING race (the intended use
        case of the original elif at manager.py:811).

        Agent is in SPEAKING. State races to TRANSCRIBING before the final
        transcript is processed (Dev Phone / no Twilio VAD speech event).
        The fix must still route this to handle_barge_in — NOT silently cancel.
        """
        mgr = _make_orchestrator()

        # 1. Agent is SPEAKING — response_task is live
        mgr.state.transition_to(CallState.LISTENING)
        mgr.state.transition_to(CallState.TRANSCRIBING)
        mgr.state.transition_to(CallState.INTENT_EVAL)
        mgr.state.transition_to(CallState.SPEAKING)
        self.assertEqual(mgr.state.get_state(), CallState.SPEAKING)

        mgr.response_task = asyncio.create_task(asyncio.sleep(100))

        barge_in_calls = []

        async def mock_barge_in(_call_id, caller_input, **kwargs):
            barge_in_calls.append(caller_input)

        with patch.object(mgr, 'handle_barge_in', side_effect=mock_barge_in), \
             patch.object(mgr, 'generate_and_speak', new_callable=AsyncMock) as mock_gas, \
             patch.object(mgr, '_send_clear_message', new_callable=AsyncMock), \
             patch.object(mgr, '_create_task_with_log', side_effect=lambda coro: asyncio.create_task(coro)), \
             patch('contracts.policy.detect_restricted_topic', return_value=MagicMock(is_restricted=False)), \
             patch.object(mgr._lang_interceptor, 'check', return_value=_make_english_intercept_result()), \
             patch.object(mgr.policy, 'classify_intent', return_value='PROCEED'), \
             patch.object(mgr.policy, 'check_escalation', return_value=False):

            # 2. State races to TRANSCRIBING (simulate what Deepgram partial did earlier)
            #    _on_transcript will capture pre_transition_state = SPEAKING at line 605
            #    then transition to TRANSCRIBING at line 607
            await mgr._on_transcript(
                "What are the tuition fees?",
                confidence=0.99,
                stt_latency=0.01,
                is_final=True,
                detected_lang="en"
            )
            await asyncio.sleep(0)

        # handle_barge_in MUST have been called — late barge-in path is still correct
        self.assertTrue(
            len(barge_in_calls) >= 1,
            "REGRESSION: genuine late barge-in (SPEAKING→TRANSCRIBING) no longer routes "
            "to handle_barge_in after the STAB-02 fix. The guard is too broad."
        )

        # generate_and_speak must NOT have been called (barge-in path handles it)
        mock_gas.assert_not_called()

        mgr.response_task.cancel()
        try:
            await mgr.response_task
        except (asyncio.CancelledError, Exception):
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
