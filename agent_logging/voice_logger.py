"""
agent_logging/voice_logger.py — PII masking helpers.

The only function callers need today is ``mask_phone()``. It is used by the
call logger and the access audit before any caller phone number is written
to disk or printed to the console. Keeping it in one place means there is a
single source of truth for PII masking format — change it here and every log
line follows.

Format: keep the country prefix + first 3 digits, mask the middle, keep the
last 2 digits. Example: ``+18567165450`` → ``+1856******50``.
"""

from __future__ import annotations

import re


def mask_phone(number: str | None) -> str:
    """
    Mask a caller phone number for safe logging.

    Returns ``"<unknown>"`` for empty input. Otherwise keeps the leading
    ``+`` and first 4 digits, masks the middle, keeps the last 2 digits.
    Non-digit characters in the input are stripped before masking.
    """
    if not number:
        return "<unknown>"

    digits = re.sub(r"\D", "", number)
    if len(digits) < 6:
        # Too short to mask meaningfully — drop everything except the last 2.
        return "+" + "*" * max(0, len(digits) - 2) + digits[-2:]
    return f"+{digits[:4]}{'*' * (len(digits) - 6)}{digits[-2:]}"
