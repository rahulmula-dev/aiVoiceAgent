"""
STAB-12: SessionTimerManager — Single source of truth for session duration management.

Responsibilities:
  - Track session start time.
  - Fire on_soft_warning() at 5 minutes (±15 s) — ONCE, idempotent.
  - Fire on_hard_end()      at 6 minutes (±15 s) — ONCE, idempotent.

Design goals (per STAB-12 spec):
  - Non-blocking: runs as an independent asyncio.Task.
  - Async-safe: no shared mutable state outside of the two boolean guards.
  - Zero coupling with TTS, telephony, or silence-timer layers.
  - Event-driven: exposes on_soft_warning / on_hard_end callbacks.
  - Idempotent: flags prevent duplicate fires regardless of tick resolution.

Usage:
    timer = SessionTimerManager(session_start_wall_time=time.time())
    timer.on_soft_warning = my_soft_warning_coro_factory   # callable → coroutine
    timer.on_hard_end      = my_hard_end_coro_factory       # callable → coroutine
    await timer.start()      # fires the background task
    # …
    await timer.cancel()     # clean shutdown
"""

import asyncio
import logging
import time

logger = logging.getLogger("SessionTimerManager")

# ── Timing constants (STAB-12 spec) ──────────────────────────────────────────
SOFT_WARNING_S: float = 300.0   # 5 minutes
HARD_END_S:     float = 360.0   # 6 minutes
TICK_S:         float = 0.5     # 0.5 s tick — stays within ±15 s tolerance


class SessionTimerManager:
    """
    Isolated session duration controller.

    Attributes:
        session_start_wall_time (float): wall-clock epoch of session start.
        on_soft_warning (callable):      async callback (no args) for 5-min event.
        on_hard_end     (callable):      async callback (no args) for 6-min event.
    """

    def __init__(self, session_start_wall_time: float):
        self.session_start_wall_time: float = session_start_wall_time
        # [Event Loop Delay Tolerance] Use monotonic clock to prevent CPU spike drift
        self._start_monotonic: float = time.monotonic()
        
        # Fallback for unit tests that pass an artificial wall time
        if session_start_wall_time is not None:
             fake_elapsed = time.time() - session_start_wall_time
             if fake_elapsed > 1.0:
                 self._start_monotonic -= fake_elapsed

        # ── Safety guards — CRITICAL for idempotency (STAB-12 §6) ─────────
        self._soft_warning_fired: bool = False
        self._hard_end_fired:     bool = False

        # ── Event callbacks (set by orchestrator) ─────────────────────────
        self.on_soft_warning = None   # async callable()
        self.on_hard_end     = None   # async callable()

        # ── Internal task handle ───────────────────────────────────────────
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event = asyncio.Event()

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Launch the background timing loop. Safe to call once per session."""
        if self._task and not self._task.done():
            logger.warning("[SessionTimer] start() called while already running — ignoring.")
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="session_timer")
        self._task.add_done_callback(self._on_task_done)
        logger.info(
            f"[SessionTimer] Started — soft_warning@{SOFT_WARNING_S:.0f}s, "
            f"hard_end@{HARD_END_S:.0f}s"
        )

    async def cancel(self) -> None:
        """Gracefully stop the timer loop."""
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("[SessionTimer] Cancelled.")

    # ─────────────────────────────────────────────────────────────────────────
    # Internal loop
    # ─────────────────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(TICK_S)

                # Use monotonic time to avoid drift if wall clock is adjusted by NTP or CPU spikes pause the event loop
                elapsed = time.monotonic() - self._start_monotonic

                # ── 5-min soft warning ─────────────────────────────────────
                # Guard: fire exactly once; ±15 s tolerance satisfied by 0.5 s tick.
                if elapsed >= SOFT_WARNING_S and not self._soft_warning_fired:
                    self._soft_warning_fired = True
                    logger.info(
                        f"[SessionTimer] Soft warning threshold reached "
                        f"(elapsed={elapsed:.1f}s). Firing on_soft_warning."
                    )
                    await self._invoke(self.on_soft_warning, "on_soft_warning")

                # ── 6-min hard end ─────────────────────────────────────────
                # Guard: fire exactly once; idempotent regardless of how many ticks pass.
                if elapsed >= HARD_END_S and not self._hard_end_fired:
                    self._hard_end_fired = True
                    logger.warning(
                        f"[SessionTimer] Hard-end threshold reached "
                        f"(elapsed={elapsed:.1f}s). Firing on_hard_end."
                    )
                    await self._invoke(self.on_hard_end, "on_hard_end")
                    # Hard end terminates the loop — no further timer work needed.
                    break

        except asyncio.CancelledError:
            logger.info("[SessionTimer] Loop cancelled via CancelledError.")
        except Exception as exc:
            logger.error(f"[SessionTimer] Unexpected error in timing loop: {exc}", exc_info=True)

    async def _invoke(self, callback, name: str) -> None:
        """Safely invoke an async callback; swallows exceptions so the loop never dies."""
        if callback is None:
            logger.debug(f"[SessionTimer] No handler registered for {name}.")
            return
        try:
            await callback()
        except Exception as exc:
            logger.error(f"[SessionTimer] Error in {name} handler: {exc}", exc_info=True)

    def _on_task_done(self, task: asyncio.Task) -> None:
        """Done-callback: log unexpected exceptions that escaped the loop."""
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"[SessionTimer] Background task ended with error: {exc}", exc_info=True)
