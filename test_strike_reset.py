# -*- coding: utf-8 -*-
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
"""
Test 4: State Machine Amnesia — Strike Reset Verification
==========================================================
Bypasses audio/STT entirely by directly calling _on_transcript on a mock orchestrator.
Sequence:
  Step 1 → Hindi text "नमस्ते"  ← should trigger Strike 1
  Step 2 → English "What programs does the college have?"  ← should RESET counter to 0
  Step 3 → Hindi text "नमस्ते"  ← should trigger Strike 1 again (NOT Strike 2)

Run: python test_strike_reset.py
"""
import asyncio
import sys
import os
import logging

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s | %(name)s | %(message)s")

# Ensure project root is on path
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dotenv import load_dotenv
load_dotenv()

from contracts.policy import ResponsePolicyEngine, PRDScripts


class MockOrchestrator:
    """Minimal stub that replicates the strike counter logic from manager.py."""

    def __init__(self):
        self.policy = ResponsePolicyEngine()
        self.language_strike_count = 0
        self.consecutive_empty_frames = 0
        self.user_has_spoken = False
        self.log = logging.getLogger("MockOrchestrator")

    async def _on_transcript(self, text: str, confidence: float = 0.99, is_final: bool = True, detected_lang: str = None):
        raw = text.strip()
        if not is_final or not raw:
            return

        self.user_has_spoken = True
        is_eng = self.policy._is_english(raw, detected_lang=detected_lang)

        if not is_eng:
            self.language_strike_count += 1
            self.log.warning(f"❌ Non-English detected. Strike = {self.language_strike_count}/3 | Text: '{raw}'")
            return

        # Valid English → reset
        if self.language_strike_count > 0:
            self.log.info(f"✅ Valid English received. Resetting strike counter {self.language_strike_count} → 0 | Text: '{raw}'")
            self.language_strike_count = 0
        else:
            self.log.info(f"✅ Valid English. Strike counter already 0 | Text: '{raw}'")


async def run_test():
    ORC = MockOrchestrator()
    print("\n" + "="*60)
    print("  TEST 4: State Machine Amnesia — Strike Reset")
    print("="*60)

    # Step 1: Hindi input → Strike 1
    print("\n[STEP 1] Injecting Hindi: 'namaste' (Devanagari)")
    await ORC._on_transcript("नमस्ते", confidence=0.99)
    assert ORC.language_strike_count == 1, f"FAIL: Expected strike=1, got {ORC.language_strike_count}"
    print(f"  → Strike counter: {ORC.language_strike_count}  Expected: 1  ✓")

    # Step 2: English input → Reset to 0
    print("\n[STEP 2] Injecting English: 'What programs does the college have?'")
    await ORC._on_transcript("What programs does the college have?", confidence=0.99)
    assert ORC.language_strike_count == 0, f"FAIL: Expected strike=0 after reset, got {ORC.language_strike_count}"
    print(f"  → Strike counter: {ORC.language_strike_count}  Expected: 0  ✓")

    # Step 3: Hindi again → Strike 1 (NOT 2)
    print("\n[STEP 3] Injecting Hindi again: 'namaste' (Devanagari)")
    await ORC._on_transcript("नमस्ते", confidence=0.99)
    assert ORC.language_strike_count == 1, f"FAIL: Expected strike=1 (reset worked), got {ORC.language_strike_count}"
    print(f"  → Strike counter: {ORC.language_strike_count}  Expected: 1  ✓")

    print("\n" + "="*60)
    print("  🎉 TEST 4 PASSED — State machine correctly resets on valid English")
    print("="*60 + "\n")


if __name__ == "__main__":
    asyncio.run(run_test())
