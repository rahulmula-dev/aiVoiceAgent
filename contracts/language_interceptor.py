"""
contracts/language_interceptor.py — language gate with 3-strike enforcement.

Sits between Deepgram STT and the LLM. For each finalized user transcript:

  1. Fast-path: if the text is in the English-affirmation set (single short
     words like "ok", "yes", "no", "hello"), allow immediately. These are
     too short for any detector to classify reliably.
  2. Name-introduction bypass: "Hi, my name is ..." and similar patterns
     are allowed without language detection (names from any culture should
     not count as non-English).
  3. Lingua detection: pure-Python detector. If confidence >= 0.75 and
     detected language != English, count a strike.
  4. Fail-open: if Lingua is unavailable or throws, allow the request
     through (we never block the caller because the detector misbehaved).

Strike policy:
  - Strikes 1 and 2: speak ``REFUSAL_LANGUAGE_1`` / ``REFUSAL_LANGUAGE_2``,
    continue the call.
  - Strike 3 (final): speak ``REFUSAL_LANGUAGE_3``, terminate the call.

State (strike count, last detected language) lives on the interceptor
instance. One instance per call; the orchestrator creates it in __init__.

Adapted from the company's contracts/language_interceptor.py (478 lines).
The clean-build version uses Lingua as the primary text classifier. Deepgram's
acoustic per-word language tag is intentionally NOT used to fire strikes —
it routinely mis-tags accented English on PSTN audio. Lingua alone is good
enough for the demo categories (English vs Hindi / Spanish / French /
Mandarin / Portuguese / German / Punjabi).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from contracts.policy import PRDScripts


logger = logging.getLogger("LanguageInterceptor")


# ─────────────────────────────────────────────────────────────────────────────
# Lingua detector — lazy import so the package only loads on first check()
# call. This keeps `from contracts.language_interceptor import ...` cheap.
# ─────────────────────────────────────────────────────────────────────────────


_lingua_detector = None  # type: ignore[assignment]


def _get_lingua_detector():
    """Return a cached Lingua LanguageDetector, building on first call."""
    global _lingua_detector
    if _lingua_detector is None:
        try:
            from lingua import Language, LanguageDetectorBuilder

            # Limit to the languages we actually want to distinguish. Building
            # with a small set is dramatically faster than the full 75-language
            # detector (~10x speedup, ~50 MB less RAM).
            _lingua_detector = (
                LanguageDetectorBuilder.from_languages(
                    Language.ENGLISH,
                    Language.HINDI,
                    Language.SPANISH,
                    Language.FRENCH,
                    Language.CHINESE,
                    Language.PUNJABI,
                    Language.GERMAN,
                    Language.PORTUGUESE,
                )
                .with_preloaded_language_models()
                .build()
            )
        except Exception as e:  # pragma: no cover  — fail-open path
            logger.warning(f"[LANG] Lingua unavailable: {e}. Failing open.")
            _lingua_detector = False  # sentinel: "tried and failed"
    return _lingua_detector if _lingua_detector else None


# ─────────────────────────────────────────────────────────────────────────────
# InterceptResult — immutable decision the orchestrator uses to route the turn
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InterceptResult:
    """Decision returned by ``LanguageGovernanceInterceptor.check()``."""

    proceed_to_llm: bool         # True = English (or allow), continue normally
    refusal_text: Optional[str]  # If proceed_to_llm=False, speak this instead of calling LLM
    terminate_call: bool         # True only on final strike — close WS after speaking
    strike: int                  # Cumulative strike count for this call
    lang_code: str               # Detected language code or "unknown"
    confidence: float            # Detector confidence in [0, 1]
    detection_method: str        # "fast_path" | "name_intro" | "lingua" | "fail_open"


# ─────────────────────────────────────────────────────────────────────────────
# LanguageGovernanceInterceptor
# ─────────────────────────────────────────────────────────────────────────────


class LanguageGovernanceInterceptor:
    """
    Per-call 3-strike language enforcement gate.

    Construct one per inbound call; pass to the LLM loop via VoiceOrchestrator.
    Not thread-safe (asyncio single-loop assumption is fine for our use).
    """

    # Single-word English utterances that detectors often misclassify.
    _ENGLISH_FAST_PATH: frozenset[str] = frozenset({
        "ok", "okay", "yes", "yeah", "yep", "yup", "no", "nope",
        "hi", "hey", "hello", "thanks", "thank", "sure", "right",
        "fine", "good", "great", "alright", "correct", "exactly",
        "agreed", "understood", "got", "noted",
        "bye", "goodbye", "later", "wait", "hold", "stop", "go",
        "help", "please", "sorry", "pardon",
        "what", "how", "when", "where", "why", "who", "which",
        "hmm", "uh", "um", "ah", "oh", "mhm", "mhmm",
    })

    # Name-introduction phrases that should never trigger a strike — names
    # from any culture are valid English-mode inputs.
    _NAME_INTRO_REGEX = re.compile(
        r"^(hi|hello|hey)?[\s.,!]*?(my name is|i am|this is|it's|i'm)\b",
        re.IGNORECASE,
    )

    # Languages that count as English (Deepgram returns region codes like en-IN).
    _ENGLISH_CODES: frozenset[str] = frozenset({
        "en", "en-us", "en-gb", "en-ca", "en-au", "en-in",
    })

    # Detection confidence threshold below which we don't block.
    _MIN_BLOCK_CONFIDENCE: float = 0.75

    def __init__(self, session_id: str, max_strikes: int = 3) -> None:
        self.session_id = session_id
        self.max_strikes = max_strikes
        self.strike_count = 0

    # ── Public entry point ───────────────────────────────────────────────

    def check(self, user_text: str, detected_lang: Optional[str] = None) -> InterceptResult:
        """
        Classify a finalized user transcript and return the routing decision.

        Parameters
        ----------
        user_text : str
            The finalized transcript from STT.
        detected_lang : str, optional
            Deepgram's acoustic / word-level language code for this utterance
            (from ``language=multi`` mode), e.g. "en", "hi", "ja". Accepted
            for backward compatibility and informational logging only — it is
            NOT consulted for strike decisions, since it routinely mis-tags
            accented English on PSTN audio.

        Caller is expected to:
          - If ``proceed_to_llm`` is True   → call the LLM normally.
          - If False and ``terminate_call`` False → speak ``refusal_text``,
            do NOT call the LLM, continue listening for the next turn.
          - If False and ``terminate_call`` True → speak ``refusal_text``,
            push None into the LLM/TTS queues so the call closes cleanly.

        Detection order (first decisive signal wins):
          1. empty input            → allow
          2. fast-path affirmations → allow
          3. name introduction      → allow
          4. non-Latin script ratio → strike  (Devanagari / Bengali / CJK / etc.)
          5. Lingua text model      → strike if text is confidently non-English
          6. fail open              → allow

        Note: Deepgram's per-word `detected_lang` tag is intentionally NOT
        used to fire strikes. On PSTN audio with accents it routinely mis-tags
        clear English as Hindi/Spanish; trusting it caused false terminations
        mid-conversation. The text itself (via Lingua) is the source of truth.
        """
        # Empty input — never block, never strike
        if not user_text or not user_text.strip():
            return self._allow("empty_input", lang_code="en", confidence=1.0)

        normalized = user_text.strip()
        lower = normalized.lower()

        # ── (1) Fast-path: short English affirmations ────────────────────
        words = re.findall(r"\b\w+\b", lower)
        if len(words) <= 2 and all(w in self._ENGLISH_FAST_PATH for w in words):
            return self._allow("fast_path", lang_code="en", confidence=1.0)

        # ── (2) Name introduction — never a strike ───────────────────────
        if self._NAME_INTRO_REGEX.search(lower):
            return self._allow("name_intro", lang_code="en", confidence=1.0)

        # ── (3) Non-Latin script check ───────────────────────────────────
        # If most of the alphabetic characters are NOT Latin (a-z), the caller
        # is speaking a language written in another script — Hindi (Devanagari),
        # Bengali, Japanese, Chinese, Arabic, etc. This is decisive and cheap,
        # and works whenever Deepgram returns native-script text, regardless of
        # the per-word language tags.
        alpha = [c for c in normalized if c.isalpha()]
        if len(alpha) >= 3:
            latin = sum(1 for c in alpha if "a" <= c.lower() <= "z")
            latin_ratio = latin / len(alpha)
            if latin_ratio < 0.40:
                code = (detected_lang.split("-")[0].lower() if detected_lang else "non-en")
                return self._strike(lang_code=code, confidence=1.0)

        # ── (5) Lingua text detection — PRIMARY signal for Latin-script ──
        # We classify the actual transcribed text, not the acoustic guess.
        # Lingua looks at character n-grams and word distributions, which is
        # far more reliable than Deepgram's per-word language tag on PSTN
        # audio with accents.
        detector = _get_lingua_detector()
        if detector is None:
            # Detector unavailable — fail open (do not strike on tag alone)
            return self._allow("fail_open", lang_code="unknown", confidence=0.0)

        try:
            confidences = detector.compute_language_confidence_values(normalized)
            if not confidences:
                return self._allow("fail_open", lang_code="unknown", confidence=0.0)

            top = confidences[0]
            lang_name = top.language.iso_code_639_1.name.lower()  # e.g. "EN" -> "en"
            confidence = float(top.value)
        except Exception as e:
            logger.warning(f"[LANG][{self.session_id}] Lingua error: {e}. Failing open.")
            return self._allow("fail_open", lang_code="unknown", confidence=0.0)

        # English at any confidence → allow. The text reads as English; that
        # wins over whatever Deepgram's acoustic guess was.
        if lang_name in self._ENGLISH_CODES:
            return self._allow("lingua", lang_code="en", confidence=confidence)

        # Non-English but low confidence → allow (likely accented English or
        # a short utterance Lingua can't pin down). Default to trusting the
        # caller rather than striking on a weak signal.
        if confidence < self._MIN_BLOCK_CONFIDENCE:
            return self._allow("lingua_low_conf", lang_code=lang_name, confidence=confidence)

        # ── Strike: confident non-English text ───────────────────────────
        return self._strike(lang_code=lang_name, confidence=confidence)

    # ── Internal helpers ─────────────────────────────────────────────────

    def _allow(self, method: str, lang_code: str, confidence: float) -> InterceptResult:
        return InterceptResult(
            proceed_to_llm=True,
            refusal_text=None,
            terminate_call=False,
            strike=self.strike_count,
            lang_code=lang_code,
            confidence=confidence,
            detection_method=method,
        )

    def _strike(self, lang_code: str, confidence: float) -> InterceptResult:
        self.strike_count += 1
        is_final = self.strike_count >= self.max_strikes
        if self.strike_count == 1:
            refusal = PRDScripts.REFUSAL_LANGUAGE_1
        elif self.strike_count == 2:
            refusal = PRDScripts.REFUSAL_LANGUAGE_2
        else:
            refusal = PRDScripts.REFUSAL_LANGUAGE_3

        logger.info(
            f"[LANG][{self.session_id}] strike {self.strike_count}/{self.max_strikes} "
            f"lang={lang_code} conf={confidence:.2f} terminate={is_final}"
        )
        return InterceptResult(
            proceed_to_llm=False,
            refusal_text=refusal,
            terminate_call=is_final,
            strike=self.strike_count,
            lang_code=lang_code,
            confidence=confidence,
            detection_method="lingua",
        )
