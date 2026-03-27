"""
contracts/language_guard.py
===========================
Lingua-based language validator — the Circuit Breaker between Deepgram STT
and the Gemini LLM.

Architecture
------------
Deepgram (audio) → [Lingua text gate] → [langdetect fallback] → Gemini

Lingua is a pure-Python language detector designed specifically to be more
accurate than langdetect on short, real-world utterances. No C++ compiler
or model file download required — it works out of the box on all platforms.

Why Lingua over FastText on Windows?
    FastText requires Microsoft C++ Build Tools and has no pre-built wheel
    for Python 3.13 on Windows. Lingua is pure Python and installs instantly
    while delivering comparable (often better) accuracy on short voice
    transcripts.

EC2 deployment note
-------------------
    FastText (lid.176.ftz) remains available as an optional upgrade. If the
    environment variable FASTTEXT_MODEL_PATH points to a valid .ftz file AND
    the fasttext-wheel package is installed, this module will use FastText
    automatically. Otherwise it falls back to Lingua transparently.

    EC2 setup for FastText (optional, Linux only):
        pip install fasttext-wheel
        wget https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz \
             -O models/lid.176.ftz

Dependencies (always required)
-------------------------------
    pip install lingua-language-detector

Optional (Linux/Mac EC2 upgrade)
---------------------------------
    pip install fasttext-wheel
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("LanguageGuard")

# ── Constants ──────────────────────────────────────────────────────────────────

# FastText threshold: 0.80 (stricter for voice).
# STT pipelines force-map non-English phonemes to English words with artificially
# low confidence. Requiring 0.80 means we only fail-open on genuinely ambiguous
# inputs; anything confidently non-English (>=0.80) is blocked.
FASTTEXT_CONFIDENCE_THRESHOLD: float = 0.80

# Lingua threshold: 0.75 (standard).
# Lingua's confidence scale differs from FastText — 0.75 is already conservative
# for pure-Python text-based detection on short voice transcripts.
LINGUA_CONFIDENCE_THRESHOLD: float = 0.75

# Legacy alias kept for any external callers that import this name directly.
CONFIDENCE_THRESHOLD: float = FASTTEXT_CONFIDENCE_THRESHOLD

# ── FastText optional singleton ────────────────────────────────────────────────

_fasttext_model = None
_fasttext_init_attempted = False

_DEFAULT_MODEL_PATH = Path(__file__).parent.parent / "models" / "lid.176.ftz"


def _try_load_fasttext():
    """
    Attempt to load the FastText model exactly once per process.
    Returns the model if available, else None.
    This is a best-effort upgrade — failure is silent and non-fatal.
    """
    global _fasttext_model, _fasttext_init_attempted
    if _fasttext_init_attempted:
        return _fasttext_model
    _fasttext_init_attempted = True

    model_path = Path(os.getenv("FASTTEXT_MODEL_PATH", str(_DEFAULT_MODEL_PATH)))
    if not model_path.exists():
        return None  # No model file — use Lingua

    try:
        import fasttext  # noqa: PLC0415
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            _fasttext_model = fasttext.load_model(str(model_path))
        logger.info(f"[LanguageGuard] FastText model loaded from '{model_path}'. Using FastText as primary detector.")
    except ImportError:
        pass  # fasttext-wheel not installed — fall through to Lingua
    except Exception as exc:
        logger.warning(f"[LanguageGuard] FastText load failed ({exc}). Falling back to Lingua.")

    return _fasttext_model


# ── Lingua singleton ───────────────────────────────────────────────────────────

_lingua_detector = None


def _get_lingua_detector():
    """
    Lazy-load the Lingua detector exactly once per process.
    Loads only English + the most common non-English languages heard in a
    Canadian college context to keep memory usage minimal.
    """
    global _lingua_detector
    if _lingua_detector is not None:
        return _lingua_detector

    try:
        from lingua import Language, LanguageDetectorBuilder  # noqa: PLC0415

        # Include English + languages most commonly spoken in Canadian colleges:
        # French (bilingual country), Punjabi, Hindi, Mandarin, Arabic, Spanish,
        # Portuguese, Tagalog, Korean, German, Urdu, Bengali.
        # Lingua accuracy improves when the target set is smaller.
        # Note: KANNADA is not available in lingua-language-detector v2.x.
        _lingua_detector = (
            LanguageDetectorBuilder
            .from_languages(
                Language.ENGLISH,
                Language.FRENCH,
                Language.PUNJABI,
                Language.HINDI,
                Language.CHINESE,
                Language.ARABIC,
                Language.SPANISH,
                Language.PORTUGUESE,
                Language.TAGALOG,
                Language.KOREAN,
                Language.GERMAN,
                Language.URDU,
                Language.TAMIL,
                Language.TELUGU,
                Language.MARATHI,
                Language.GUJARATI,
                Language.BENGALI,
                Language.MALAY,
            )
            .with_minimum_relative_distance(0.15)
            .build()
        )
        logger.info("[LanguageGuard] Lingua detector initialised (19-language set).")
    except ImportError:
        logger.error(
            "[LanguageGuard] 'lingua-language-detector' is not installed. "
            "Run: pip install lingua-language-detector"
        )
    except Exception as exc:
        logger.error(f"[LanguageGuard] Lingua init failed: {exc}")

    return _lingua_detector


# ── Module-level fast-path word set ────────────────────────────────────────────
# Defined here (not inside validate_language) so it is built exactly once per
# process rather than re-created on every STT frame call.
SINGLE_WORD_ENGLISH: frozenset = frozenset({
    "ok", "okay", "yes", "yeah", "yep", "yup", "no", "nope", "hi", "hey",
    "hello", "thanks", "thank", "sure", "right", "fine", "good", "great",
    "alright", "correct", "exactly", "agreed", "understood", "got", "noted",
    "bye", "goodbye", "later", "wait", "hold", "stop", "go", "help",
    "please", "sorry", "pardon", "what", "how", "when", "where", "why",
    "who", "which", "hmm", "uh", "um", "ah", "oh", "mhm", "mhmm",
})

# ── Public API ─────────────────────────────────────────────────────────────────

class LanguageValidationResult:
    """
    Structured result from validate_language().

    Attributes
    ----------
    is_english          : True if the detector is confident the text is English.
    predicted_label     : Normalised label string, e.g. 'en', 'fr', 'hi'.
    predicted_lang_code : Alias for predicted_label (kept for API compatibility).
    confidence          : Detector confidence score (0.0–1.0).
                          For Lingua, 1.0 = definite, 0.0 = unknown.
    model_available     : False when no detector could be initialised.
                          Callers must treat this as pass-through (don't block).
    """

    __slots__ = (
        "is_english",
        "predicted_label",
        "predicted_lang_code",
        "confidence",
        "model_available",
    )

    def __init__(
        self,
        is_english: bool,
        predicted_label: str,
        confidence: float,
        model_available: bool,
    ):
        self.is_english = is_english
        self.predicted_label = predicted_label
        self.predicted_lang_code = predicted_label
        self.confidence = confidence
        self.model_available = model_available

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"LanguageValidationResult("
            f"is_english={self.is_english}, "
            f"lang={self.predicted_lang_code}, "
            f"confidence={self.confidence:.3f}, "
            f"model_available={self.model_available})"
        )


def validate_language(text: str) -> LanguageValidationResult:
    """
    Language Circuit Breaker — classify the language of `text`.

    Detection priority
    ------------------
    1. FastText (lid.176.ftz)  — if installed + model file present (EC2)
    2. Lingua                  — pure Python, always available (local dev + EC2)

    Decision logic
    --------------
    ENGLISH     : detected language is English AND confidence >= 0.75
    NON-ENGLISH : detected language is not English OR confidence < 0.75

    When no detector is available the function returns is_english=True
    (fail-open) so downstream callers are never blocked by a missing library.

    Parameters
    ----------
    text : Transcript string received from Deepgram.

    Returns
    -------
    LanguageValidationResult
    """
    if not text or not text.strip():
        return LanguageValidationResult(
            is_english=True,
            predicted_label="en",
            confidence=1.0,
            model_available=True,
        )

    # ── Fast-path: common single-word English inputs ───────────────────────────
    # Language detectors are unreliable on single words. "okay", "yes", "hi"
    # are valid in many languages, causing false non-English classifications
    # (e.g. Lingua classifies "okay" as Tagalog). Use the module-level constant
    # (built once per process, not per call).
    stripped = text.strip().lower().rstrip(".,!?")
    if stripped in SINGLE_WORD_ENGLISH:
        return LanguageValidationResult(
            is_english=True,
            predicted_label="en",
            confidence=1.0,
            model_available=True,
        )

    # ── Path 1: FastText (optional, Linux/EC2 upgrade) ────────────────────────
    ft_model = _try_load_fasttext()
    if ft_model is not None:
        try:
            clean = text.replace("\n", " ").strip()
            labels, probabilities = ft_model.predict(clean, k=1)
            label: str = labels[0].replace("__label__", "")   # 'en', 'fr', …
            confidence: float = float(probabilities[0])
            # Governance rule (spec): PASS only when BOTH conditions hold:
            #   label == "en"  AND  confidence >= 0.80
            # Any other combination is treated as uncertain or non-English:
            #   • label != "en", conf >= 0.80  → confidently non-English → BLOCK
            #   • label == "en", conf < 0.80   → STT may have force-mapped non-English
            #                                    phonemes to English words → BLOCK
            #   • label != "en", conf < 0.80   → uncertain detection → fail-open
            # The fast path above this block already handles common English fillers
            # ("okay", "yes", "hi" …) and name introductions, so real English
            # sentences reaching here consistently score >= 0.90 in FastText.
            if label == "en" and confidence >= FASTTEXT_CONFIDENCE_THRESHOLD:
                is_english = True   # Confirmed English
            elif label != "en" and confidence >= FASTTEXT_CONFIDENCE_THRESHOLD:
                is_english = False  # Confidently non-English — block
            else:
                is_english = True   # Uncertain in either direction — fail open

            result = LanguageValidationResult(
                is_english=is_english,
                predicted_label=label,
                confidence=confidence,
                model_available=True,
            )
            if not is_english:
                logger.warning(
                    f"[LanguageGuard] NON-ENGLISH (FastText) — "
                    f"lang={label}, confidence={confidence:.3f}, "
                    f"text='{text[:80]}'"
                )
            else:
                logger.debug(
                    f"[LanguageGuard] English confirmed (FastText) — "
                    f"confidence={confidence:.3f}"
                )
            return result
        except Exception as exc:
            logger.error(f"[LanguageGuard] FastText prediction failed: {exc}. Falling back to Lingua.")

    # ── Path 2: Lingua (pure Python, always available) ────────────────────────
    detector = _get_lingua_detector()
    if detector is None:
        # No detector at all — fail open.
        logger.error("[LanguageGuard] No language detector available. Failing open.")
        return LanguageValidationResult(
            is_english=True,
            predicted_label="unknown",
            confidence=0.0,
            model_available=False,
        )

    try:
        from lingua import Language  # noqa: PLC0415

        confidence_values = detector.compute_language_confidence_values(text)

        if not confidence_values:
            # Lingua couldn't decide — treat as English (very short input / names).
            return LanguageValidationResult(
                is_english=True,
                predicted_label="en",
                confidence=0.0,
                model_available=True,
            )

        # confidence_values is sorted descending by confidence.
        top = confidence_values[0]
        lang_code = top.language.iso_code_639_1.name.lower()  # e.g. 'en', 'fr'
        confidence = top.value                                  # 0.0 – 1.0

        # Block ONLY when Lingua is confidently non-English (threshold=0.75).
        # Low-confidence detections (ambiguous short phrases, names, accented
        # English) fall through as English to avoid false positives.
        if top.language == Language.ENGLISH:
            is_english = True
        elif confidence >= LINGUA_CONFIDENCE_THRESHOLD:
            is_english = False   # Confidently non-English — block
        else:
            is_english = True    # Uncertain — fail open (could be a name / accent)

        result = LanguageValidationResult(
            is_english=is_english,
            predicted_label=lang_code,
            confidence=confidence,
            model_available=True,
        )

        if not is_english:
            logger.warning(
                f"[LanguageGuard] NON-ENGLISH (Lingua) -- "
                f"lang={lang_code}, confidence={confidence:.3f}, "
                f"text='{text[:80]}'"
            )
        else:
            logger.debug(
                f"[LanguageGuard] English confirmed (Lingua) -- "
                f"confidence={confidence:.3f}"
            )

        return result

    except Exception as exc:
        logger.error(f"[LanguageGuard] Lingua prediction failed: {exc}. Failing open.")
        return LanguageValidationResult(
            is_english=True,
            predicted_label="unknown",
            confidence=0.0,
            model_available=True,
        )
