"""
Pretty-print a single call transcript from logs/transcripts/.

Usage:
    python view_call.py                        # show the most recent call
    python view_call.py <filename.json>        # show a specific file
    python view_call.py <partial-call-id>      # find by call_id prefix
"""

import json
import sys
from pathlib import Path

_TRANSCRIPTS_DIR = Path(__file__).resolve().parent / "logs" / "transcripts"


def _resolve_path(arg: str | None) -> Path | None:
    """Pick which transcript JSON to display."""
    if not _TRANSCRIPTS_DIR.exists():
        return None

    files = sorted(_TRANSCRIPTS_DIR.glob("*.json"))
    if not files:
        return None

    if arg is None:
        return files[-1]

    # Exact path / filename match
    candidate = Path(arg)
    if candidate.exists():
        return candidate
    candidate = _TRANSCRIPTS_DIR / arg
    if candidate.exists():
        return candidate
    candidate = _TRANSCRIPTS_DIR / f"{arg}.json"
    if candidate.exists():
        return candidate

    # Search by call_id prefix inside the JSON
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("call_id", "").startswith(arg):
            return f
    return None


def _hhmmss(iso_ts: str) -> str:
    """ISO 8601 → HH:MM:SS."""
    return iso_ts[11:19] if len(iso_ts) >= 19 else iso_ts


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    path = _resolve_path(arg)
    if path is None:
        print("No matching transcript found in logs/transcripts/")
        sys.exit(1)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ Could not read {path}: {e}")
        sys.exit(1)

    call_id = data.get("call_id", "?")
    started = data.get("started_at", "?")
    ended   = data.get("ended_at", "(call still in progress)")
    events  = data.get("events", [])

    print(f"=== Call {call_id} ===")
    print(f"File:    {path}")
    print(f"Started: {started}")
    print(f"Ended:   {ended}")
    print()

    user_count = bot_count = timing_count = 0
    timings: list[dict] = []

    for ev in events:
        ts = _hhmmss(ev.get("ts", ""))
        if ev.get("event") == "timing":
            timing_count += 1
            timings.append(ev)
            print(
                f"[{ts}] ⏱  "
                f"user→llm: {ev.get('user_final_to_llm_first_token_ms', '?')}ms, "
                f"llm→tts: {ev.get('llm_first_token_to_tts_first_audio_ms', '?')}ms, "
                f"total: {ev.get('user_final_to_tts_first_audio_ms', '?')}ms"
            )
        elif ev.get("speaker") == "user":
            user_count += 1
            print(f"[{ts}] USER: {ev.get('text','')}")
        elif ev.get("speaker") == "bot":
            bot_count += 1
            print(f"[{ts}] BOT:  {ev.get('text','')}")

    print()
    print(f"--- Summary: {user_count} user turn(s), {bot_count} bot turn(s), {timing_count} timing event(s) ---")

    if timings:
        total = [t["user_final_to_tts_first_audio_ms"] for t in timings if "user_final_to_tts_first_audio_ms" in t]
        llm   = [t["user_final_to_llm_first_token_ms"]  for t in timings if "user_final_to_llm_first_token_ms"  in t]
        tts   = [t["llm_first_token_to_tts_first_audio_ms"] for t in timings if "llm_first_token_to_tts_first_audio_ms" in t]
        if total:
            print(f"    avg total (user-final → first-audio-out): {sum(total) // len(total)}ms  (min {min(total)}ms / max {max(total)}ms)")
        if llm:
            print(f"    avg LLM TTFT (user-final → first token):  {sum(llm)   // len(llm)}ms")
        if tts:
            print(f"    avg TTS first byte (token → first audio): {sum(tts)   // len(tts)}ms")


if __name__ == "__main__":
    main()
