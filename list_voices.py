"""
Utility: inspect what the ElevenLabs API key in .env can do.

Free tier accounts can only call voices with category == "premade" via the API.
Library / shared voices return HTTP 402 even with characters remaining.
Scoped API keys may also be restricted to a specific voice list.

Usage:
    python list_voices.py
"""

import httpx
import config


_PREMADE_VOICES = [
    ("21m00Tcm4TlvDq8ikWAM", "Rachel",   "calm female,  US"),
    ("pNInz6obpgDQGcFmaJgB", "Adam",     "deep male,    US"),
    ("EXAVITQu4vr4xnSDxMaL", "Sarah",    "soft female,  US"),
    ("nPczCjzI2devNBz1zQrb", "Brian",    "warm male,    US"),
    ("9BWtsMINqrJLrRacOk9x", "Aria",     "expressive,   US"),
    ("CwhRBWXzGAHq8TQ4Fs17", "Roger",    "confident,    US"),
    ("FGY2WhTYpPnrIDTdsKH5", "Laura",    "upbeat,       US"),
    ("IKne3meq5aSn9XLyUdCD", "Charlie",  "casual,       AU"),
    ("JBFqnCBsd6RMkjVDRZzb", "George",   "warm,         GB"),
    ("XB0fDUnXU5powFXDhCwa", "Charlotte","seductive,    EN"),
    ("onwK4e9ZLuTAKqWW03F9", "Daniel",   "authoritative,GB"),
    ("pFZP5JQG7iQjIQuC4Bku", "Lily",     "warm female,  GB"),
]


def _print_premade_table() -> None:
    print("\nKnown ElevenLabs premade voice IDs (callable on Free tier, no")
    print("voices_read permission required, no Voice Library access required):\n")
    print(f"  {'voice_id':<32}  {'name':<10}  description")
    print(f"  {'-'*32:<32}  {'-'*10:<10}  {'-'*30}")
    for vid, name, desc in _PREMADE_VOICES:
        print(f"  {vid:<32}  {name:<10}  {desc}")
    print()
    print("Set ELEVENLABS_VOICE_ID in .env to any of these, e.g.:")
    print("  ELEVENLABS_VOICE_ID=21m00Tcm4TlvDq8ikWAM")
    print()
    print("Dashboard view: https://elevenlabs.io/app/voice-lab")


def main() -> None:
    if not config.ELEVENLABS_API_KEY:
        print("❌ ELEVENLABS_API_KEY missing from .env")
        return

    headers = {"xi-api-key": config.ELEVENLABS_API_KEY}

    # 1) User / subscription info — tells us about quota and tier
    try:
        u = httpx.get("https://api.elevenlabs.io/v1/user", headers=headers, timeout=15)
        if u.status_code == 200:
            sub = u.json().get("subscription", {})
            print(f"\nAccount tier      : {sub.get('tier', 'unknown')}")
            print(f"Characters used   : {sub.get('character_count', '?')}")
            print(f"Character limit   : {sub.get('character_limit', '?')}")
            print(f"Next reset (epoch): {sub.get('next_character_count_reset_unix', '?')}")
        elif u.status_code == 401:
            print("\n⚠  API key is scoped — `/v1/user` returned 401.")
            print(f"   Detail: {u.text}")
        else:
            print(f"\n/v1/user returned {u.status_code}: {u.text}")
    except Exception as e:
        print(f"\nCould not query /v1/user: {e}")

    # 2) Voices list — the key may or may not have voices_read
    print("\nTrying /v1/voices ...")
    try:
        v = httpx.get("https://api.elevenlabs.io/v1/voices", headers=headers, timeout=15)
    except Exception as e:
        print(f"   request failed: {e}")
        _print_premade_table()
        return

    if v.status_code == 200:
        voices = v.json().get("voices", [])
        by_cat: dict[str, list[dict]] = {}
        for voice in voices:
            by_cat.setdefault(voice.get("category", "unknown"), []).append(voice)

        print(f"\n{len(voices)} voices on this account:\n")
        for cat in sorted(by_cat):
            tag = "✅ Free-tier API safe" if cat == "premade" else "⚠  may 402 on Free tier"
            print(f"── {cat.upper()}  ({tag}) ──")
            for voice in sorted(by_cat[cat], key=lambda x: x.get("name", "")):
                print(f"  {voice.get('voice_id'):<32}  {voice.get('name')}")
            print()
        return

    # Fallback: scoped key, no voices_read — show the curated premade list
    print(f"   /v1/voices returned {v.status_code}")
    print(f"   {v.text}")
    print()
    print("The API key is scoped without `voices_read` permission. That's fine —")
    print("the premade voices below are still callable, you just can't enumerate")
    print("them via the API. Pick one and put it in .env.")
    _print_premade_table()

    print("If your key is ALSO restricted to specific voice IDs (the manager may")
    print("have set this), then only those listed in the dashboard under")
    print("'Allowed voices' for the key will work — ask for the list, or have a")
    print("key issued with `text_to_speech` + at least 'all premade voices'.")


if __name__ == "__main__":
    main()
