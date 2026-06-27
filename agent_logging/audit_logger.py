"""
agent_logging/audit_logger.py — append-only access audit.

A single function, ``log_access()``, writes one JSON line per event to
``logs/access_audit.jsonl``. The file is append-only and never rewritten —
this gives a tamper-evident trail of every hit to sensitive endpoints
(currently ``/api/call-status``; ``/admin/*`` routes when those land in a
later step).

Each line is a small flat record:

    {"ts": "2026-06-23T17:00:00.000+00:00", "endpoint": "/api/call-status",
     "status": "received", "action": "call_status_callback",
     "role": "twilio", "ip": "54.x.y.z", "extra": {...}}

Why a separate logger (vs. the per-call CallLogger):
  * Different lifetime: audit lives across calls / restarts; CallLogger is
    per-call.
  * Different consumers: audit is for compliance / SIEM ingest; CallLogger
    is for debugging one call.
  * Different durability: audit can never lose a line — we ``fsync`` after
    each write. CallLogger trades that for speed (it appends but does not
    fsync each event).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_AUDIT_FILE = Path(__file__).resolve().parent.parent / "logs" / "access_audit.jsonl"
_AUDIT_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def log_access(
    endpoint: str,
    status: str,
    action: str = "",
    role: str = "anonymous",
    ip: str = "unknown",
    **extra: Any,
) -> None:
    """
    Append a single audit record. Cheap, synchronous, fsync'd.

    Parameters
    ----------
    endpoint
        The route the event is about (e.g. ``"/api/call-status"``).
    status
        Outcome: ``"received"``, ``"rejected"``, ``"error"``, etc.
    action
        Human-readable verb (e.g. ``"call_status_callback"``).
    role
        Who initiated the request: ``"twilio"``, ``"admin"``, ``"anonymous"``.
    ip
        Client IP if known.
    extra
        Arbitrary additional key/value pairs (e.g. CallSid, CallDuration).
    """
    _AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": _now_iso(),
        "endpoint": endpoint,
        "status": status,
        "action": action,
        "role": role,
        "ip": ip,
    }
    if extra:
        record["extra"] = extra

    line = json.dumps(record, ensure_ascii=False) + "\n"
    with _AUDIT_LOCK:
        # Open / append / fsync for each line. The cost is small (low write
        # rate on this file) and the durability guarantee is worth it for an
        # audit trail.
        with _AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
