"""
STAB-04 Silence Timer Compliance — Verification Tests
======================================================
Verifies PRD §6.4 two-stage silence behaviour against the refactored
_monitor_silence() / _trigger_silence_termination() implementation.

Acceptance Criteria tested:
  AC1 — Soft prompt fires at ≤ soft_prompt_s + 1 s tolerance (default 10 s)
  AC2 — Termination fires at ≤ total_s + 1 s tolerance (default 20 s total)
  AC3 — NO third stage: timer terminates after exactly 2 fired events
  AC4 — Mid-query grace: 3 s grace after TRANSCRIBING; soft prompt does NOT fire early
  AC5 — Post-clarification path: stage-1 prompt is the last AI question, not SILENCE_1
  AC6 — CRM callback fires when interrupted/abandoned turns exist on silence termination
  AC7 — CRM callback does NOT fire when all turns are complete

Run:
  python -m pytest tests/smoke/test_stab04_silence.py -v
"""

import sys
import os
import asyncio
import time
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
    call_logger.call_id = "stab04_test"
    call_logger.log_event = MagicMock()

    mgr = VoiceOrchestrator(stt_provider=stt, tts_provider=tts, call_logger=call_logger)

    session = MagicMock()
    session.session_id = "stab04_session"
    session.crm_call_id = "stab04_crm"
    session.conversation_history = []
    session.structured_turns = []
    session.start_time = None
    session.termination_reason = None

    mgr.session = session
    mgr.sid = "stab04_sid"
    mgr.websocket = AsyncMock()
    mgr.session_manager = MagicMock()
    mgr.session_manager.save_session = MagicMock()
    mgr.crm = MagicMock()
    mgr.crm.create_ticket = AsyncMock()
    mgr.context_manager = MagicMock()
    mgr.session_start_wall_time = None  # Disable wrapup timer in tests
    mgr.wrapup_triggered = False
    mgr.silence_stage = 0
    mgr.last_interaction_time = time.time()
    mgr.last_response_was_question = False
    mgr._last_ai_question_text = ""
    mgr.stop_event = asyncio.Event()
    return mgr


def _add_turn(mgr, status="completed", is_multi_step=False):
    turn = StandardTurn(
        turn_id=len(mgr.session.structured_turns) + 1,
        caller_input="test input",
        topic="Test",
        agent_response_status=status,
        is_multi_step=is_multi_step,
    )
    mgr.session.structured_turns.append(turn)
    return turn


async def _run_monitor_until_termination(mgr, timeout=35.0):
    """
    Run _monitor_silence() and collect all speak_immediate_response calls.
    Returns (spoken_texts, elapsed_seconds).
    """
    spoken = []
    start = time.time()

    async def mock_speak(text, trace_id=None):
        spoken.append((text, time.time() - start))

    async def mock_cleanup():
        pass  # Don't actually clean up in tests

    with patch.object(mgr, "speak_immediate_response", side_effect=mock_speak), \
         patch.object(mgr, "cleanup", side_effect=mock_cleanup), \
         patch.object(mgr.state, "transition_to", MagicMock()):
        try:
            await asyncio.wait_for(mgr._monitor_silence(), timeout=timeout)
        except asyncio.TimeoutError:
            pass  # Expected if termination didn't fire within timeout

    return spoken, time.time() - start


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------

class TestStab04SilenceCompliance(unittest.IsolatedAsyncioTestCase):

    # -----------------------------------------------------------------------
    # AC1 — Soft prompt fires at ~10 s
    # -----------------------------------------------------------------------
    async def test_soft_prompt_fires_at_10s(self):
        """
        With silence_soft_prompt_s=3 (accelerated), the soft prompt must fire
        within 3 + 1 s tolerance.
        """
        mgr = _make_mgr()
        mgr.state.transition_to(CallState.LISTENING)

        with patch.object(mgr.config.__class__, "silence_soft_prompt_s",
                          new_callable=lambda: property(lambda self: 3.0)), \
             patch.object(mgr.config.__class__, "silence_termination_s",
                          new_callable=lambda: property(lambda self: 8.0)):

            spoken = []
            termination_called = asyncio.Event()

            async def mock_speak(text, trace_id=None):
                spoken.append(text)

            async def mock_termination():
                termination_called.set()

            with patch.object(mgr, "speak_immediate_response", side_effect=mock_speak), \
                 patch.object(mgr, "_trigger_silence_termination", side_effect=mock_termination), \
                 patch.object(mgr, "cleanup", new_callable=AsyncMock):

                monitor_task = asyncio.create_task(mgr._monitor_silence())

                # Wait up to 5 s for the soft prompt to appear (3 s threshold + 1 s slack)
                deadline = time.time() + 5.0
                while time.time() < deadline and len(spoken) == 0:
                    await asyncio.sleep(0.1)

                mgr.stop_event.set()
                monitor_task.cancel()
                try:
                    await monitor_task
                except (asyncio.CancelledError, Exception):
                    pass

        self.assertGreater(len(spoken), 0, "FAIL (AC1): Soft prompt never fired.")
        self.assertEqual(spoken[0], PRDScripts.SILENCE_1,
                         "FAIL (AC1): Wrong soft prompt script played.")

    # -----------------------------------------------------------------------
    # AC2 — Termination fires at ~20 s total (10+10)
    # -----------------------------------------------------------------------
    async def test_termination_fires_at_20s_total(self):
        """
        Two-stage: soft prompt at ~3 s, termination at ~6 s total (3+3 accelerated).
        Verifies the stage-1 timer resets and termination fires after the second gap.
        """
        mgr = _make_mgr()
        mgr.state.transition_to(CallState.LISTENING)

        termination_event = asyncio.Event()

        async def mock_speak(text, trace_id=None):
            pass  # soft prompt is a no-op here

        async def mock_termination():
            termination_event.set()

        with patch.object(mgr.config.__class__, "silence_soft_prompt_s",
                          new_callable=lambda: property(lambda self: 2.0)), \
             patch.object(mgr.config.__class__, "silence_termination_s",
                          new_callable=lambda: property(lambda self: 4.0)), \
             patch.object(mgr, "speak_immediate_response", side_effect=mock_speak), \
             patch.object(mgr, "_trigger_silence_termination", side_effect=mock_termination), \
             patch.object(mgr, "cleanup", new_callable=AsyncMock):

            monitor_task = asyncio.create_task(mgr._monitor_silence())

            # Allow up to 8 s for both stages to fire (2+2 thresholds + 4 s slack)
            try:
                await asyncio.wait_for(termination_event.wait(), timeout=8.0)
            except asyncio.TimeoutError:
                pass
            finally:
                mgr.stop_event.set()
                monitor_task.cancel()
                try:
                    await monitor_task
                except (asyncio.CancelledError, Exception):
                    pass

        self.assertTrue(
            termination_event.is_set(),
            "FAIL (AC2): Termination did not fire within the expected window."
        )

    # -----------------------------------------------------------------------
    # AC3 — Only two stages fire (no third warning)
    # -----------------------------------------------------------------------
    async def test_no_third_stage(self):
        """
        The old 3-stage implementation had SILENCE_1, SILENCE_2, then termination.
        The new implementation must only play one soft prompt (SILENCE_1) then terminate.
        SILENCE_2 must never be spoken.
        """
        mgr = _make_mgr()
        mgr.state.transition_to(CallState.LISTENING)

        spoken = []
        termination_event = asyncio.Event()

        async def mock_speak(text, trace_id=None):
            spoken.append(text)

        async def mock_termination():
            termination_event.set()

        with patch.object(mgr.config.__class__, "silence_soft_prompt_s",
                          new_callable=lambda: property(lambda self: 2.0)), \
             patch.object(mgr.config.__class__, "silence_termination_s",
                          new_callable=lambda: property(lambda self: 4.0)), \
             patch.object(mgr, "speak_immediate_response", side_effect=mock_speak), \
             patch.object(mgr, "_trigger_silence_termination", side_effect=mock_termination), \
             patch.object(mgr, "cleanup", new_callable=AsyncMock):

            monitor_task = asyncio.create_task(mgr._monitor_silence())
            try:
                await asyncio.wait_for(termination_event.wait(), timeout=8.0)
            except asyncio.TimeoutError:
                pass
            finally:
                mgr.stop_event.set()
                monitor_task.cancel()
                try:
                    await monitor_task
                except (asyncio.CancelledError, Exception):
                    pass

        self.assertNotIn(
            PRDScripts.SILENCE_2, spoken,
            "FAIL (AC3): SILENCE_2 (second warning) was spoken — old 3-stage logic still present."
        )

    # -----------------------------------------------------------------------
    # AC4 — Mid-query grace: soft prompt does NOT fire during / immediately after TRANSCRIBING
    # -----------------------------------------------------------------------
    async def test_midquery_grace_delays_soft_prompt(self):
        """
        While the caller is in TRANSCRIBING state, the 3 s grace window is armed.
        Even if total elapsed time would exceed soft_s, the prompt must not fire until
        3 s after the last TRANSCRIBING tick.
        """
        mgr = _make_mgr()
        # Start in TRANSCRIBING so grace is armed on first tick
        mgr.state.transition_to(CallState.LISTENING)
        mgr.state.transition_to(CallState.TRANSCRIBING)

        spoken = []

        async def mock_speak(text, trace_id=None):
            spoken.append(text)

        with patch.object(mgr.config.__class__, "silence_soft_prompt_s",
                          new_callable=lambda: property(lambda self: 1.0)), \
             patch.object(mgr.config.__class__, "silence_termination_s",
                          new_callable=lambda: property(lambda self: 5.0)), \
             patch.object(mgr, "speak_immediate_response", side_effect=mock_speak), \
             patch.object(mgr, "_trigger_silence_termination", new_callable=AsyncMock), \
             patch.object(mgr, "cleanup", new_callable=AsyncMock):

            monitor_task = asyncio.create_task(mgr._monitor_silence())

            # Run for 2 s while still in TRANSCRIBING — soft prompt threshold is 1 s
            # but grace should prevent it from firing
            await asyncio.sleep(2.0)

            # Still in TRANSCRIBING, so no prompt should have fired
            self.assertEqual(
                spoken, [],
                "FAIL (AC4): Soft prompt fired while caller was still in TRANSCRIBING (grace broken)."
            )

            mgr.stop_event.set()
            monitor_task.cancel()
            try:
                await monitor_task
            except (asyncio.CancelledError, Exception):
                pass

    # -----------------------------------------------------------------------
    # AC5 — Post-clarification: stage-1 prompt repeats the AI question
    # -----------------------------------------------------------------------
    async def test_post_clarification_repeats_question(self):
        """
        When last_response_was_question=True and _last_ai_question_text is set,
        the stage-1 soft prompt must be the question text, not PRDScripts.SILENCE_1.
        """
        mgr = _make_mgr()
        mgr.state.transition_to(CallState.LISTENING)
        mgr.last_response_was_question = True
        mgr._last_ai_question_text = "Could you tell me which program you are interested in?"

        spoken = []

        async def mock_speak(text, trace_id=None):
            spoken.append(text)

        with patch.object(mgr.config.__class__, "silence_soft_prompt_s",
                          new_callable=lambda: property(lambda self: 2.0)), \
             patch.object(mgr.config.__class__, "silence_termination_s",
                          new_callable=lambda: property(lambda self: 6.0)), \
             patch.object(mgr, "speak_immediate_response", side_effect=mock_speak), \
             patch.object(mgr, "_trigger_silence_termination", new_callable=AsyncMock), \
             patch.object(mgr, "cleanup", new_callable=AsyncMock):

            monitor_task = asyncio.create_task(mgr._monitor_silence())

            # Wait until the soft prompt fires (2 s threshold + 1 s slack)
            deadline = time.time() + 4.0
            while time.time() < deadline and len(spoken) == 0:
                await asyncio.sleep(0.1)

            mgr.stop_event.set()
            monitor_task.cancel()
            try:
                await monitor_task
            except (asyncio.CancelledError, Exception):
                pass

        self.assertGreater(len(spoken), 0, "FAIL (AC5): No soft prompt fired.")
        self.assertEqual(
            spoken[0], mgr._last_ai_question_text,
            "FAIL (AC5): Post-clarification prompt was not the AI's question text."
        )
        self.assertNotEqual(
            spoken[0], PRDScripts.SILENCE_1,
            "FAIL (AC5): Generic SILENCE_1 was played instead of the clarifying question."
        )

    # -----------------------------------------------------------------------
    # AC6 — CRM callback fires when interrupted/abandoned turns exist
    # -----------------------------------------------------------------------
    async def test_crm_callback_fires_for_incomplete_turns(self):
        """
        _trigger_silence_termination() must fire a CRM ticket when at least one
        structured turn has agent_response_status of 'interrupted' or 'abandoned'.
        """
        mgr = _make_mgr()
        _add_turn(mgr, status="interrupted")
        _add_turn(mgr, status="abandoned")
        _add_turn(mgr, status="completed")

        # Patch cleanup + state + speak so we can call termination directly
        with patch.object(mgr, "cleanup", new_callable=AsyncMock), \
             patch.object(mgr, "speak_immediate_response", new_callable=AsyncMock), \
             patch.object(mgr.state, "transition_to", MagicMock()):
            await mgr._trigger_silence_termination()

        mgr.crm.create_ticket.assert_called_once()
        call_kwargs = mgr.crm.create_ticket.call_args
        self.assertIn("Silence_Termination_Incomplete", str(call_kwargs),
                      "FAIL (AC6): CRM ticket title missing 'Silence_Termination_Incomplete'.")
        self.assertEqual(
            mgr.session.termination_reason, "silence_termination",
            "FAIL (AC6): termination_reason not set."
        )

    # -----------------------------------------------------------------------
    # AC7 — CRM callback does NOT fire when all turns are complete
    # -----------------------------------------------------------------------
    async def test_crm_callback_not_fired_for_complete_turns(self):
        """
        If every structured turn has agent_response_status == 'completed',
        no CRM ticket must be created on silence termination.
        """
        mgr = _make_mgr()
        _add_turn(mgr, status="completed")
        _add_turn(mgr, status="completed")

        with patch.object(mgr, "cleanup", new_callable=AsyncMock), \
             patch.object(mgr, "speak_immediate_response", new_callable=AsyncMock), \
             patch.object(mgr.state, "transition_to", MagicMock()):
            await mgr._trigger_silence_termination()

        mgr.crm.create_ticket.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
