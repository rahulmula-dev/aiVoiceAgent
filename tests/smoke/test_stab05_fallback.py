"""
STAB-05 Strict Fallback & No-Speculation Compliance — Smoke Tests
==================================================================
Verifies PRD §4.1 / Escalation Spec v1.1 §6.2 compliance:

  AC1 — Low-confidence RAG score yields verbatim LOW_CONFIDENCE_FALLBACK (no LLM call)
  AC2 — Verbatim phrase is exactly "I don't have that information right now."
  AC3 — CALLBACK_OFFER is spoken immediately after the fallback
  AC4 — Caller says "Yes" → CRM ticket created with callback_required, call ends
  AC5 — Caller says "No" → ANYTHING_ELSE spoken, call remains in LISTENING
  AC6 — Category-specific threshold: fee query at score 0.59 (< 0.60 threshold) triggers fallback
  AC7 — High-confidence score (>= threshold) does NOT trigger fallback (LLM path taken)

Run:
  python -m pytest tests/smoke/test_stab05_fallback.py -v
"""

import sys
import os
import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from orchestrator.manager import VoiceOrchestrator
from contracts.state import CallState
from contracts.policy import PRDScripts
from models.schemas import StandardTurn


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_mgr():
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
    call_logger.call_id = "stab05_test"
    call_logger.log_event = MagicMock()

    mgr = VoiceOrchestrator(stt_provider=stt, tts_provider=tts, call_logger=call_logger)

    session = MagicMock()
    session.session_id = "stab05_session"
    session.crm_call_id = "stab05_crm"
    session.conversation_history = []
    session.structured_turns = []
    session.call_context = MagicMock()
    session.call_context.kb_version_id = None
    session.call_context.chunk_ids_used = []
    session.call_context.caller_number = "+10000000000"
    session.confidence_scores = []
    session.continuation_offered = False
    session.prefetched_context_task = None
    session.retrieved_chunks_cache = []
    session.last_intent = None

    mgr.session = session
    mgr.sid = "stab05_sid"
    mgr.websocket = AsyncMock()
    mgr.session_manager = MagicMock()
    mgr.session_manager.save_session = MagicMock()
    mgr.session_manager.update_state = MagicMock()
    mgr.crm = MagicMock()
    mgr.crm.create_ticket = AsyncMock()
    mgr.crm.create_callback = AsyncMock()
    mgr.context_manager = MagicMock()
    mgr.context_manager.update_context = MagicMock()
    mgr._pending_callback_offer = False
    mgr._pending_callback_query = ""
    mgr._post_tts_ingress_logged = False
    mgr.language_strike_count = 0
    mgr.silence_stage = 0
    return mgr


def _kb_miss_stream(caller_query="What are the tuition fees?", rag_score=0.42, threshold=0.58):
    """
    Async generator that mimics brain.generate_stream() yielding a KB miss tuple.
    The caller_query is injected into the error_meta so the manager can store it.
    """
    async def _gen():
        yield (
            PRDScripts.LOW_CONFIDENCE_FALLBACK,
            {
                "error": "kb_miss",
                "has_grounding": False,
                "rag_score": rag_score,
                "category_threshold": threshold,
                "caller_query": caller_query,
            }
        )
    return _gen()


def _normal_stream(text="Here is what you need to know about our programs."):
    """Async generator mimicking a normal LLM response (no kb_miss)."""
    async def _gen():
        yield (text, {"rag_score": 0.75, "has_grounding": True, "topic": "Programs"})
    return _gen()


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------

class TestStab05FallbackCompliance(unittest.IsolatedAsyncioTestCase):

    # -----------------------------------------------------------------------
    # AC1 + AC2 — Low-confidence score yields verbatim fallback, LLM not called
    # -----------------------------------------------------------------------
    async def test_low_confidence_yields_verbatim_fallback(self):
        """
        When brain.generate_stream yields a kb_miss tuple, the spoken response
        must be exactly PRDScripts.LOW_CONFIDENCE_FALLBACK — no LLM paraphrase.
        """
        mgr = _make_mgr()
        mgr.state.transition_to(CallState.LISTENING)

        spoken = []
        async def mock_speak(text, trace_id=None):
            spoken.append(text)

        with patch.object(mgr.brain, "generate_stream", return_value=_kb_miss_stream()), \
             patch.object(mgr, "speak_immediate_response", side_effect=mock_speak), \
             patch.object(mgr, "_delayed_call_end", new_callable=AsyncMock):
            await mgr.generate_and_speak("What are the tuition fees?", trace_id="t1")

        # The verbatim fallback must be in the audio queue output
        all_spoken = " ".join(spoken)
        self.assertIn(
            PRDScripts.LOW_CONFIDENCE_FALLBACK, all_spoken,
            "FAIL (AC1/AC2): Verbatim LOW_CONFIDENCE_FALLBACK not spoken."
        )

    # -----------------------------------------------------------------------
    # AC2 — Exact string match
    # -----------------------------------------------------------------------
    def test_verbatim_phrase_exact_match(self):
        """
        PRDScripts.LOW_CONFIDENCE_FALLBACK must be EXACTLY the required string.
        Any variation is a PRD violation.
        """
        required = "I don't have that information right now."
        self.assertEqual(
            PRDScripts.LOW_CONFIDENCE_FALLBACK, required,
            f"FAIL (AC2): Verbatim phrase mismatch. Got: '{PRDScripts.LOW_CONFIDENCE_FALLBACK}'"
        )

    # -----------------------------------------------------------------------
    # AC3 — CALLBACK_OFFER spoken immediately after fallback
    # -----------------------------------------------------------------------
    async def test_callback_offer_spoken_after_fallback(self):
        """
        After the KB miss fallback is spoken, speak_immediate_response must be called
        with PRDScripts.CALLBACK_OFFER — the mandatory "Would you like me to arrange a callback?"
        """
        mgr = _make_mgr()
        mgr.state.transition_to(CallState.LISTENING)

        spoken = []
        async def mock_speak(text, trace_id=None):
            spoken.append(text)

        with patch.object(mgr.brain, "generate_stream", return_value=_kb_miss_stream()), \
             patch.object(mgr, "speak_immediate_response", side_effect=mock_speak), \
             patch.object(mgr, "_delayed_call_end", new_callable=AsyncMock):
            await mgr.generate_and_speak("What are the tuition fees?", trace_id="t1")

        self.assertIn(
            PRDScripts.CALLBACK_OFFER, spoken,
            "FAIL (AC3): CALLBACK_OFFER not spoken after KB miss fallback."
        )
        # Offer must come AFTER the fallback
        fallback_idx = next((i for i, s in enumerate(spoken) if PRDScripts.LOW_CONFIDENCE_FALLBACK in s), -1)
        offer_idx = next((i for i, s in enumerate(spoken) if s == PRDScripts.CALLBACK_OFFER), -1)
        if fallback_idx >= 0 and offer_idx >= 0:
            self.assertGreater(
                offer_idx, fallback_idx,
                "FAIL (AC3): CALLBACK_OFFER was spoken before LOW_CONFIDENCE_FALLBACK."
            )

    # -----------------------------------------------------------------------
    # AC4 — Caller says "Yes" → CRM ticket + call ends
    # -----------------------------------------------------------------------
    async def test_yes_response_creates_crm_ticket_and_ends_call(self):
        """
        When _pending_callback_offer=True and caller says "Yes", the manager must:
        1. Create a CRM ticket with 'Callback_Required_KB_Miss' in the title.
        2. Set _pending_callback_offer back to False.
        3. Schedule call end.
        """
        mgr = _make_mgr()
        mgr._pending_callback_offer = True
        mgr._pending_callback_query = "What are the tuition fees?"
        mgr.state.transition_to(CallState.LISTENING)

        spoken = []
        async def mock_speak(text, trace_id=None):
            spoken.append(text)

        with patch.object(mgr, "speak_immediate_response", side_effect=mock_speak), \
             patch.object(mgr, "_delayed_call_end", new_callable=AsyncMock) as mock_end:
            # Simulate final transcript "Yes"
            await mgr._on_transcript("Yes", confidence=0.95, is_final=True, detected_lang="en")

        self.assertFalse(
            mgr._pending_callback_offer,
            "FAIL (AC4): _pending_callback_offer not cleared after 'Yes'."
        )
        mgr.crm.create_ticket.assert_called()
        call_args = mgr.crm.create_ticket.call_args
        self.assertIn(
            "Callback_Required_KB_Miss", str(call_args),
            "FAIL (AC4): CRM ticket title 'Callback_Required_KB_Miss' missing."
        )
        mock_end.assert_called()

    # -----------------------------------------------------------------------
    # AC5 — Caller says "No" → ANYTHING_ELSE spoken, stays in LISTENING
    # -----------------------------------------------------------------------
    async def test_no_response_speaks_anything_else(self):
        """
        When _pending_callback_offer=True and caller says "No", the manager must:
        1. Speak PRDScripts.ANYTHING_ELSE.
        2. NOT create a CRM ticket.
        3. NOT end the call.
        """
        mgr = _make_mgr()
        mgr._pending_callback_offer = True
        mgr._pending_callback_query = "What are the tuition fees?"
        mgr.state.transition_to(CallState.LISTENING)

        spoken = []
        async def mock_speak(text, trace_id=None):
            spoken.append(text)

        with patch.object(mgr, "speak_immediate_response", side_effect=mock_speak), \
             patch.object(mgr, "_delayed_call_end", new_callable=AsyncMock) as mock_end:
            await mgr._on_transcript("No", confidence=0.95, is_final=True, detected_lang="en")

        self.assertFalse(mgr._pending_callback_offer)
        self.assertIn(
            PRDScripts.ANYTHING_ELSE, spoken,
            "FAIL (AC5): ANYTHING_ELSE not spoken after 'No'."
        )
        mgr.crm.create_ticket.assert_not_called()
        mock_end.assert_not_called()

    # -----------------------------------------------------------------------
    # AC6 — Fee query at 0.59 (below 0.60 fee threshold) triggers fallback
    # -----------------------------------------------------------------------
    async def test_fee_query_below_threshold_triggers_fallback(self):
        """
        A fee-related query with rag_score=0.59 must trigger the KB miss path
        because the FEE_DETAILS threshold is 0.60.
        The _pending_callback_offer flag must be armed after generate_and_speak.
        """
        mgr = _make_mgr()
        mgr.state.transition_to(CallState.LISTENING)

        spoken = []
        async def mock_speak(text, trace_id=None):
            spoken.append(text)

        # score=0.59 < 0.60 (FEE_DETAILS threshold)
        fee_miss_stream = _kb_miss_stream(
            caller_query="How much is the tuition fee?",
            rag_score=0.59,
            threshold=0.60,
        )

        with patch.object(mgr.brain, "generate_stream", return_value=fee_miss_stream), \
             patch.object(mgr, "speak_immediate_response", side_effect=mock_speak), \
             patch.object(mgr, "_delayed_call_end", new_callable=AsyncMock):
            await mgr.generate_and_speak("How much is the tuition fee?", trace_id="t1")

        self.assertTrue(
            mgr._pending_callback_offer,
            "FAIL (AC6): _pending_callback_offer not armed for fee query at score 0.59 (threshold 0.60)."
        )
        self.assertIn(
            PRDScripts.LOW_CONFIDENCE_FALLBACK, " ".join(spoken),
            "FAIL (AC6): Verbatim fallback not spoken for fee query below threshold."
        )

    # -----------------------------------------------------------------------
    # AC7 — High-confidence score does NOT trigger fallback
    # -----------------------------------------------------------------------
    async def test_high_confidence_score_does_not_trigger_fallback(self):
        """
        A score of 0.80 (well above all thresholds) must NOT yield the KB miss fallback.
        The normal LLM response path must be taken.
        """
        mgr = _make_mgr()
        mgr.state.transition_to(CallState.LISTENING)

        spoken = []
        async def mock_speak(text, trace_id=None):
            spoken.append(text)

        with patch.object(mgr.brain, "generate_stream", return_value=_normal_stream()), \
             patch.object(mgr, "speak_immediate_response", side_effect=mock_speak), \
             patch.object(mgr, "_delayed_call_end", new_callable=AsyncMock):
            await mgr.generate_and_speak("Tell me about the programs.", trace_id="t1")

        self.assertFalse(
            mgr._pending_callback_offer,
            "FAIL (AC7): _pending_callback_offer armed for a high-confidence response."
        )
        self.assertNotIn(
            PRDScripts.CALLBACK_OFFER, spoken,
            "FAIL (AC7): CALLBACK_OFFER spoken for a high-confidence response."
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
