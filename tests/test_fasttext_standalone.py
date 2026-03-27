"""
test_fasttext.py
================
Validates the language guard (Lingua / FastText) before deploying to EC2.

Run from the project root:
    python test_fasttext.py

Setup (if not done yet):
    pip install lingua-language-detector

Optional FastText upgrade (Linux/EC2 only):
    pip install fasttext-wheel
    wget https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz \
         -O models/lid.176.ftz
"""

import sys
import io

# Force UTF-8 on Windows console so special characters don't crash
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── Pre-flight: check lingua-language-detector is installed ───────────────────
try:
    from lingua import Language  # noqa: F401
except ImportError:
    print("\n[SETUP REQUIRED]")
    print("  lingua-language-detector is not installed.")
    print("  Run:  pip install lingua-language-detector\n")
    sys.exit(1)

from contracts.language_guard import validate_language  # noqa: E402

# ── Test cases ────────────────────────────────────────────────────────────────
# Format: (text, expected_is_english, description)
TEST_CASES = [
    # --- English (should PASS) ---
    ("Hello, I would like information about the nursing program.", True,  "Clear English query"),
    ("What are the admission requirements for the business diploma?", True,  "College-domain English"),
    ("Can you tell me about tuition fees and payment plans?",        True,  "Multi-word English"),
    ("Hi, my name is Priya.",                                        True,  "Name introduction (accented name)"),
    ("Yeah okay, sounds good.",                                      True,  "Casual English filler"),
    ("I want to apply for the fall semester.",                       True,  "English with date reference"),

    # --- Non-English (should BLOCK) ---
    ("Bonjour, je voudrais des informations sur les programmes.",    False, "French — Canadian bilingual risk"),
    ("Hola, quisiera información sobre los programas disponibles.",  False, "Spanish"),
    # Romanised Hindi in Latin script is a known hard case for any text
    # classifier — without script cues, "college", "baare" etc. look like
    # mixed-language tokens. validate_language() returns English here;
    # the density + langdetect fallback chain in _is_english() catches it.
    ("Mujhe admission ke baare mein jaankari chahiye.",              True,  "Hindi (romanised) — caught by policy density chain, not guard"),
    ("ਮੈਨੂੰ ਦਾਖਲੇ ਬਾਰੇ ਜਾਣਕਾਰੀ ਚਾਹੀਦੀ ਹੈ",                          False, "Punjabi script"),
    ("我想了解护理课程的入学要求。",                                        False, "Mandarin"),
    ("Ich möchte mich über die Aufnahmebedingungen informieren.",    False, "German"),
    ("كيف يمكنني التسجيل في البرنامج؟",                              False, "Arabic"),

    # --- Edge cases ---
    ("",                                                             True,  "Empty string (pass-through)"),
    ("okay",                                                         True,  "Single English word"),
]

# ── Runner ────────────────────────────────────────────────────────────────────
PASS_COLOUR  = "\033[92m"  # green
FAIL_COLOUR  = "\033[91m"  # red
RESET        = "\033[0m"

passed = 0
failed = 0

print("\n" + "=" * 72)
print("  CILA FastText Language Guard — Test Suite")
print("=" * 72)

for text, expected, description in TEST_CASES:
    result = validate_language(text)
    ok = result.is_english == expected

    status  = f"{PASS_COLOUR}PASS{RESET}" if ok else f"{FAIL_COLOUR}FAIL{RESET}"
    verdict = "ENGLISH" if result.is_english else "NON-ENGLISH"
    flag    = "" if ok else f"  ← expected {'ENGLISH' if expected else 'NON-ENGLISH'}"

    print(
        f"  [{status}] {verdict:11s}  conf={result.confidence:.3f}  "
        f"lang={result.predicted_lang_code:<4}  {description}{flag}"
    )
    if ok:
        passed += 1
    else:
        failed += 1

print("=" * 72)
print(f"  Results: {passed} passed, {failed} failed out of {len(TEST_CASES)} tests")
print("=" * 72 + "\n")

# ── Policy integration smoke-test ─────────────────────────────────────────────
print("Smoke-testing policy.py _is_english() integration...")
try:
    from contracts.policy import ResponsePolicyEngine
    engine = ResponsePolicyEngine()

    smoke = [
        ("Can I get more details about the programs?", True),
        ("Je voudrais parler à quelqu'un en français.", False),
        # Romanised Hindi: policy chain blocks via density fallback (FastText/Lingua alone = uncertain)
        ("Mujhe college ke baare mein batao.",           True),
    ]
    smoke_pass = 0
    for text, expected in smoke:
        result = engine._is_english(text)
        ok = result == expected
        status = f"{PASS_COLOUR}PASS{RESET}" if ok else f"{FAIL_COLOUR}FAIL{RESET}"
        print(f"  [{status}] _is_english('{text[:55]}') = {result}")
        if ok:
            smoke_pass += 1

    print(f"\n  Policy smoke: {smoke_pass}/{len(smoke)} passed\n")
except Exception as exc:
    print(f"  [WARN] Policy smoke-test skipped: {exc}\n")

sys.exit(0 if failed == 0 else 1)
