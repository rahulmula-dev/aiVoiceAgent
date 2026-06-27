"""
agent_logging/call_logger.py — crash-safe per-call event stream + summary.

``CallLogger`` is a drop-in subclass of ``logs.transcript_logger.TranscriptLogger``.
It keeps every public method the STT / LLM / TTS modules already use
(``log_user``, ``log_bot``, ``mark_user_finalized``, ``mark_llm_first_token``,
``mark_tts_first_audio``, ``close``), so no changes are needed at the call
sites — the orchestrator just instantiates ``CallLogger`` instead of
``TranscriptLogger``.

What ``CallLogger`` adds on top:

1. **Crash-safe append** — every event is written to
   ``logs/calls/<datetime>_<id>.events.jsonl`` the moment it happens. If the
   process dies mid-call, the JSONL file is still well-formed up to the last
   complete line, so post-mortem analysis still has the conversation up to
   the failure point. The existing ``logs/transcripts/<datetime>.json`` keeps
   working as before (parent's responsibility), preserving ``view_call.py``.

2. **Sealed summary** — on ``close()``, a small ``logs/calls/<datetime>_<id>.json``
   is written atomically (``.tmp`` then ``os.replace``). It contains:
     - duration, masked caller, turn counts
     - latency p50/p90/p95/p99/avg for the user-final → tts-first-audio leg
     - governance counts (language strikes, restricted-topic refusals)
   This is the file to scan when you want one-line-per-call analytics.

3. **Governance event recording** — extra method ``log_governance_*`` so the
   LLM loop can record when a strike or topic refusal fires. These do not
   land in the transcript JSON (which stays speaker/bot-only) — they go to
   the new events.jsonl and the summary stats.

The two destination directories don't overlap, so there is no risk of
clobbering the existing transcript writes:

    logs/
      transcripts/<datetime>.json          # existing TranscriptLogger, unchanged
      calls/<datetime>_<id>.events.jsonl   # NEW: append-as-it-happens
      calls/<datetime>_<id>.json           # NEW: sealed summary
      access_audit.jsonl                   # NEW: audit_logger writes here
"""

from __future__ import annotations

import json
import os
import statistics
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from logs.transcript_logger import TranscriptLogger


_CALLS_DIR = Path(__file__).resolve().parent.parent / "logs" / "calls"
_CALLS_DIR.mkdir(parents=True, exist_ok=True)

_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _safe_filename_id(call_id: str) -> str:
    """Sanitize a call_id into a filename-safe slug, capped at 32 chars."""
    return "".join(c if c.isalnum() else "_" for c in (call_id or ""))[:32] or "nocallid"


class CallLogger(TranscriptLogger):
    """
    Drop-in replacement for ``TranscriptLogger`` that also writes a crash-safe
    events JSONL and a sealed summary JSON.

    Constructor takes the same ``call_id`` as the parent, plus an optional
    ``caller_number_masked`` for the summary (use ``voice_logger.mask_phone``
    before passing).
    """

    def __init__(self, call_id: str, caller_number_masked: str = "<unknown>") -> None:
        super().__init__(call_id=call_id)

        # Use the parent's filename stamp so the .events.jsonl and the
        # transcript JSON share the same timestamp prefix (easier correlation).
        stamp = self._filename
        slug = _safe_filename_id(call_id)
        self._events_path = _CALLS_DIR / f"{stamp}_{slug}.events.jsonl"
        self._summary_path = _CALLS_DIR / f"{stamp}_{slug}.json"

        self._caller_masked = caller_number_masked
        self._call_start_mono = time.monotonic()
        self._latencies_ms: list[float] = []
        self._user_turns = 0
        self._bot_turns = 0
        self._lang_strikes = 0
        self._topic_refusals = 0

        self._append({
            "event": "call_start",
            "call_id": call_id,
            "caller": caller_number_masked,
        })

    # ── Internal append helper (thread-safe per-instance) ────────────────

    def _append(self, record: dict) -> None:
        record.setdefault("ts", _now_iso())
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with _LOCK:
            try:
                with self._events_path.open("a", encoding="utf-8") as f:
                    f.write(line)
            except Exception as e:
                # Logging must never break the call. Print once and move on.
                print(f"[CALL_LOGGER] events append failed: {e}")

    # ── Drop-in overrides — same signatures as TranscriptLogger ──────────

    def log_user(self, text: str) -> None:
        text = (text or "").strip()
        if not text or self._closed:
            return
        super().log_user(text)
        self._user_turns += 1
        self._append({"event": "user_turn", "text": text})

    def log_bot(self, text: str) -> None:
        text = (text or "").strip()
        if not text or self._closed:
            return
        super().log_bot(text)
        self._bot_turns += 1
        self._append({"event": "bot_turn", "text": text})

    def mark_tts_first_audio(self) -> None:
        if self._closed or self._t_user_final is None:
            super().mark_tts_first_audio()
            return
        # We want to also capture the latency in our own list, but the parent
        # method consumes the timers in place. Compute the same thing first.
        now = time.monotonic()
        latency_ms: float | None = None
        if self._t_user_final is not None and self._t_llm_first_token is not None:
            latency_ms = (now - self._t_user_final) * 1000.0
        super().mark_tts_first_audio()
        if latency_ms is not None:
            self._latencies_ms.append(latency_ms)
            self._append({
                "event": "latency",
                "user_final_to_tts_first_audio_ms": round(latency_ms),
            })

    # ── Governance hook methods (called from run_llm) ────────────────────

    def log_governance_lang_strike(
        self,
        strike: int,
        lang_code: str,
        confidence: float,
        terminated: bool,
    ) -> None:
        self._lang_strikes += 1
        self._append({
            "event": "gov_lang_strike",
            "strike": strike,
            "lang": lang_code,
            "confidence": round(confidence, 2),
            "terminated": terminated,
        })

    def log_governance_topic_refusal(self, category: str) -> None:
        self._topic_refusals += 1
        self._append({"event": "gov_topic_refusal", "category": category})

    # ── Sealed summary on close ──────────────────────────────────────────

    def close(self) -> None:
        if self._closed:
            return
        # Parent writes the transcript JSON and flips self._closed.
        end_mono = time.monotonic()
        duration_s = round(end_mono - self._call_start_mono, 2)
        summary = self._build_summary(duration_s)
        self._append({"event": "call_end", "duration_s": duration_s, **summary})
        super().close()  # writes logs/transcripts/<datetime>.json + sets _closed

        # Atomic sealed summary write
        tmp_path = self._summary_path.with_suffix(self._summary_path.suffix + ".tmp")
        try:
            with _LOCK:
                with tmp_path.open("w", encoding="utf-8") as f:
                    json.dump(summary, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self._summary_path)
            print(f"[LOG] Call summary saved -> {self._summary_path}")
        except Exception as e:
            print(f"[CALL_LOGGER] summary write failed: {e}")
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass

    # ── Summary builder ──────────────────────────────────────────────────

    def _build_summary(self, duration_s: float) -> dict[str, Any]:
        lat = sorted(self._latencies_ms)

        def pct(p: float) -> float | None:
            if not lat:
                return None
            k = max(0, min(len(lat) - 1, int(round((p / 100.0) * (len(lat) - 1)))))
            return round(lat[k], 1)

        return {
            "call_id": self.call_id,
            "caller": self._caller_masked,
            "started_at": self._started_at,
            "ended_at": _now_iso(),
            "duration_s": duration_s,
            "user_turns": self._user_turns,
            "bot_turns": self._bot_turns,
            "governance": {
                "language_strikes": self._lang_strikes,
                "topic_refusals": self._topic_refusals,
            },
            "latency_ms": {
                "count": len(lat),
                "p50": pct(50),
                "p90": pct(90),
                "p95": pct(95),
                "p99": pct(99),
                "avg": round(statistics.mean(lat), 1) if lat else None,
                "max": round(max(lat), 1) if lat else None,
            },
        }
