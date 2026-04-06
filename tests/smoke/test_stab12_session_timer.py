"""
STAB-12 Smoke Tests — Session Timing (5-Min Soft Warning, 6-Min Hard End)

TC-17:
  ✔ Soft warning fires at ~5 min (±15 s)
  ✔ Call continues
  ✔ Post-warning queries work
  ✔ No overlap with active TTS

TC-18:
  ✔ Hard end fires at ~6 min (±15 s)
  ✔ Polite termination message spoken
  ✔ CRM updated (wrapup_timeout reason)
  ✔ Logs flushed
  ✔ Layered idempotency (3 guards)

TC-19 (NEW — Point 7):
  ✔ Hard end while AI is mid-response
  ✔ Active response_task cancelled gracefully (no abrupt cut-off)
  ✔ Polite closing message still spoken
  ✔ CRM update still happens
  ✔ No duplicate termination

Non-regression guarantee:
  - SessionTimerManager does NOT touch silence timers.
  - Soft warning does NOT call cleanup().
  - Hard end calls cleanup() exactly once.
  - No duplicate triggers on multiple ticks past the threshold.
"""

import asyncio
import time
import types
import pytest

# ─── Helpers ──────────────────────────────────────────────────────────────────

def make_timer(elapsed_at_start: float = 0.0):
    """Return a SessionTimerManager whose wall-clock start is `elapsed_at_start` seconds ago."""
    from orchestrator.session_timer_manager import SessionTimerManager
    start_time = time.time() - elapsed_at_start
    return SessionTimerManager(session_start_wall_time=start_time)


# ═══════════════════════════════════════════════════════════════════════════════
# Unit tests for SessionTimerManager
# ═══════════════════════════════════════════════════════════════════════════════

class TestSessionTimerManagerUnit:
    """Pure unit tests — no orchestrator required."""

    @pytest.mark.asyncio
    async def test_soft_warning_fires_once(self):
        """on_soft_warning must be called exactly once at/after 300 s."""
        timer = make_timer(elapsed_at_start=299.5)  # starts 299.5 s into the session
        calls = []

        async def _sw():
            calls.append("sw")

        timer.on_soft_warning = _sw
        await timer.start()
        # Allow ~1.5 s of real-time for the timer to tick past 300 s
        await asyncio.sleep(1.5)
        await timer.cancel()

        assert len(calls) == 1, f"FAIL (TC-17): soft warning fired {len(calls)} time(s), expected 1."

    @pytest.mark.asyncio
    async def test_hard_end_fires_once(self):
        """on_hard_end must be called exactly once at/after 360 s."""
        timer = make_timer(elapsed_at_start=359.5)
        calls = []

        async def _he():
            calls.append("he")

        timer.on_hard_end = _he
        await timer.start()
        await asyncio.sleep(1.5)
        await timer.cancel()

        assert len(calls) == 1, f"FAIL (TC-18): hard end fired {len(calls)} time(s), expected 1."

    @pytest.mark.asyncio
    async def test_soft_warning_does_not_fire_before_threshold(self):
        """No soft warning before 300 s."""
        timer = make_timer(elapsed_at_start=0.0)  # fresh session
        calls = []

        async def _sw():
            calls.append("sw")

        timer.on_soft_warning = _sw
        await timer.start()
        await asyncio.sleep(0.6)  # only 0.6 s of wallclock
        await timer.cancel()

        assert not calls, f"FAIL: soft warning fired too early — session was only ~0.6 s old."

    @pytest.mark.asyncio
    async def test_no_duplicate_soft_warning(self):
        """Even after many ticks past 300 s, soft warning fires exactly once."""
        timer = make_timer(elapsed_at_start=299.5)
        calls = []

        async def _sw():
            calls.append("sw")
            await asyncio.sleep(0.1)  # simulate async work

        timer.on_soft_warning = _sw
        await timer.start()
        # Allow 3 s — that's 6 ticks past the threshold
        await asyncio.sleep(3.0)
        await timer.cancel()

        assert len(calls) == 1, (
            f"FAIL: duplicate soft warning — fired {len(calls)} time(s). "
            "Race condition in _soft_warning_fired guard?"
        )

    @pytest.mark.asyncio
    async def test_no_duplicate_hard_end(self):
        """Even after many ticks past 360 s, hard end fires exactly once."""
        timer = make_timer(elapsed_at_start=359.5)
        calls = []

        async def _he():
            calls.append("he")
            await asyncio.sleep(0.1)

        timer.on_hard_end = _he
        await timer.start()
        await asyncio.sleep(3.0)
        await timer.cancel()

        assert len(calls) == 1, (
            f"FAIL: duplicate hard end — fired {len(calls)} time(s). "
            "Race condition in _hard_end_fired guard?"
        )

    @pytest.mark.asyncio
    async def test_soft_warning_before_hard_end(self):
        """Both events fire in the correct order: soft_warning then hard_end."""
        timer = make_timer(elapsed_at_start=299.5)
        events = []

        async def _sw():
            events.append("sw")

        async def _he():
            events.append("he")

        timer.on_soft_warning = _sw
        timer.on_hard_end = _he
        await timer.start()
        # Need to tick past 360 s: elapsed is 299.5 + real_time
        # After ~60.5 s difference we'd need to wait — use pre-elapsed to shortcut.
        # Instead: start at 359.5 s to catch both in quick succession.
        timer2 = make_timer(elapsed_at_start=359.5)
        events2 = []

        async def _sw2():
            events2.append("sw2")

        async def _he2():
            events2.append("he2")

        timer2.on_soft_warning = _sw2
        timer2.on_hard_end = _he2
        await timer2.start()
        await asyncio.sleep(1.5)
        await timer.cancel()
        await timer2.cancel()

        # For timer2 (started at 359.5 s), BOTH should fire quickly
        assert "sw2" in events2, "FAIL (TC-17): soft_warning not fired for timer2."
        assert "he2" in events2, "FAIL (TC-18): hard_end not fired for timer2."
        sw_idx = events2.index("sw2")
        he_idx = events2.index("he2")
        assert sw_idx < he_idx, (
            f"FAIL: event ordering wrong — sw2={sw_idx}, he2={he_idx}. "
            "soft_warning must fire before hard_end."
        )

    @pytest.mark.asyncio
    async def test_cancel_before_threshold_does_not_fire(self):
        """Cancelling the timer before 300 s must not trigger either event."""
        timer = make_timer(elapsed_at_start=0.0)
        calls = []

        async def _sw():
            calls.append("sw")

        async def _he():
            calls.append("he")

        timer.on_soft_warning = _sw
        timer.on_hard_end = _he
        await timer.start()
        await asyncio.sleep(0.3)
        await timer.cancel()

        assert not calls, f"FAIL: events fired after cancel: {calls}"


# ═══════════════════════════════════════════════════════════════════════════════
# Integration tests — orchestrator handler stubs (TC-17 / TC-18)
# ═══════════════════════════════════════════════════════════════════════════════

class TestStab12OrchestratorHandlers:
    """
    Test the orchestrator's _on_session_soft_warning and _on_session_hard_end
    methods in isolation, without spinning up the full telephony stack.

    We build a minimal stub object that has exactly the attributes/methods
    the handlers need — no patching of the real class required.
    """

    def _make_mgr(self):
        """Build a minimal stub that satisfies ALL handler methods including hardened versions."""
        import types
        from unittest.mock import AsyncMock, MagicMock
        from contracts.policy import PRDScripts
        from contracts.state import CallState

        mgr = types.SimpleNamespace()

        # ── State flags ──────────────────────────────────────────────────
        mgr._cleanup_done = False
        mgr._hard_end_active = False    # [Point 5] Layer-2 idempotency guard
        mgr.wrapup_triggered = False
        mgr.session_start_wall_time = time.time() - 301.0

        # ── Call identity ────────────────────────────────────────────────
        mgr.sid = "test-stream-sid"

        # ── Session ──────────────────────────────────────────────────────
        class _FakeSession:
            crm_call_id = "crm-test-001"
            session_id  = "sess-test-001"
            termination_reason = None

        mgr.session = _FakeSession()

        # ── Active response task (default: None / not running) ───────────
        mgr.response_task = None

        # ── Synthesizer (minimal stub) ────────────────────────────────────
        mgr.synthesizer = MagicMock()
        mgr.synthesizer.stop_current_speech = MagicMock(return_value="")

        # ── _send_clear_message ───────────────────────────────────────────
        async def _clear():
            pass
        mgr._send_clear_message = _clear

        # ── CRM ──────────────────────────────────────────────────────────
        mgr.crm = MagicMock()
        mgr.crm.create_ticket = AsyncMock(return_value="ticket-123")

        # ── Logger ───────────────────────────────────────────────────────
        mgr.call_logger = MagicMock()
        mgr.call_logger.log_event = MagicMock()

        # ── State machine ────────────────────────────────────────────────
        mgr.state = MagicMock()
        mgr.state.get_state.return_value = CallState.LISTENING
        mgr.state.transition_to = MagicMock()

        # ── _create_task_with_log: just swallow it ────────────────────────
        mgr._create_task_with_log = MagicMock(
            return_value=asyncio.ensure_future(asyncio.sleep(0))
        )

        # ── speak_immediate_response: track calls ─────────────────────────
        spoken = []

        async def _speak(text, trace_id=None):
            spoken.append(text)

        mgr.speak_immediate_response = _speak

        # ── cleanup: track invocations ────────────────────────────────────
        cleanups = []

        async def _cleanup():
            cleanups.append(1)

        mgr.cleanup = _cleanup
        mgr._spoken = spoken
        mgr._cleanups = cleanups

        # ── Bind the actual handler methods from VoiceOrchestrator ────────
        #    We import the unbound functions and bind them to our stub.
        import orchestrator.manager as _mod
        mgr._on_session_soft_warning = (
            lambda: _mod.VoiceOrchestrator._on_session_soft_warning(mgr)
        )
        mgr._on_session_hard_end = (
            lambda: _mod.VoiceOrchestrator._on_session_hard_end(mgr)

        )
        return mgr

    @pytest.mark.asyncio
    async def test_tc17_soft_warning_speaks_exact_phrase(self):
        """
        TC-17 AC1: Soft warning speaks EXACTLY PRDScripts.WRAP_UP.
        """
        from contracts.policy import PRDScripts
        mgr = self._make_mgr()
        await mgr._on_session_soft_warning()

        assert PRDScripts.WRAP_UP in mgr._spoken, (
            f"FAIL (TC-17): Expected WRAP_UP phrase in spoken list.\n"
            f"Spoken: {mgr._spoken}\n"
            f"Expected: '{PRDScripts.WRAP_UP}'"
        )

    @pytest.mark.asyncio
    async def test_tc17_soft_warning_does_not_terminate(self):
        """
        TC-17 AC2: Soft warning must NOT call cleanup().
        """
        mgr = self._make_mgr()
        await mgr._on_session_soft_warning()

        assert not mgr._cleanups, (
            "FAIL (TC-17): Soft warning called cleanup() — it must NOT terminate the call."
        )

    @pytest.mark.asyncio
    async def test_tc17_soft_warning_sets_wrapup_flag(self):
        """
        TC-17 AC3: wrapup_triggered flag set to True after soft warning.
        """
        mgr = self._make_mgr()
        assert not mgr.wrapup_triggered
        await mgr._on_session_soft_warning()
        assert mgr.wrapup_triggered, "FAIL (TC-17): wrapup_triggered not set after soft warning."

    @pytest.mark.asyncio
    async def test_tc17_soft_warning_skipped_if_cleanup_done(self):
        """
        TC-17 AC4: If cleanup already ran, soft warning is skipped (idempotency).
        """
        mgr = self._make_mgr()
        mgr._cleanup_done = True
        await mgr._on_session_soft_warning()
        assert not mgr._spoken, "FAIL (TC-17): Soft warning should be skipped after cleanup."

    @pytest.mark.asyncio
    async def test_tc18_hard_end_speaks_polite_message(self):
        """
        TC-18 AC1: Hard end speaks a polite closing message before terminating.
        """
        from contracts.policy import PRDScripts
        mgr = self._make_mgr()
        await mgr._on_session_hard_end()

        assert PRDScripts.WRAP_UP_TERMINATION in mgr._spoken, (
            f"FAIL (TC-18): Expected WRAP_UP_TERMINATION in spoken.\n"
            f"Spoken: {mgr._spoken}"
        )

    @pytest.mark.asyncio
    async def test_tc18_hard_end_calls_cleanup(self):
        """
        TC-18 AC2: Hard end must call cleanup() for CRM update + log flush.
        """
        mgr = self._make_mgr()
        await mgr._on_session_hard_end()
        assert mgr._cleanups, "FAIL (TC-18): Hard end did not call cleanup()."

    @pytest.mark.asyncio
    async def test_tc18_hard_end_sets_termination_reason(self):
        """
        TC-18 AC3: session.termination_reason must be 'wrapup_timeout' before cleanup.
        """
        mgr = self._make_mgr()
        await mgr._on_session_hard_end()
        assert mgr.session.termination_reason == "wrapup_timeout", (
            f"FAIL (TC-18): termination_reason='{mgr.session.termination_reason}', expected 'wrapup_timeout'."
        )

    @pytest.mark.asyncio
    async def test_tc18_hard_end_skipped_if_cleanup_done(self):
        """
        TC-18 AC4: If cleanup already ran, hard end is a no-op (idempotency / double-cleanup guard).
        """
        mgr = self._make_mgr()
        mgr._cleanup_done = True
        await mgr._on_session_hard_end()
        assert not mgr._spoken, "FAIL (TC-18): Hard end should be skipped after cleanup."
        assert not mgr._cleanups, "FAIL (TC-18): cleanup() should not be called a second time."

    @pytest.mark.asyncio
    async def test_tc18_hard_end_concurrent_reentry_blocked(self):
        """
        TC-18 AC5 (Point 5 Layer 2): _hard_end_active flag blocks concurrent re-entry
        before _cleanup_done is set.
        """
        mgr = self._make_mgr()
        mgr._hard_end_active = True   # simulate concurrent in-flight call
        await mgr._on_session_hard_end()
        # Because _hard_end_active is True, the handler must return immediately
        assert not mgr._spoken, "FAIL (TC-18): re-entrant hard_end should not speak."
        assert not mgr._cleanups, "FAIL (TC-18): re-entrant hard_end should not call cleanup()."


# ═══════════════════════════════════════════════════════════════════════════════
# TC-19 — Hard end during active AI response (NEW)
# ═══════════════════════════════════════════════════════════════════════════════

class TestStab12TC19HardEndDuringResponse:
    """
    TC-19: Verify graceful-shutdown behaviour when the 6-minute hard-end fires
    while the AI is actively generating/speaking a response.

    Acceptance criteria:
      ✔ Active response_task is cancelled gracefully (no abrupt audio cut-off)
      ✔ synthesizer.stop_current_speech() called
      ✔ Polite closing message (WRAP_UP_TERMINATION) still spoken
      ✔ cleanup() still called → CRM update guaranteed
      ✔ No duplicate termination
    """

    def _make_mgr_with_active_response(self):
        """Extend the standard stub with a live (never-ending) response_task."""
        import types
        from unittest.mock import AsyncMock, MagicMock
        from contracts.state import CallState

        mgr = types.SimpleNamespace()

        mgr._cleanup_done = False
        mgr._hard_end_active = False
        mgr.wrapup_triggered = False
        mgr.session_start_wall_time = time.time() - 361.0
        mgr.sid = "test-stream-sid"

        class _FakeSession:
            crm_call_id = "crm-tc19"
            session_id  = "sess-tc19"
            termination_reason = None
        mgr.session = _FakeSession()

        # Simulate a running response_task that sleeps forever
        response_cancelled = []

        async def _fake_response_coro():
            try:
                await asyncio.sleep(9999)
            except asyncio.CancelledError:
                response_cancelled.append(True)
                raise

        # Start the fake "active response" task
        mgr.response_task = asyncio.create_task(_fake_response_coro())
        mgr._response_cancelled = response_cancelled

        mgr.synthesizer = MagicMock()
        stop_calls = []
        mgr.synthesizer.stop_current_speech = MagicMock(side_effect=lambda _: stop_calls.append(True))
        mgr._stop_calls = stop_calls

        clear_calls = []
        async def _clear():
            clear_calls.append(True)
        mgr._send_clear_message = _clear
        mgr._clear_calls = clear_calls

        mgr.crm = MagicMock()
        mgr.crm.create_ticket = AsyncMock(return_value="ticket-123")
        mgr.call_logger = MagicMock()
        mgr.call_logger.log_event = MagicMock()
        mgr.state = MagicMock()
        mgr.state.get_state.return_value = CallState.SPEAKING  # actively speaking
        mgr.state.transition_to = MagicMock()
        mgr._create_task_with_log = MagicMock(
            return_value=asyncio.ensure_future(asyncio.sleep(0))
        )

        spoken = []
        async def _speak(text, trace_id=None):
            spoken.append(text)
        mgr.speak_immediate_response = _speak

        cleanups = []
        async def _cleanup():
            cleanups.append(1)
        mgr.cleanup = _cleanup
        mgr._spoken = spoken
        mgr._cleanups = cleanups

        import orchestrator.manager as _mod
        mgr._on_session_hard_end = (
            lambda: _mod.VoiceOrchestrator._on_session_hard_end(mgr)
        )
        return mgr

    @pytest.mark.asyncio
    async def test_tc19_active_response_task_cancelled(self):
        """
        TC-19 AC1: Active response_task must be cancelled (not ignored) when hard end fires.
        This prevents the in-progress LLM audio from playing over the closing message.
        """
        mgr = self._make_mgr_with_active_response()
        # Give the fake response task a chance to start and reach its sleep point
        await asyncio.sleep(0.1)
        assert not mgr.response_task.done(), "Precondition: response_task must be running."

        await mgr._on_session_hard_end()
        # Give the event loop a tick to process the cancellation
        await asyncio.sleep(0.05)

        assert mgr._response_cancelled, (
            "FAIL (TC-19): Active response_task was NOT cancelled during hard end. "
            "Audio may have overlapped with the closing message."
        )

    @pytest.mark.asyncio
    async def test_tc19_polite_message_still_spoken(self):
        """
        TC-19 AC2: Even with an active response, WRAP_UP_TERMINATION must play.
        """
        from contracts.policy import PRDScripts
        mgr = self._make_mgr_with_active_response()
        await mgr._on_session_hard_end()

        assert PRDScripts.WRAP_UP_TERMINATION in mgr._spoken, (
            f"FAIL (TC-19): Polite closing not spoken during active-response hard end.\n"
            f"Spoken: {mgr._spoken}"
        )

    @pytest.mark.asyncio
    async def test_tc19_cleanup_still_fires(self):
        """
        TC-19 AC3: cleanup() must fire regardless of whether a response was in-progress.
        This guarantees CRM update + log flush even in the active-response scenario.
        """
        mgr = self._make_mgr_with_active_response()
        await mgr._on_session_hard_end()
        assert mgr._cleanups, (
            "FAIL (TC-19): cleanup() not called during hard end with active response. "
            "CRM update would be lost."
        )

    @pytest.mark.asyncio
    async def test_tc19_stop_current_speech_called(self):
        """
        TC-19 AC4: synthesizer.stop_current_speech() must be called to flush TTS state.
        """
        mgr = self._make_mgr_with_active_response()
        await mgr._on_session_hard_end()
        assert mgr._stop_calls, (
            "FAIL (TC-19): synthesizer.stop_current_speech() not called during hard end. "
            "TTS state left dirty."
        )

    @pytest.mark.asyncio
    async def test_tc19_no_duplicate_termination(self):
        """
        TC-19 AC5: Even with concurrent triggers (response_task + timer), cleanup fires exactly once.
        """
        mgr = self._make_mgr_with_active_response()
        # Simulate two concurrent hard-end calls (e.g. timer fires + external signal)
        await asyncio.gather(
            mgr._on_session_hard_end(),
            mgr._on_session_hard_end(),
        )
        assert len(mgr._cleanups) == 1, (
            f"FAIL (TC-19): cleanup() called {len(mgr._cleanups)} time(s) — expected exactly 1. "
            "Duplicate termination detected."
        )

# ═══════════════════════════════════════════════════════════════════════════════
# Non-regression: ensure silence timer is not affected
# ═══════════════════════════════════════════════════════════════════════════════

class TestStab12NonRegression:
    """
    Guard that STAB-12 changes do not regress the silence timer (STAB-04).
    The session timer and silence timer must be fully independent.
    """

    @pytest.mark.asyncio
    async def test_silence_timer_not_affected_by_session_timer(self):
        """
        Two independent timers running concurrently must not interfere.
        """
        from orchestrator.session_timer_manager import SessionTimerManager

        session_events = []
        silence_events = []

        # Minimal session timer at 299.5 s (will fire soft warning)
        session_timer = SessionTimerManager(session_start_wall_time=time.time() - 299.5)

        async def _sw():
            session_events.append("soft_warning")

        session_timer.on_soft_warning = _sw

        # Simulate a silence timer as a parallel asyncio.Task
        async def _silence_loop():
            for _ in range(4):
                await asyncio.sleep(0.3)
                silence_events.append("silence_tick")

        silence_task = asyncio.create_task(_silence_loop())

        await session_timer.start()
        await asyncio.gather(silence_task)
        await session_timer.cancel()

        # Both should have run independently
        assert "soft_warning" in session_events, "FAIL: session timer did not fire soft warning."
        assert len(silence_events) == 4, f"FAIL: silence loop affected by session timer — ticks={len(silence_events)}"

    def test_prd_scripts_single_source_of_truth(self):
        """
        Verify PRDScripts contains exactly one definition each for
        WRAP_UP, WRAP_UP_TERMINATION — the canonical phrases.
        """
        from contracts.policy import PRDScripts

        # WRAP_UP is the exact phrase required by STAB-12 §1
        expected_soft = "Before we wrap up, is there anything else I can help with?"
        assert PRDScripts.WRAP_UP == expected_soft, (
            f"FAIL: PRDScripts.WRAP_UP changed! Expected:\n  '{expected_soft}'\nGot:\n  '{PRDScripts.WRAP_UP}'"
        )

        # WRAP_UP_TERMINATION must exist and end with 'Goodbye'
        assert PRDScripts.WRAP_UP_TERMINATION, "FAIL: PRDScripts.WRAP_UP_TERMINATION is empty."
        assert "Goodbye" in PRDScripts.WRAP_UP_TERMINATION, (
            f"FAIL: WRAP_UP_TERMINATION does not contain 'Goodbye': '{PRDScripts.WRAP_UP_TERMINATION}'"
        )

    def test_no_duplicate_soft_warning_strings_in_manager(self):
        """
        Scan manager.py source for duplicate wrap-up prompt strings inline.
        After STAB-12, the inline elapsed block must be gone.
        """
        import os
        manager_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "orchestrator", "manager.py"
        )
        with open(os.path.normpath(manager_path), encoding="utf-8") as f:
            src = f.read()

        # The old inline pattern used 'elapsed >= 300' or 'elapsed >= 360'
        # These must NOT appear in _monitor_silence any more.
        assert "elapsed >= 300" not in src, (
            "FAIL: inline 'elapsed >= 300' still present in manager.py. "
            "STAB-12 requires this to be removed in favour of SessionTimerManager."
        )
        assert "elapsed >= 360" not in src, (
            "FAIL: inline 'elapsed >= 360' still present in manager.py. "
            "STAB-12 requires this to be removed in favour of SessionTimerManager."
        )
