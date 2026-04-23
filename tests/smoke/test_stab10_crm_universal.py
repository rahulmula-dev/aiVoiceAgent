"""
STAB-10 Universal CRM Ticket Creation — Smoke Tests
=====================================================
PRD §10: every call produces a CRM ticket, without exception.

  Scenario A (Normal):     1 question answered, call ends normally.
                           → Ticket created, callback_required=False, title contains "Normal"
  Scenario B (Restricted): Hard refusal triggered mid-call.
                           → Cleanup still creates a session-summary ticket (in addition to the
                             security alert ticket created by handle_restricted_topic).
  Scenario C (Silence):    Caller goes silent, _trigger_silence_termination() fires.
                           → Ticket created, callback_required=True, reason="silence_termination"

Run:
  python -m pytest tests/smoke/test_stab10_crm_universal.py -v
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


# ---------------------------------------------------------------------------
# Fixture helper
# ---------------------------------------------------------------------------

def _make_mgr(termination_reason="user_hangup", with_history=True):
    stt = MagicMock()
    stt.connect = AsyncMock(return_value=True)
    stt.close = AsyncMock()
    stt.set_callback = MagicMock()
    stt.set_listener_error_callback = MagicMock()

    tts = MagicMock()
    tts.speak = AsyncMock()
    tts.stop_current_speech = MagicMock(return_value="")
    tts.close = AsyncMock()

    call_logger = MagicMock()
    call_logger.call_id = "stab10_test"
    call_logger.log_event = MagicMock()
    call_logger.generate_summary_line = MagicMock()
    call_logger.save_log = MagicMock()

    mgr = VoiceOrchestrator(stt_provider=stt, tts_provider=tts, call_logger=call_logger)

    session = MagicMock()
    session.session_id = "stab10_session"
    session.crm_call_id = "stab10_crm"
    session.termination_reason = termination_reason
    session.caller_type = "prospect"
    session.structured_turns = []
    session.conversation_history = (
        [{"role": "user", "parts": ["What programs do you offer?"]},
         {"role": "model", "parts": ["We offer Business, IT, and Design programs."]}]
        if with_history else []
    )

    mgr.session = session
    mgr.sid = "stab10_sid"
    mgr.websocket = AsyncMock()
    mgr.session_manager = MagicMock()
    mgr.session_manager.save_session = MagicMock()
    mgr.session_manager.end_session = MagicMock()
    mgr.session_manager.update_state = MagicMock()
    mgr.crm = MagicMock()
    mgr.crm.create_ticket = AsyncMock()
    mgr.recorder = None
    mgr.transcriber = AsyncMock()
    mgr.transcriber.close = AsyncMock()
    mgr.synthesizer = AsyncMock()
    mgr.synthesizer.close = AsyncMock()
    mgr.silence_task = None
    mgr.response_task = None
    mgr.session_start_wall_time = time.time() - 45.0  # 45 s call duration
    mgr.wrapup_triggered = False
    mgr._cleanup_done = False
    mgr._language_termination_active = False
    mgr.stop_event = asyncio.Event()
    return mgr


async def _run_cleanup(mgr):
    """Run cleanup() with WebSocket sleep bypassed for speed."""
    with patch("asyncio.sleep", new_callable=AsyncMock):
        await mgr.cleanup()


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------

class TestStab10UniversalCRM(unittest.IsolatedAsyncioTestCase):

    # -----------------------------------------------------------------------
    # Scenario A — Normal call: 1 Q&A, user hangs up
    # -----------------------------------------------------------------------
    async def test_normal_call_creates_ticket(self):
        """
        A completed normal call must produce exactly one CRM ticket with:
        - callback_required=False
        - status="Completed"
        - title containing "Normal"
        """
        mgr = _make_mgr(termination_reason="user_hangup")
        await _run_cleanup(mgr)

        mgr.crm.create_ticket.assert_called_once()
        kwargs = mgr.crm.create_ticket.call_args.kwargs

        self.assertFalse(
            kwargs.get("callback_required"),
            "FAIL (A): callback_required should be False for a normal call."
        )
        self.assertEqual(
            kwargs.get("status"), "Completed",
            "FAIL (A): status should be 'Completed' for a normal call."
        )
        self.assertIn(
            "Normal", kwargs.get("title", ""),
            "FAIL (A): Ticket title should contain 'Normal'."
        )
        self.assertIsNotNone(
            kwargs.get("duration"),
            "FAIL (A): duration must be set (not None)."
        )

    # -----------------------------------------------------------------------
    # Scenario B — Restricted: cleanup must still create a session-summary ticket
    # -----------------------------------------------------------------------
    async def test_restricted_call_cleanup_still_creates_ticket(self):
        """
        Even when session_state["restricted_handled"] is True (old guard removed),
        cleanup() must write a session-summary CRM ticket.
        """
        mgr = _make_mgr(termination_reason="user_hangup")
        # Simulate restricted topic having been handled
        mgr.session_state = {"restricted_handled": True}

        await _run_cleanup(mgr)

        # [STAB-10] The skip guard was removed — ticket must always be created
        mgr.crm.create_ticket.assert_called_once()
        kwargs = mgr.crm.create_ticket.call_args.kwargs
        self.assertEqual(kwargs.get("status"), "Completed")

    # -----------------------------------------------------------------------
    # Scenario C — Silence termination: callback_required=True, reason in summary
    # -----------------------------------------------------------------------
    async def test_silence_termination_creates_callback_ticket(self):
        """
        When termination_reason="silence_termination", the cleanup ticket must have:
        - callback_required=True
        - title containing "Silence"
        - metadata["termination_reason"] == "silence_termination"
        """
        mgr = _make_mgr(termination_reason="silence_termination")
        await _run_cleanup(mgr)

        mgr.crm.create_ticket.assert_called_once()
        kwargs = mgr.crm.create_ticket.call_args.kwargs

        self.assertTrue(
            kwargs.get("callback_required"),
            "FAIL (C): callback_required must be True for silence termination."
        )
        self.assertIn(
            "Silence", kwargs.get("title", ""),
            "FAIL (C): Ticket title should contain 'Silence'."
        )
        meta = kwargs.get("metadata", {})
        self.assertEqual(
            meta.get("termination_reason"), "silence_termination",
            "FAIL (C): metadata.termination_reason must be 'silence_termination'."
        )

    # -----------------------------------------------------------------------
    # No-session path: connection failed before session init
    # -----------------------------------------------------------------------
    async def test_no_session_still_creates_ticket(self):
        """Even with no session object, cleanup must write a Failed ticket."""
        mgr = _make_mgr()
        mgr.session = None
        mgr._early_sid = "no_session_sid"

        await _run_cleanup(mgr)

        mgr.crm.create_ticket.assert_called_once()
        kwargs = mgr.crm.create_ticket.call_args.kwargs
        self.assertEqual(kwargs.get("status"), "Failed")
        self.assertTrue(kwargs.get("callback_required"))

    # -----------------------------------------------------------------------
    # Double-cleanup guard: second cleanup() must NOT create a duplicate ticket
    # -----------------------------------------------------------------------
    async def test_double_cleanup_does_not_duplicate_ticket(self):
        """
        _cleanup_done guard: calling cleanup() twice (e.g. silence + disconnect)
        must only create one CRM ticket, not two.
        """
        mgr = _make_mgr(termination_reason="silence_termination")
        await _run_cleanup(mgr)
        await _run_cleanup(mgr)  # second call must be a no-op

        self.assertEqual(
            mgr.crm.create_ticket.call_count, 1,
            "FAIL: CRM ticket created twice — double-cleanup guard broken."
        )

    # -----------------------------------------------------------------------
    # Duration is populated from session_start_wall_time
    # -----------------------------------------------------------------------
    async def test_duration_is_calculated(self):
        """duration field in CRM ticket must be a positive float."""
        mgr = _make_mgr(termination_reason="user_hangup")
        mgr.session_start_wall_time = time.time() - 62.0  # 62 s call

        await _run_cleanup(mgr)

        kwargs = mgr.crm.create_ticket.call_args.kwargs
        duration = kwargs.get("duration")
        self.assertIsNotNone(duration, "FAIL: duration is None.")
        self.assertGreater(duration, 0, "FAIL: duration must be positive.")

    # -----------------------------------------------------------------------
    # Recording URL: local path passed when recorder exists
    # -----------------------------------------------------------------------
    async def test_recording_url_local_path_fallback(self):
        """When recorder.filename is set (local WAV), recording_url must be that path."""
        mgr = _make_mgr(termination_reason="user_hangup")
        mgr.recorder = MagicMock()
        mgr.recorder.filename = "/recordings/stab10_session_20260406_120000.wav"
        mgr.recorder.close = MagicMock()

        await _run_cleanup(mgr)

        kwargs = mgr.crm.create_ticket.call_args.kwargs
        self.assertEqual(
            kwargs.get("recording_url"),
            "/recordings/stab10_session_20260406_120000.wav",
            "FAIL: recording_url should be the local WAV path when S3 is unavailable."
        )

    # -----------------------------------------------------------------------
    # No-history path (early exit): ticket still created
    # -----------------------------------------------------------------------
    async def test_no_history_early_exit_creates_ticket(self):
        """Call ended before any conversation — cleanup must still write a ticket."""
        mgr = _make_mgr(termination_reason="abandoned_setup", with_history=False)
        await _run_cleanup(mgr)

        mgr.crm.create_ticket.assert_called_once()
        kwargs = mgr.crm.create_ticket.call_args.kwargs
        self.assertEqual(kwargs.get("status"), "Completed")
        self.assertFalse(kwargs.get("callback_required"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
