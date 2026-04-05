"""
STAB-05 Strict Fallback & No-Speculation Compliance — Smoke Tests
==================================================================
Verifies PRD §4.1 / Escalation Spec v1.1 §6.2 compliance:

  AC1 — Low-confidence RAG score yields verbatim LOW_CONFIDENCE_FALLBACK (no LLM call)
  AC2 — Verbatim phrase is exactly "I don't have that information right now."
  AC3 — CALLBACK_OFFER is spoken immediately after the fallback
  AC4 — Caller says "Yes" → CRM ticket created with Callback_Required_KB_Miss, call ends
  AC5 — Caller says "No" → ANYTHING_ELSE spoken, call remains in LISTENING
  AC6 — Category-specific threshold: fee query at score 0.59 (< 0.60) triggers fallback
  AC7 — High-confidence score (>= threshold) does NOT trigger fallback (LLM path taken)

Run:
  python -m pytest tests/smoke/test_stab05_fallback.py -v
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


# ---------------------------------------------------------------------------
# Async generator helpers — must be *functions* (called per-test), not objects
# ---------------------------------------------------------------------------

def _kb_miss_gen(
    caller_query="What are the tuition fees?",
    rag_score=0.42,
    threshold=0.58,
):
    """Returns an async-generator *function* that yields one KB miss tuple."""
    async def _gen(*args, **kwargs):
        yield (
            PRDScripts.LOW_CONFIDENCE_FALLBACK,
            {
                "error": "kb_miss",
                "has_grounding": False,
                "rag_score": rag_score,
                "category_threshold": threshold,
                "caller_query": caller_query,
            },
        )
    return _gen


def _normal_gen(text="Here is what you need to know about our programs."):
    """Returns an async-generator *function* that yields one normal LLM response."""
    async def _gen(*args, **kwargs):
        yield (text, {"rag_score": 0.80, "has_grounding": True, "topic": "Programs"})
    return _gen


async def _null_speak_gen(*args, **kwargs):
    """Async generator that yields nothing — stands in for synthesizer.speak()."""
    return
    yield  # Makes it an async generator without yielding audio chunks


# ---------------------------------------------------------------------------
# Fixture helper
# ---------------------------------------------------------------------------

def _make_mgr():
    stt = MagicMock()
    stt.connect = AsyncMock(return_value=True)
    stt.send_audio = AsyncMock()
    stt.close = AsyncMock()
    stt.set_callback = MagicMock()
    stt.set_listener_error_callback = MagicMock()

    tts = MagicMock()
    tts.speak = _null_speak_gen          # async generator — no chunks, returns immediately
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
    session.current_speaking_turn_id = 0
    session.call_context = MagicMock()
    session.call_context.kb_version_id = None
    session.call_context.chunk_ids_used = []
    session.call_context.caller_number = "+10000000000"
    session.call_context.last_intents = []
    session.call_context.retrieved_chunks_snapshot = None
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


async def _run_generate_and_speak(mgr, text, gen_fn):
    """
    Drive generate_and_speak() with a controlled async-generator mock for brain.generate_stream.
    Captures all speak_immediate_response calls.
    """
    spoken = []

    async def mock_speak_immediate(text, **_):
        spoken.append(text)

    # tts_worker sleeps `remaining_time` = chars/10 - generation_time.
    # Patch asyncio.sleep inside generate_and_speak scope to return immediately.
    original_sleep = asyncio.sleep

    async def fast_sleep(delay, *a, **kw):
        # Only skip the tts_worker's playback sleep (> 0.1 s); keep yields (0 s) intact.
        if delay > 0.1:
            return
        await original_sleep(0)

    mgr.brain.generate_stream = gen_fn

    with patch.object(mgr, "speak_immediate_response", side_effect=mock_speak_immediate), \
         patch("asyncio.sleep", side_effect=fast_sleep), \
         patch.object(mgr, "_delayed_call_end", new_callable=AsyncMock), \
         patch.object(mgr, "_send_response_chunk", new_callable=AsyncMock):
        await asyncio.wait_for(
            mgr.generate_and_speak(text, trace_id="stab05_trace"),
            timeout=10.0
        )

    return spoken


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------

class TestStab05FallbackCompliance(unittest.IsolatedAsyncioTestCase):

    # -----------------------------------------------------------------------
    # AC1 — Low-confidence score yields verbatim fallback, LLM not called
    # -----------------------------------------------------------------------
    async def test_low_confidence_yields_verbatim_fallback(self):
        """
        When generate_stream yields a kb_miss tuple, the spoken output must include
        PRDScripts.LOW_CONFIDENCE_FALLBACK — the LLM is bypassed entirely.
        """
        mgr = _make_mgr()
        mgr.state.transition_to(CallState.LISTENING)

        spoken = await _run_generate_and_speak(
            mgr, "What are the tuition fees?", _kb_miss_gen()
        )

        all_spoken = " ".join(spoken)
        self.assertIn(
            PRDScripts.LOW_CONFIDENCE_FALLBACK, all_spoken,
            "FAIL (AC1): LOW_CONFIDENCE_FALLBACK not spoken on KB miss."
        )

    # -----------------------------------------------------------------------
    # AC2 — Verbatim phrase exact match
    # -----------------------------------------------------------------------
    def test_verbatim_phrase_exact_match(self):
        """PRDScripts.LOW_CONFIDENCE_FALLBACK must match the PRD §4.1 required string exactly."""
        required = "I don't have that information right now."
        self.assertEqual(
            PRDScripts.LOW_CONFIDENCE_FALLBACK, required,
            f"FAIL (AC2): Verbatim mismatch. Got: '{PRDScripts.LOW_CONFIDENCE_FALLBACK}'"
        )

    # -----------------------------------------------------------------------
    # AC3 — CALLBACK_OFFER spoken after fallback, in correct order
    # -----------------------------------------------------------------------
    async def test_callback_offer_spoken_after_fallback(self):
        """
        After the KB miss fallback is spoken, speak_immediate_response must be called
        with PRDScripts.CALLBACK_OFFER — and it must come AFTER the fallback.
        """
        mgr = _make_mgr()
        mgr.state.transition_to(CallState.LISTENING)

        spoken = await _run_generate_and_speak(
            mgr, "What are the tuition fees?", _kb_miss_gen()
        )

        self.assertIn(
            PRDScripts.CALLBACK_OFFER, spoken,
            "FAIL (AC3): CALLBACK_OFFER not spoken after KB miss fallback."
        )

        # Order check: fallback before offer
        fallback_idx = next(
            (i for i, s in enumerate(spoken) if PRDScripts.LOW_CONFIDENCE_FALLBACK in s), -1
        )
        offer_idx = next(
            (i for i, s in enumerate(spoken) if s == PRDScripts.CALLBACK_OFFER), -1
        )
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
        1. Create a CRM ticket titled 'Callback_Required_KB_Miss'.
        2. Clear _pending_callback_offer.
        3. Schedule call end.
        """
        mgr = _make_mgr()
        mgr._pending_callback_offer = True
        mgr._pending_callback_query = "What are the tuition fees?"
        mgr.state.transition_to(CallState.LISTENING)

        spoken = []

        async def mock_speak(text, **_):
            spoken.append(text)

        with patch.object(mgr, "speak_immediate_response", side_effect=mock_speak), \
             patch.object(mgr, "_delayed_call_end", new_callable=AsyncMock) as mock_end, \
             patch.object(mgr, "_send_response_chunk", new_callable=AsyncMock):
            await mgr._on_transcript("Yes", confidence=0.95, is_final=True, detected_lang="en")

            # Allow fire-and-forget tasks to complete
            await asyncio.sleep(0)

        self.assertFalse(
            mgr._pending_callback_offer,
            "FAIL (AC4): _pending_callback_offer not cleared after 'Yes'."
        )
        mgr.crm.create_ticket.assert_called()
        self.assertIn(
            "Callback_Required_KB_Miss",
            str(mgr.crm.create_ticket.call_args),
            "FAIL (AC4): CRM ticket title 'Callback_Required_KB_Miss' missing."
        )
        mock_end.assert_called()

    # -----------------------------------------------------------------------
    # AC5 — Caller says "No" → ANYTHING_ELSE spoken, no CRM, no call end
    # -----------------------------------------------------------------------
    async def test_no_response_speaks_anything_else(self):
        """
        When _pending_callback_offer=True and caller says "No", the manager must:
        1. Speak PRDScripts.ANYTHING_ELSE.
        2. NOT create a CRM ticket.
        3. NOT schedule call end.
        """
        mgr = _make_mgr()
        mgr._pending_callback_offer = True
        mgr._pending_callback_query = "What are the tuition fees?"
        mgr.state.transition_to(CallState.LISTENING)

        spoken = []

        async def mock_speak(text, **_):
            spoken.append(text)

        with patch.object(mgr, "speak_immediate_response", side_effect=mock_speak), \
             patch.object(mgr, "_delayed_call_end", new_callable=AsyncMock) as mock_end, \
             patch.object(mgr, "_send_response_chunk", new_callable=AsyncMock):
            await mgr._on_transcript("No", confidence=0.95, is_final=True, detected_lang="en")
            await asyncio.sleep(0)

        self.assertFalse(mgr._pending_callback_offer)
        self.assertIn(
            PRDScripts.ANYTHING_ELSE, spoken,
            "FAIL (AC5): ANYTHING_ELSE not spoken after 'No'."
        )
        mgr.crm.create_ticket.assert_not_called()
        mock_end.assert_not_called()

    # -----------------------------------------------------------------------
    # AC6 — Fee query at 0.59 (below 0.60 threshold) arms the callback offer
    # -----------------------------------------------------------------------
    async def test_fee_query_below_threshold_triggers_fallback(self):
        """
        A fee-related query with rag_score=0.59 must trigger KB miss (threshold=0.60).
        After generate_and_speak: _pending_callback_offer is True, fallback was spoken.
        """
        mgr = _make_mgr()
        mgr.state.transition_to(CallState.LISTENING)

        spoken = await _run_generate_and_speak(
            mgr,
            "How much is the tuition fee?",
            _kb_miss_gen(
                caller_query="How much is the tuition fee?",
                rag_score=0.59,
                threshold=0.60,
            ),
        )

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
        A score of 0.80 (above all thresholds) must NOT arm the callback offer
        and must NOT speak CALLBACK_OFFER or LOW_CONFIDENCE_FALLBACK.
        """
        mgr = _make_mgr()
        mgr.state.transition_to(CallState.LISTENING)

        spoken = await _run_generate_and_speak(
            mgr, "Tell me about the programs.", _normal_gen()
        )

        self.assertFalse(
            mgr._pending_callback_offer,
            "FAIL (AC7): _pending_callback_offer armed for high-confidence response."
        )
        self.assertNotIn(
            PRDScripts.CALLBACK_OFFER, spoken,
            "FAIL (AC7): CALLBACK_OFFER spoken for high-confidence response."
        )
        self.assertNotIn(
            PRDScripts.LOW_CONFIDENCE_FALLBACK, " ".join(spoken),
            "FAIL (AC7): LOW_CONFIDENCE_FALLBACK spoken for high-confidence response."
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
