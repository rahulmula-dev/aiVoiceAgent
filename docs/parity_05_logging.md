# Step 5 — Logging upgrade (CallLogger + access audit + PII masking)

**Status:** ✅ done
**Risk to working pipeline:** LOW — additive everywhere. Existing
`TranscriptLogger` keeps writing `logs/transcripts/<datetime>.json`
unchanged, so `view_call.py` continues to work.
**Time:** ~2 hours

## Why

Before Step 5 the only operational record per call was
`logs/transcripts/<datetime>.json`, written only at call end. Three
practical gaps:

1. **Lost on crash.** If the process died mid-call, that call's transcript
   was gone — the file was only written on `close()`.
2. **No summary metrics.** Latency, turn counts, governance counts had to
   be eyeballed manually with `view_call.py`.
3. **Raw phone numbers** could leak into log lines (e.g. when we eventually
   read `From` from Twilio's start event).
4. **No audit trail** for sensitive endpoints (the `/api/call-status`
   webhook had no record of who called it or when).

Step 5 adds three small modules under `agent_logging/`, plus a one-line
wire-in at two call sites.

## What was added

| File | Role |
|---|---|
| `agent_logging/voice_logger.py` | `mask_phone(number)` — keep `+` + first 4 digits + last 2; mask the middle |
| `agent_logging/audit_logger.py` | `log_access(endpoint, status, action, role, ip, **extra)` — fsync'd append-only JSONL |
| `agent_logging/call_logger.py` | `CallLogger(TranscriptLogger)` — crash-safe `events.jsonl` + sealed summary `.json` with latency percentiles, turn counts, governance counts |

Plus two integration edits:

| File | Change |
|---|---|
| `orchestrator/manager.py` | Instantiate `CallLogger` (not `TranscriptLogger`); mask `caller_number` before storing in `CallContext` |
| `llm/groq_llm.py` | When governance fires, call `logger.log_governance_lang_strike()` / `log_governance_topic_refusal()` if the logger supports them (CallLogger does; bare TranscriptLogger doesn't — `hasattr` guard) |
| `telephony/server.py` | `/api/call-status` writes one `log_access(...)` line per Twilio status callback; the `From` field is masked before it lands in the audit |
| `logs/transcript_logger.py` | One-character fix: ASCII `->` instead of unicode `→` in the close-time print, which Windows cp1252 console can't encode (was crashing `super().close()` in CallLogger) |

## Directory layout after Step 5

```
logs/
  transcripts/<datetime>.json              # existing, unchanged — view_call.py uses this
  calls/<datetime>_<id>.events.jsonl       # NEW: append-as-it-happens, survives crashes
  calls/<datetime>_<id>.json               # NEW: atomic sealed summary on call end
  access_audit.jsonl                       # NEW: append-only, fsync'd, tamper-evident
```

## What's in a CallLogger sealed summary

```json
{
  "call_id": "MZ03602dffc4d553076e6e146ef32f4a10",
  "caller": "+1856*****50",
  "started_at": "2026-06-23T17:50:48.340+00:00",
  "ended_at":   "2026-06-23T17:51:29.704+00:00",
  "duration_s": 41.36,
  "user_turns": 6,
  "bot_turns":  7,
  "governance": {
    "language_strikes": 0,
    "topic_refusals": 1
  },
  "latency_ms": {
    "count": 6, "p50": 4422.0, "p90": 8438.0, "p95": 8438.0, "p99": 8438.0,
    "avg":   5210.5, "max": 8438.0
  }
}
```

Practical use: grep `logs/calls/*.json` for `governance.language_strikes >
0` or `latency_ms.p90 > 6000` to find weird calls without opening each
transcript.

## Drop-in compatibility — why STT/LLM/TTS didn't change

`CallLogger` is a subclass of `TranscriptLogger`. It overrides
`log_user`, `log_bot`, `mark_tts_first_audio`, and `close` to *also* write
the events.jsonl and summary, then delegates to the parent so the
existing transcript JSON keeps being written. Callers (STT, LLM, TTS,
orchestrator) see the same API. The orchestrator just changed one line:

```python
# Before
self.logger = TranscriptLogger(call_id=streamsid)

# After
self.logger = CallLogger(call_id=streamsid, caller_number_masked=caller_masked)
```

LLM additions for governance recording are guarded with `hasattr(...)`
so the LLM module can still be called with a plain TranscriptLogger or
`None` for testing.

## PII masking — what it looks like

| Input | Masked |
|---|---|
| `+18567165450` | `+1856*****50` |
| `+919116802635` | `+9191******35` |
| `""` / `None` | `<unknown>` |

Applied at two boundaries:

1. **CallContext / CallLogger summary**: the orchestrator masks
   `caller_number` before constructing `CallContext`, so the masked form is
   the only one that ever lives in memory or hits the summary JSON.
2. **Audit log**: `/api/call-status` masks the `From` field before
   `log_access(...)` writes it.

## Verification

- **All 6 changed/new files compile + import** cleanly.
- **Simulated call** with two turns, a topic refusal, and a language
  strike produced:
  - Well-formed events.jsonl (6 events including call_end)
  - Atomic sealed summary JSON with all the metrics correctly aggregated
  - Audit line with masked phone for `/api/call-status`
- **Live server** boots, `/healthz` 200, `/api/call-status` 200, audit
  file has the new line, `+18567165450` → `+1856*****50` in the audit
  `extra.From`.

## Known follow-ups (deferred — not in Step 5 scope)

- **`audit_logging/recorder.py`** (WAV recording of raw call audio for
  compliance). Adds Twilio µ-law → 16-bit PCM conversion + wave file
  rotation. Postponed; the current 4 streams already cover ops debugging.
- **`call_logger` PII redaction for transcripts** (mask numbers users
  speak aloud). Currently transcripts are saved as-spoken. A small pass
  with a regex would help when transcripts are exported.
- **Audit rotation** — `logs/access_audit.jsonl` grows unbounded. A
  daily-rotation or size-cap helper is trivial to add when traffic
  warrants it.

## What this unlocks

- **Latency regression detection.** When you test the multilingual
  governance call from Step 4, the new sealed summary will tell you
  whether `language=multi` shifted p50/p90 noticeably without you
  comparing files by eye.
- **CRM integration (deferred — other dev's domain).** The CallLogger
  summary is a natural payload for a CRM ticket — `caller`, `duration_s`,
  `user_turns`, `bot_turns`, `governance.*` are exactly the fields a CRM
  record wants.
- **Production triage.** When something goes wrong on a real call, you
  open the events.jsonl for that timestamp and replay every event in
  order; you don't have to reproduce the call.
