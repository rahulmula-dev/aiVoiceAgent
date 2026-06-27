"""
tests/test_tts_sentence_boundary.py

Unit tests for ``_is_sentence_boundary`` in tts/elevenlabs_tts.py.

This helper decides when to flush an accumulating token buffer to ElevenLabs
for synthesis. Splitting too early would chop the middle of "$5.99" or
"Mr. Smith" and produce robotic output. Splitting too late would push
TTS first-audio latency above the 600 ms budget.

Rules tested:
  - '!' or '?' at end always terminates
  - '.' at end IS a boundary
  - '.' preceded by a digit is NOT (decimal: $5.99)
  - '.' preceded by an abbreviation is NOT (Mr., Dr., etc.)
  - "..." ellipsis once fully formed IS a boundary
"""
import unittest

from tts.elevenlabs_tts import _is_sentence_boundary


class TestHardEndings(unittest.TestCase):
    def test_exclamation_terminates(self):
        self.assertTrue(_is_sentence_boundary("Great answer!"))

    def test_question_terminates(self):
        self.assertTrue(_is_sentence_boundary("Can I help you?"))

    def test_period_terminates(self):
        self.assertTrue(_is_sentence_boundary("Our office is open."))


class TestDecimalNumbersDontSplit(unittest.TestCase):
    """Common bug — splitting on the dot inside a price."""

    def test_dollar_amount(self):
        self.assertFalse(_is_sentence_boundary("The total is $5.99"))

    def test_decimal_number(self):
        self.assertFalse(_is_sentence_boundary("That measures 12.5"))

    def test_decimal_in_middle_of_sentence(self):
        # Buffer is mid-sentence; the trailing dot belongs to a decimal.
        self.assertFalse(_is_sentence_boundary("Our rate of 4.2"))


class TestAbbreviationsDontSplit(unittest.TestCase):
    """Mr./Dr./St. etc. must not terminate a sentence."""

    def test_mr(self):
        self.assertFalse(_is_sentence_boundary("Please contact Mr."))

    def test_dr(self):
        self.assertFalse(_is_sentence_boundary("Speak with Dr."))

    def test_etc(self):
        self.assertFalse(_is_sentence_boundary("programs, applications, fees, etc."))

    def test_eg(self):
        self.assertFalse(_is_sentence_boundary("e.g."))


class TestNormalSentences(unittest.TestCase):
    """Realistic LLM output snippets — these SHOULD split."""

    def test_full_sentence_with_period(self):
        self.assertTrue(_is_sentence_boundary(
            "Our Nail Technician Diploma is a 4-month program."
        ))

    def test_question_with_question_mark(self):
        self.assertTrue(_is_sentence_boundary(
            "Would you like to know about the application process?"
        ))

    def test_exclamation_in_greeting(self):
        self.assertTrue(_is_sentence_boundary("Welcome to GD College!"))


class TestIncompleteBuffer(unittest.TestCase):
    """Mid-stream tokens — no terminator, should NOT split."""

    def test_incomplete_sentence_no_punctuation(self):
        self.assertFalse(_is_sentence_boundary("We offer a variety of"))

    def test_empty_buffer(self):
        self.assertFalse(_is_sentence_boundary(""))
        self.assertFalse(_is_sentence_boundary("   "))


class TestEllipsis(unittest.TestCase):
    """LLMs sometimes emit '...' — treat fully-formed ellipsis as terminal."""

    def test_full_ellipsis(self):
        self.assertTrue(_is_sentence_boundary("Hmm let me think..."))


if __name__ == "__main__":
    unittest.main()
