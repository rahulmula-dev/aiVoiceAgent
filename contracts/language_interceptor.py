"""
contracts/language_interceptor.py
==================================
Language Governance Interceptor — the explicit gate between Deepgram STT
and the Gemini LLM call.

Architecture position
---------------------

  Twilio audio
      │
  Deepgram STT  (transcript + detected_lang)
      │
  ┌───▼──────────────────────────────────────┐
  │  LanguageGovernanceInterceptor.check()   │  ← YOU ARE HERE
  │  • Deepgram acoustic signal (PRIMARY)    │
  │  • FastText lid.176.ftz (0.80 threshold) │
  │  • Lingua pure-Python (0.75 threshold)   │
  │  • 3-Strike state machine                │
  └──────────┬────────────────┬──────────────┘
             │ proceed=True   │ proceed=False
             ▼                ▼
         Gemini LLM       Refusal TTS
         (secondary          │
          Hinglish           │ terminate=True?
          filter)            └──► websocket.close()

Session state
-------------
Strike count lives on this object (one instance per call).
The orchestrator (manager.py) is responsible for creating the interceptor
when a call starts and persisting ``interceptor.strike_count`` to the
``Session.language_warning_count`` field after each ``check()`` call.

Usage in manager.py
-------------------
::

    # In VoiceOrchestrator.__init__ / start_call():
    self._lang_interceptor = LanguageGovernanceInterceptor(session_id=self.sid)

    # In _on_transcript():
    result = self._lang_interceptor.check(raw_text, deepgram_lang=detected_lang)
    self.session.language_warning_count = result.strike  # persist to session

    if not result.proceed_to_llm:
        await self.speak_immediate_response(result.refusal_text)
        if result.terminate_call:
            await self._language_termination_flow(result.refusal_text, trace_id)
        return

    # … continue to Gemini …

FastText on EC2
---------------
Set the environment variable ``FASTTEXT_MODEL_PATH`` to the absolute path of
``lid.176.ftz``.  If the file is absent or the package is not installed, the
interceptor transparently falls back to Lingua, then fails-open (never blocks)
so the call is never dropped due to a missing library.

    FASTTEXT_MODEL_PATH=/home/ubuntu/ai-voice-agent/models/lid.176.ftz
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("LanguageInterceptor")

# ── Lazy import — avoid circular deps ─────────────────────────────────────────
# These are resolved at call time, not import time.
_policy_scripts = None


def _get_scripts():
    global _policy_scripts
    if _policy_scripts is None:
        from contracts.policy import PRDScripts  # noqa: PLC0415
        _policy_scripts = PRDScripts
    return _policy_scripts


# ── Result dataclass ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class InterceptResult:
    """
    Structured decision returned by ``LanguageGovernanceInterceptor.check()``.

    Attributes
    ----------
    proceed_to_llm  : True  → transcript is English; forward to Gemini.
                      False → refusal must be spoken; do NOT call Gemini.
    refusal_text    : The exact script to speak when ``proceed_to_llm`` is False.
    terminate_call  : True on Strike 3 → close websocket after speaking refusal.
    strike          : Current cumulative strike count for this session.
    lang_code       : ISO-639-1 code of the detected language (``"en"``, ``"fr"``…).
    confidence      : Detector confidence in [0, 1].
    detection_method: Which detector fired (``"deepgram_acoustic"``, ``"fasttext"``,
                      ``"lingua"``, ``"fast_path"``, or ``"fail_open"``).
    """
    proceed_to_llm: bool
    refusal_text: Optional[str]
    terminate_call: bool
    strike: int
    lang_code: str
    confidence: float
    detection_method: str


# ── Interceptor ────────────────────────────────────────────────────────────────

class LanguageGovernanceInterceptor:
    """
    Stateful 3-Strike language gate for one call session.

    One instance is created per inbound call.  Thread-safety is not required
    because each call is processed on a single asyncio event loop.

    Parameters
    ----------
    session_id  : Identifier used in log messages (call_id / session_id).
    max_strikes : Number of non-English detections before termination (default 3).
    """

    # BCP-47 codes considered "English" (covers Deepgram region variants)
    _SUPPORTED_CODES: frozenset[str] = frozenset({"en", "en-us", "en-gb", "en-ca", "en-au", "en-in"})

    # Common single-word English utterances that language models mis-classify
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

    # Regex: name-introduction utterances must never trigger a strike
    _INTRO_RE = re.compile(
        r"^(hi|hello)?[\s.,!]*?(my name is|i am|this is|it'?s)\b",
        re.IGNORECASE,
    )

    def __init__(self, session_id: str, max_strikes: int = 3) -> None:
        self.session_id = session_id
        self.max_strikes = max_strikes
        self._strike_count: int = 0

    # ── Public API ─────────────────────────────────────────────────────────────

    @property
    def strike_count(self) -> int:
        """Read-only view of cumulative non-English strike count."""
        return self._strike_count

    def reset(self) -> None:
        """Reset strike count. Call this when reusing the orchestrator for a new call."""
        self._strike_count = 0
        logger.info(f"[Interceptor:{self.session_id}] Strike counter reset.")

    def check(
        self,
        transcript: str,
        deepgram_lang: Optional[str] = None,
    ) -> InterceptResult:
        """
        Validate one Deepgram transcript and apply the 3-Strike policy.

        Parameters
        ----------
        transcript    : Final transcript string from Deepgram's ``on_message``.
        deepgram_lang : ``detected_language`` field from Deepgram metadata, e.g.
                        ``"en"``, ``"fr"``, ``"hi"``.  Pass ``None`` if unavailable.

        Returns
        -------
        InterceptResult
            Callers must check ``proceed_to_llm`` before forwarding to Gemini,
            and ``terminate_call`` before closing the WebSocket.
        """
        # ── 1. Empty / silence ─────────────────────────────────────────────────
        text = (transcript or "").strip()
        if not text:
            return self._pass("", 0.0, "empty")

        # ── 2. Single-word English fast path ───────────────────────────────────
        # Language models are unreliable on standalone fillers ("okay" → Tagalog).
        # Hard-code the unambiguous English affirmations.
        normalised = text.lower().rstrip(".,!?")
        if normalised in self._ENGLISH_FAST_PATH:
            return self._pass("en", 1.0, "fast_path")

        # ── 3. Name-introduction fast path ─────────────────────────────────────
        # "Hi, my name is Jaspreet" must never trigger a strike regardless of the
        # name's language of origin.
        if self._INTRO_RE.search(text):
            return self._pass("en", 1.0, "fast_path")

        # ── 4. Deepgram acoustic detection (PRIMARY when available) ────────────
        # Operating on raw audio phonemes, Deepgram's acoustic detection is the
        # most reliable signal and is checked before any text-based model.
        if deepgram_lang:
            norm = deepgram_lang.lower().split("-")[0]
            if self._is_supported_code(deepgram_lang):
                # Deepgram confirms a supported language — fast-path approve.
                # Text detection still runs as a secondary confirmation, but we
                # trust the acoustic signal when it says English.
                return self._pass(norm, 1.0, "deepgram_acoustic")
            else:
                # Deepgram detected a non-supported language at the phoneme level.
                # This is the strongest possible non-English signal.
                logger.warning(
                    f"[Interceptor:{self.session_id}] Deepgram acoustic blocked "
                    f"lang='{deepgram_lang}': '{text[:60]}'"
                )
                return self._strike(norm, 1.0, "deepgram_acoustic")

        # ── 5. Text-based detection: FastText → Lingua → fail-open ────────────
        is_english, lang_code, confidence, method = self._text_detect(text)

        if is_english:
            return self._pass(lang_code, confidence, method)
        else:
            return self._strike(lang_code, confidence, method)

    # ── Private helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _is_supported_code(code: str) -> bool:
        """True if the BCP-47 code (or its base form) is a supported language."""
        base = code.lower().split("-")[0]
        return base in LanguageGovernanceInterceptor._SUPPORTED_CODES

    def _text_detect(self, text: str) -> tuple[bool, str, float, str]:
        """
        Run FastText → Lingua → fail-open chain.

        Returns
        -------
        (is_english, lang_code, confidence, method)
        """
        try:
            from contracts.language_guard import validate_language, _try_load_fasttext  # noqa: PLC0415
            result = validate_language(text)
            if not result.model_available:
                # No detector was initialised — fail open (never punish the caller)
                logger.warning(
                    f"[Interceptor:{self.session_id}] No detector available — failing open."
                )
                return True, "unknown", 0.0, "fail_open"

            # Determine which detector was actually used (FastText takes priority in language_guard)
            method = "fasttext" if _try_load_fasttext() is not None else "lingua"
            return result.is_english, result.predicted_lang_code, result.confidence, method

        except Exception as exc:
            logger.error(
                f"[Interceptor:{self.session_id}] Text detection raised {exc!r}. Failing open."
            )
            return True, "unknown", 0.0, "fail_open"

    def _pass(self, lang_code: str, confidence: float, method: str) -> InterceptResult:
        """Return an allow-through result without touching the strike counter."""
        logger.debug(
            f"[Interceptor:{self.session_id}] PASS "
            f"lang={lang_code} conf={confidence:.3f} via={method} "
            f"strikes={self._strike_count}"
        )
        return InterceptResult(
            proceed_to_llm=True,
            refusal_text=None,
            terminate_call=False,
            strike=self._strike_count,
            lang_code=lang_code,
            confidence=confidence,
            detection_method=method,
        )

    def _strike(self, lang_code: str, confidence: float, method: str) -> InterceptResult:
        """Increment the strike counter and return the appropriate refusal."""
        self._strike_count += 1
        scripts = _get_scripts()

        if self._strike_count >= self.max_strikes:
            refusal = scripts.REFUSAL_LANGUAGE_3
            terminate = True
        else:
            # Strikes 1 and 2 use the same polite prompt (PRD §Language Governance)
            refusal = scripts.REFUSAL_LANGUAGE_1
            terminate = False

        logger.warning(
            f"[Interceptor:{self.session_id}] NON-ENGLISH STRIKE {self._strike_count}/{self.max_strikes} "
            f"lang={lang_code} conf={confidence:.3f} via={method} | terminate={terminate}"
        )

        return InterceptResult(
            proceed_to_llm=False,
            refusal_text=refusal,
            terminate_call=terminate,
            strike=self._strike_count,
            lang_code=lang_code,
            confidence=confidence,
            detection_method=method,
        )
