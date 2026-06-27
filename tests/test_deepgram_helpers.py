"""
tests/test_deepgram_helpers.py

Pure-function tests for the STT module's helpers — no Deepgram network, no
WebSocket, no asyncio. Just the two utilities the STT pipeline relies on:

  - ``_is_hangup_phrase``  detects "goodbye" / "thanks bye" / "hang up" etc.
                           so the orchestrator can end the call cleanly.
  - ``_dominant_language`` reduces per-word language tags from Deepgram's
                           ``language=multi`` mode to a single dominant code.
"""
import unittest

from stt.deepgram_stt import _is_hangup_phrase, _dominant_language


class TestHangupPhraseDetection(unittest.TestCase):
    """Caller's utterance ends with a goodbye → triggers end-of-call."""

    def test_simple_goodbye(self):
        self.assertTrue(_is_hangup_phrase("goodbye"))
        self.assertTrue(_is_hangup_phrase("Goodbye."))
        self.assertTrue(_is_hangup_phrase("Goodbye!"))

    def test_thanks_bye_variants(self):
        for s in ("thanks bye", "thank you bye", "thanks goodbye"):
            with self.subTest(s=s):
                self.assertTrue(_is_hangup_phrase(s))

    def test_polite_endings(self):
        self.assertTrue(_is_hangup_phrase("Ok, thanks for now. Bye."))
        self.assertTrue(_is_hangup_phrase("Alright bye"))
        self.assertTrue(_is_hangup_phrase("Okay bye!"))

    def test_explicit_hangup_requests(self):
        self.assertTrue(_is_hangup_phrase("Please hang up"))
        self.assertTrue(_is_hangup_phrase("end the call"))
        self.assertTrue(_is_hangup_phrase("I'm done"))

    def test_thats_all(self):
        self.assertTrue(_is_hangup_phrase("That's all"))
        self.assertTrue(_is_hangup_phrase("thats all"))

    def test_non_hangup_phrases_dont_match(self):
        # These contain words from the hangup set but are not actual goodbyes.
        for s in (
            "Hi, how are you?",
            "Tell me about the program",
            "What are the fees",
            "Can I apply online",
            "byebye",  # not in list; safe fail
        ):
            with self.subTest(s=s):
                self.assertFalse(_is_hangup_phrase(s),
                                 msg=f"'{s}' should NOT be a hangup")

    def test_empty_and_whitespace(self):
        self.assertFalse(_is_hangup_phrase(""))
        self.assertFalse(_is_hangup_phrase(None))


class TestDominantLanguage(unittest.TestCase):
    """Per-word language tag reduction."""

    def test_all_english_returns_en(self):
        self.assertEqual(_dominant_language(["en", "en", "en"]), "en")

    def test_region_codes_normalized(self):
        # Deepgram returns en-US / en-IN / en-GB — must all reduce to "en".
        self.assertEqual(_dominant_language(["en-US", "en-IN", "en-GB"]), "en")

    def test_mixed_languages_picks_most_frequent(self):
        # 3 hindi + 2 english → hi wins
        self.assertEqual(_dominant_language(["hi", "hi", "hi", "en", "en"]), "hi")

    def test_empty_input_returns_none(self):
        self.assertIsNone(_dominant_language([]))

    def test_all_empty_tags_returns_none(self):
        self.assertIsNone(_dominant_language(["", "", ""]))

    def test_skips_empty_tags(self):
        # Empty strings shouldn't be counted; "en" should still win.
        self.assertEqual(_dominant_language(["", "en", "en", ""]), "en")


if __name__ == "__main__":
    unittest.main()
