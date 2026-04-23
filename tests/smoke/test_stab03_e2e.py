"""
STAB-03 End-to-End Verification — Intent-Based Barge-In
========================================================
Simulates:
  Turn 1: AI delivers a multi-step admission response (is_multi_step=True)
  Turn 2: User barge-in classified as SAME_TOPIC
  Turn 3: User barge-in classified as NEW_TOPIC (triggers continuation offer, once only)
  Turn 4: Second NEW_TOPIC barge-in (continuation offer must NOT fire again)

Acceptance Criteria verified:
  AC1 — SAME_TOPIC barge-in: response does NOT contain continuation offer
  AC2 — NEW_TOPIC barge-in on multi-step turn: PRDScripts.CONTINUATION_OFFERED appended exactly once
  AC3 — Second NEW_TOPIC on same session: continuation offer NOT repeated (continuation_offered gate)
  AC4 — non-multi-step turn interrupted as NEW_TOPIC: NO offer appended
  AC5 — NEW_TOPIC clears stale RAG: prefetched_context_task and retrieved_chunks_cache reset to empty

Run:
  python -m pytest tests/smoke/test_stab03_e2e.py -v
"""

import sys
import os
import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from orchestrator.manager import VoiceOrchestrator
from contracts.state import CallState
from contracts.policy import PRDScripts
from contracts.language_interceptor import InterceptResult
from models.schemas import StandardTurn, BargeInTurn


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

def _english_intercept():
    r = MagicMock(spec=InterceptResult)
    r.proceed_to_llm = True
    r.terminate_call = False
    r.strike = 0
    r.detection_method = "fast_path"
    r.lang_code = "en"
    r.confidence = 1.0
    return r


def _make_mgr():
    stt = MagicMock()
    stt.connect = AsyncMock(return_value=True)
    stt.send_audio = AsyncMock()
    stt.close = AsyncMock()
    stt.set_callback = MagicMock()
    stt.set_listener_error_callback = MagicMock()

    tts = MagicMock()
    tts.speak = AsyncMock()
    tts.stop_current_speech = MagicMock(return_value="step 3 was...")
    tts.close = AsyncMock()

    call_logger = MagicMock()
    call_logger.call_id = "stab03_e2e"
    call_logger.log_event = MagicMock()

    mgr = VoiceOrchestrator(stt_provider=stt, tts_provider=tts, call_logger=call_logger)

    session = MagicMock()
    session.session_id = "stab03_session"
    session.crm_call_id = "stab03_crm"
    session.current_speaking_turn_id = 1
    session.conversation_history = []
    session.structured_turns = []
    session.continuation_offered = False
    session.retrieved_chunks_cache = []
    session.last_intent = None
    session.prefetched_context_task = None
    session.call_context = MagicMock()
    session.call_context.program_interest = None
    session.call_context.intake = None
    session.call_context.user_name = "Akansha"
    session.call_context.study_mode = None
    session.call_context.campus = None
    session.call_context.kb_version_id = None
    session.call_context.chunk_ids_used = []
    session.confidence_scores = []
    session.interruption_snapshot = None

    mgr.session = session
    mgr.sid = "stab03_sid"
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


def _add_multi_step_turn(mgr, turn_id=1, status="interrupted"):
    """Add a completed multi-step StandardTurn to session — simulates Turn 1 having completed."""
    turn = StandardTurn(
        turn_id=turn_id,
        caller_input="Can you walk me through the admission process?",
        topic="Admissions",
        agent_response_status=status,
        agent_partial_response="Step 3 was...",
        is_multi_step=True,
        continuation_offered=False,
    )
    mgr.session.structured_turns.append(turn)
    mgr.session.current_speaking_turn_id = turn_id
    return turn


def _add_non_multi_step_turn(mgr, turn_id=1, status="interrupted"):
    turn = StandardTurn(
        turn_id=turn_id,
        caller_input="What is the campus address?",
        topic="Campus",
        agent_response_status=status,
        is_multi_step=False,
        continuation_offered=False,
    )
    mgr.session.structured_turns.append(turn)
    mgr.session.current_speaking_turn_id = turn_id
    return turn


async def _run_barge_in(mgr, caller_input, classification, is_multi_step=False):
    """
    Drive handle_barge_in() with a mocked brain that returns a controlled classification.
    Returns the spoken response string.
    """
    spoken = []

    async def mock_speak_immediate(text, trace_id=None):
        spoken.append(text)

    brain_result = (classification, "Here is the answer to your question.", is_multi_step, "Test Topic", "unknown", [])

    mgr.state.transition_to(CallState.LISTENING)
    mgr.state.transition_to(CallState.TRANSCRIBING)
    mgr.state.transition_to(CallState.INTERRUPTED)

    with patch.object(mgr.brain, 'generate_with_classification', return_value=brain_result), \
         patch.object(mgr.brain.kb, 'search', new_callable=AsyncMock, return_value=("context", 0.8, "Topic", "kb_v1", ["c1"])), \
         patch.object(mgr, 'speak_immediate_response', side_effect=mock_speak_immediate):
        await mgr.handle_barge_in(mgr.sid, caller_input, trace_id="test_trace")

    return spoken[0] if spoken else ""


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------

class TestStab03IntentBargeIn(unittest.IsolatedAsyncioTestCase):

    # -----------------------------------------------------------------------
    # AC1 — SAME_TOPIC barge-in on multi-step turn: NO continuation offer
    # -----------------------------------------------------------------------
    async def test_same_topic_no_continuation_offer(self):
        """
        Category B (SAME_TOPIC): User asks clarification on the multi-step admission process.
        The continuation offer must NOT be appended — it is only for NEW_TOPIC.
        """
        mgr = _make_mgr()
        _add_multi_step_turn(mgr, turn_id=1)

        response = await _run_barge_in(mgr, "Can you repeat step 2?", "SAME_TOPIC")

        self.assertNotIn(
            PRDScripts.CONTINUATION_OFFERED, response,
            "FAIL (AC1): Continuation offer was appended to a SAME_TOPIC response."
        )
        # Offer gate must remain False — no offer was needed
        self.assertFalse(
            mgr.session.continuation_offered,
            "FAIL: continuation_offered flag was set for a SAME_TOPIC barge-in."
        )

    # -----------------------------------------------------------------------
    # AC2 — NEW_TOPIC barge-in on multi-step turn: offer appended exactly once
    # -----------------------------------------------------------------------
    async def test_new_topic_multi_step_appends_offer_once(self):
        """
        Category A (NEW_TOPIC) after a multi-step interrupted turn:
        PRDScripts.CONTINUATION_OFFERED must be appended to the response.
        session.continuation_offered must be set True.
        interrupted turn must be marked 'abandoned'.
        new_turn.continuation_offered must be True.
        """
        mgr = _make_mgr()
        prev_turn = _add_multi_step_turn(mgr, turn_id=1)

        response = await _run_barge_in(mgr, "What are the tuition fees?", "NEW_TOPIC")

        # Offer appended
        self.assertIn(
            PRDScripts.CONTINUATION_OFFERED, response,
            "FAIL (AC2): Continuation offer was NOT appended to NEW_TOPIC response on multi-step turn."
        )
        # Session gate set
        self.assertTrue(
            mgr.session.continuation_offered,
            "FAIL (AC2): session.continuation_offered was not set True."
        )
        # Previous turn marked abandoned
        self.assertEqual(
            prev_turn.agent_response_status, "abandoned",
            "FAIL (AC2): Interrupted multi-step turn was not marked 'abandoned'."
        )
        # New turn continuation_offered flag matches what was appended
        new_turn = mgr.session.structured_turns[-1]
        self.assertTrue(
            new_turn.continuation_offered,
            "FAIL (AC2): new_turn.continuation_offered not set True after offer was appended."
        )

    # -----------------------------------------------------------------------
    # AC3 — Second NEW_TOPIC barge-in: offer NOT repeated (once-only gate)
    # -----------------------------------------------------------------------
    async def test_new_topic_offer_not_repeated_second_time(self):
        """
        After the offer fires once, session.continuation_offered = True.
        A subsequent NEW_TOPIC barge-in must NOT append the offer again.
        """
        mgr = _make_mgr()
        _add_multi_step_turn(mgr, turn_id=1)

        # First barge-in fires the offer
        await _run_barge_in(mgr, "What are tuition fees?", "NEW_TOPIC")
        self.assertTrue(mgr.session.continuation_offered)

        # Add a second multi-step turn to give the offer another chance to fire
        _add_multi_step_turn(mgr, turn_id=2)
        response_2 = await _run_barge_in(mgr, "Tell me about scholarships.", "NEW_TOPIC")

        self.assertNotIn(
            PRDScripts.CONTINUATION_OFFERED, response_2,
            "FAIL (AC3): Continuation offer was repeated on second NEW_TOPIC barge-in (once-only gate broken)."
        )

    # -----------------------------------------------------------------------
    # AC4 — NEW_TOPIC barge-in on NON-multi-step turn: NO offer
    # -----------------------------------------------------------------------
    async def test_new_topic_non_multi_step_no_offer(self):
        """
        If the interrupted turn was a simple 1-sentence answer (is_multi_step=False),
        the continuation offer must NOT be appended even if classification == NEW_TOPIC.
        """
        mgr = _make_mgr()
        _add_non_multi_step_turn(mgr, turn_id=1)

        response = await _run_barge_in(mgr, "What are tuition fees?", "NEW_TOPIC")

        self.assertNotIn(
            PRDScripts.CONTINUATION_OFFERED, response,
            "FAIL (AC4): Continuation offer was appended for a non-multi-step interrupted turn."
        )
        self.assertFalse(mgr.session.continuation_offered)

    # -----------------------------------------------------------------------
    # AC5 — NEW_TOPIC clears stale RAG context
    # -----------------------------------------------------------------------
    async def test_new_topic_clears_stale_rag_context(self):
        """
        On NEW_TOPIC barge-in, the previous turn's RAG context must be cleared:
          - prefetched_context_task → None
          - retrieved_chunks_cache → []
          - last_intent → None
        """
        mgr = _make_mgr()
        _add_multi_step_turn(mgr, turn_id=1)

        # Seed stale RAG context from Turn 1
        fake_task = asyncio.create_task(asyncio.sleep(100))
        mgr.session.prefetched_context_task = fake_task
        mgr.session.retrieved_chunks_cache = [{"chunk": "old_data"}]
        mgr.session.last_intent = "ADMISSION_PROCESS"

        await _run_barge_in(mgr, "What are tuition fees?", "NEW_TOPIC")

        self.assertIsNone(
            mgr.session.prefetched_context_task,
            "FAIL (AC5): prefetched_context_task not cleared on NEW_TOPIC barge-in."
        )
        self.assertEqual(
            mgr.session.retrieved_chunks_cache, [],
            "FAIL (AC5): retrieved_chunks_cache not cleared on NEW_TOPIC barge-in."
        )
        self.assertIsNone(
            mgr.session.last_intent,
            "FAIL (AC5): last_intent not cleared on NEW_TOPIC barge-in."
        )

        # Cancel the background task we created
        fake_task.cancel()
        try:
            await fake_task
        except (asyncio.CancelledError, Exception):
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
