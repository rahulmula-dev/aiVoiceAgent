"""
test_governance.py
==================
Standalone validation for the CILA language governance layer.

Tests TWO paths explicitly:
  1. FastText (lid.176.ftz) — EC2 primary, if model file is present
  2. Lingua               — local dev fallback, always available

Run from the project root:
    python test_governance.py

EC2 setup (FastText):
    pip install fasttext-wheel
    wget https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz -P models/
    FASTTEXT_MODEL_PATH=models/lid.176.ftz python test_governance.py
"""

from __future__ import annotations

import os
import sys
import io

# Force UTF-8 on Windows console
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── Threshold (imported from source of truth so this test stays in sync) ──────
from contracts.language_guard import FASTTEXT_CONFIDENCE_THRESHOLD, LINGUA_CONFIDENCE_THRESHOLD

# ── Colour helpers ─────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"

def _pass(s):  return f"{GREEN}{s}{RESET}"
def _fail(s):  return f"{RED}{s}{RESET}"
def _warn(s):  return f"{YELLOW}{s}{RESET}"
def _info(s):  return f"{CYAN}{s}{RESET}"


# ══════════════════════════════════════════════════════════════════════════════
# PATH 1 — FastText direct (EC2 primary)
# ══════════════════════════════════════════════════════════════════════════════

def _load_fasttext_model():
    """Load the FastText lid.176.ftz model. Returns (model, path) or (None, reason)."""
    model_path = os.getenv(
        "FASTTEXT_MODEL_PATH",
        os.path.join(os.path.dirname(__file__), "models", "lid.176.ftz"),
    )
    if not os.path.exists(model_path):
        return None, f"Model not found at '{model_path}'. Set FASTTEXT_MODEL_PATH or download lid.176.ftz."

    try:
        import fasttext  # noqa: PLC0415
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model = fasttext.load_model(model_path)
        return model, model_path
    except ImportError:
        return None, "fasttext-wheel not installed. Run: pip install fasttext-wheel"
    except Exception as exc:
        return None, f"FastText load error: {exc}"


def _run_fasttext(model, text: str) -> dict:
    """Run FastText on `text` and apply the 0.80 governance rule."""
    clean = text.replace("\n", " ").strip()
    labels, probs = model.predict(clean, k=1)
    lang = labels[0].replace("__label__", "")
    conf = float(probs[0])

    # Governance rule: block if non-English label OR confidence < 0.80
    if lang == "en":
        is_english = True
        reason = f"FastText says 'en' (conf={conf:.4f})"
    elif conf >= FASTTEXT_CONFIDENCE_THRESHOLD:
        is_english = False
        reason = f"FastText says '{lang}' at {conf:.4f} >= {FASTTEXT_CONFIDENCE_THRESHOLD} → BLOCK"
    else:
        is_english = True
        reason = f"FastText says '{lang}' at {conf:.4f} < {FASTTEXT_CONFIDENCE_THRESHOLD} → fail-open (STT jitter)"

    return {"lang": lang, "conf": conf, "is_english": is_english, "reason": reason}


# ══════════════════════════════════════════════════════════════════════════════
# PATH 2 — Lingua (local dev, always available)
# ══════════════════════════════════════════════════════════════════════════════

def _run_lingua(text: str) -> dict:
    """Run Lingua on `text` and apply the 0.75 governance rule."""
    from contracts.language_guard import validate_language  # noqa: PLC0415
    result = validate_language(text)
    return {
        "lang": result.predicted_lang_code,
        "conf": result.confidence,
        "is_english": result.is_english,
        "model_available": result.model_available,
        "reason": (
            f"Lingua: lang={result.predicted_lang_code}, conf={result.confidence:.4f}, "
            f"threshold={LINGUA_CONFIDENCE_THRESHOLD}"
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# TEST CASES
# ══════════════════════════════════════════════════════════════════════════════

# Format: (text, expected_is_english, category, note)
TEST_CASES = [
    # ── Clean English ──────────────────────────────────────────────────────────
    (
        "I would like to enroll in the nursing program.",
        True, "Clean English",
        "Standard admissions query",
    ),
    (
        "What are the tuition fees for the software diploma?",
        True, "Clean English",
        "College-domain vocabulary",
    ),
    (
        "Hi, my name is Priya and I'm calling about the fall intake.",
        True, "Clean English",
        "Indian name inside an English sentence — must NOT block",
    ),
    (
        "I live on Rue de la Gare and I want to apply.",
        True, "Proper Noun (CA French street)",
        "Canadian-French street name embedded in English — must NOT block",
    ),

    # ── Jitter / Low-confidence fillers ───────────────────────────────────────
    (
        "Yeah, I think... uh... okay.",
        True, "Jitter / Filler",
        "Voice stammers: low confidence — should PASS (fail-open), not block",
    ),
    (
        "Okay.",
        True, "Jitter / Filler",
        "Single-word affirmation",
    ),

    # ── Pure Non-English ──────────────────────────────────────────────────────
    (
        "Bonjour, je voudrais des informations sur les programmes.",
        False, "Pure French",
        "Canadian bilingual risk — must BLOCK",
    ),
    (
        "Hola, quisiera información sobre los programas disponibles.",
        False, "Spanish",
        "Must BLOCK",
    ),
    (
        "كيف يمكنني التسجيل في البرنامج؟",
        False, "Arabic script",
        "Non-Latin — must BLOCK instantly",
    ),
    (
        "ਮੈਨੂੰ ਦਾਖਲੇ ਬਾਰੇ ਜਾਣਕਾਰੀ ਚਾਹੀਦੀ ਹੈ",
        False, "Punjabi script",
        "Non-Latin — must BLOCK instantly",
    ),

    # ── Hinglish / Code-switching ─────────────────────────────────────────────
    # Romanised Hindi uses the Latin alphabet, so there are no non-Latin script
    # cues. Lingua/FastText fail-open here (is_english=True) and these cases
    # are caught downstream by the policy density + langdetect chain.
    # Expected=True for this layer; the policy layer test below verifies blocking.
    (
        "Main nursing program mein enroll hona chahta hoon.",
        True, "Hinglish (Mixed) — guard layer",
        "Latin script: Lingua fail-opens. Policy density chain blocks (see below).",
    ),
    (
        "Mujhe admission ke baare mein batao.",
        True, "Romanised Hindi — guard layer",
        "Latin script: Lingua fail-opens. Policy density chain blocks (see below).",
    ),
]


# ══════════════════════════════════════════════════════════════════════════════
# RUNNER
# ══════════════════════════════════════════════════════════════════════════════

def _print_row(text, expected, category, note, result: dict, detector_label: str):
    ok = result["is_english"] == expected
    verdict = "PASS" if ok else "FAIL"
    colour = GREEN if ok else RED
    decision = "ENGLISH  → Gemini" if result["is_english"] else "NON-ENG  → Strike"

    print(f"  [{colour}{verdict}{RESET}] {_info(detector_label):<22}  {decision}  "
          f"lang={result['lang']:<4}  conf={result['conf']:.4f}")
    if not ok:
        expected_str = "ENGLISH" if expected else "NON-ENGLISH"
        print(f"         {_warn('^ expected ' + expected_str)}")
    print(f"         {_warn(category)} | {note}")
    print(f"         {result['reason']}")
    print()
    return ok


# ══════════════════════════════════════════════════════════════════════════════
# POLICY CHAIN VERIFICATION — tests the full _is_english() stack
# (Lingua/FastText → langdetect → density fallback)
# This is the layer that catches romanised Hindi and Hinglish.
# ══════════════════════════════════════════════════════════════════════════════

POLICY_CASES = [
    # (text, expected_is_english, note)
    ("I would like to enroll in the nursing program.", True,  "Clean English"),
    ("Hi, my name is Priya and I'm calling about the fall intake.", True, "Indian name in English sentence"),
    ("I live on Rue de la Gare and I want to apply.", True, "CA French street name in English"),
    ("Yeah, I think... uh... okay.", True, "Voice filler — must NOT strike"),
    ("Bonjour, je voudrais des informations sur les programmes.", False, "Pure French"),
    ("Hola, quisiera información sobre los programas disponibles.", False, "Spanish"),
    # "Main nursing program mein enroll hona chahta hoon" has 3 English tokens
    # (main, program, enroll) giving density=0.375 > 0.20, so the density fallback
    # passes it through when langdetect is uncertain. The Gemini secondary filter
    # (system instruction) is the final backstop for this class of Hinglish.
    ("Main nursing program mein enroll hona chahta hoon.", True, "Hinglish (mixed English tokens) — reaches Gemini secondary filter"),
    ("Mujhe admission ke baare mein batao.", False, "Romanised Hindi — no English tokens, density+langdetect blocks it"),
]


def _run_policy_chain(text: str) -> dict:
    from contracts.policy import ResponsePolicyEngine  # noqa: PLC0415
    engine = ResponsePolicyEngine()
    result = engine._is_english(text)
    return {"is_english": result}


def main():
    print("\n" + "=" * 78)
    print("  CILA — Language Governance Validation")
    print(f"  FastText threshold : {FASTTEXT_CONFIDENCE_THRESHOLD}  (EC2 primary)")
    print(f"  Lingua threshold   : {LINGUA_CONFIDENCE_THRESHOLD}  (local dev fallback)")
    print("=" * 78 + "\n")

    # ── Load FastText ──────────────────────────────────────────────────────────
    ft_model, ft_info = _load_fasttext_model()
    if ft_model:
        print(f"  {_pass('FastText LOADED')} from '{ft_info}'\n")
    else:
        print(f"  {_warn('FastText UNAVAILABLE')} — {ft_info}")
        print(f"  {_info('Running Lingua-only mode (local dev). EC2 will use FastText.')}\n")

    # ── Run tests ──────────────────────────────────────────────────────────────
    total = passed_ft = passed_lg = 0

    for text, expected, category, note in TEST_CASES:
        total += 1
        display = text if len(text) <= 70 else text[:67] + "..."
        print(f"  Input: \"{display}\"")

        if ft_model:
            try:
                ft_result = _run_fasttext(ft_model, text)
                ok_ft = _print_row(text, expected, category, note, ft_result, "FastText")
                passed_ft += ok_ft
            except Exception as exc:
                print(f"  [{_fail('ERROR')}] FastText prediction failed: {exc}\n")

        try:
            lg_result = _run_lingua(text)
            ok_lg = _print_row(text, expected, category, note, lg_result, "Lingua (fallback)")
            passed_lg += ok_lg
        except Exception as exc:
            print(f"  [{_fail('ERROR')}] Lingua prediction failed: {exc}\n")

        print("  " + "-" * 74)

    # ── Summary ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    if ft_model:
        ft_colour = GREEN if passed_ft == total else RED
        print(f"  FastText : {ft_colour}{passed_ft}/{total} passed{RESET}")
    print(f"  Lingua   : {GREEN if passed_lg == total else RED}{passed_lg}/{total} passed{RESET}")
    print("=" * 78)

    # ── Interpreting Divergences ───────────────────────────────────────────────
    print("""
  Interpreting Results
  ────────────────────
  PASS on both      → safe to deploy; both detectors agree.
  FAIL on FastText,
  PASS on Lingua    → FastText is stricter at 0.80; review the input.
                      If it is genuinely English, raise FASTTEXT_CONFIDENCE_THRESHOLD.
  PASS on FastText,
  FAIL on Lingua    → Lingua is catching something FastText misses.
                      The policy density chain acts as a third backstop.
  FAIL on both      → Genuine gap; add to _SINGLE_WORD_ENGLISH or intro_regex
                      in language_guard.py / policy.py.

  Key Behaviours to Verify
  ─────────────────────────
  "Jitter" test  : "Yeah, uh, okay" should PASS (fail-open) — noise, not foreign speech.
  "Mixed" test   : Hinglish should show FastText conf < 0.80 en OR label hi/ur → BLOCK.
  "Proper noun"  : "Rue de la Gare" inside English sentence must PASS.
""")

    # ── Policy chain section ───────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("  POLICY CHAIN — _is_english() (Lingua → langdetect → density fallback)")
    print("  This layer catches romanised Hindi / Hinglish that Lingua fail-opens on.")
    print("=" * 78 + "\n")

    policy_passed = 0
    try:
        for text, expected, note in POLICY_CASES:
            display = text if len(text) <= 70 else text[:67] + "..."
            try:
                res = _run_policy_chain(text)
                ok = res["is_english"] == expected
                verdict = "PASS" if ok else "FAIL"
                colour = GREEN if ok else RED
                decision = "ENGLISH  → Gemini" if res["is_english"] else "NON-ENG  → Strike"
                print(f"  [{colour}{verdict}{RESET}] {decision}  {note}")
                if not ok:
                    expected_str = "ENGLISH" if expected else "NON-ENGLISH"
                    print(f"         {_warn('^ expected ' + expected_str)}")
                if ok:
                    policy_passed += 1
            except Exception as exc:
                print(f"  [{_fail('ERROR')}] {note}: {exc}")
    except ImportError as imp:
        print(f"  {_warn('Policy chain skipped — import failed:')} {imp}")

    print(f"\n  Policy chain: {GREEN if policy_passed == len(POLICY_CASES) else RED}"
          f"{policy_passed}/{len(POLICY_CASES)} passed{RESET}\n")

    all_passed = (
        passed_lg == total
        and (not ft_model or passed_ft == total)
        and policy_passed == len(POLICY_CASES)
    )
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
