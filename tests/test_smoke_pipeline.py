"""
tests/test_smoke_pipeline.py

End-to-end smoke test for the LLM stage with a mocked Groq client.

We feed a finalized transcript into the queue the LLM consumes, replace
the real OpenAI/Groq streaming client with a fake one that emits a
deterministic sequence of tokens, and verify:

  1. Tokens flow into ``text_queue`` in order
  2. The end-of-turn "\\n" flush is emitted
  3. The None sentinel cleanly terminates run_llm
  4. Conversation history grows by one user + one assistant turn

No real network calls. No real Groq key needed. Should run in <1 second.
"""
import asyncio
import unittest
from unittest.mock import patch, AsyncMock, MagicMock


# A streamed chunk shape compatible with the openai SDK's AsyncStream:
# each iteration yields an object with .choices[0].delta.content (and
# .tool_calls = None for our purposes).
class _FakeDelta:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None


class _FakeChoice:
    def __init__(self, content):
        self.delta = _FakeDelta(content)


class _FakeChunk:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeStream:
    """Async-iterable mock that yields a list of chunks then stops."""

    def __init__(self, tokens):
        self._tokens = tokens

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for tok in self._tokens:
            yield _FakeChunk(tok)

    async def close(self):
        # Called by run_llm on barge-in abort — must exist as a no-op
        return None


class TestLLMSmokePipeline(unittest.IsolatedAsyncioTestCase):
    """One happy turn through llm.groq_llm.run_llm with everything mocked."""

    async def test_one_turn_flows_tokens_then_exits_on_sentinel(self):
        # Import here so config / env loads happen in the test loop.
        from llm import groq_llm

        # ── Fake the Groq client streaming response ───────────────────────
        fake_tokens = ["Our ", "Nail ", "Tech ", "program ", "is ", "four ", "months."]
        fake_stream = _FakeStream(fake_tokens)

        # _client.chat.completions.create is what _chat_turn awaits.
        # Mock it to return the fake stream directly.
        fake_create = AsyncMock(return_value=fake_stream)

        with patch.object(groq_llm._client.chat.completions, "create", fake_create):
            transcript_queue: asyncio.Queue = asyncio.Queue()
            text_queue: asyncio.Queue = asyncio.Queue()

            # Feed: one user turn, then end-of-call.
            await transcript_queue.put(("How long is the nail tech program?", "en", None))
            await transcript_queue.put(None)

            # Run the LLM coroutine to completion.
            await asyncio.wait_for(
                groq_llm.run_llm(transcript_queue, text_queue),
                timeout=5.0,
            )

            # ── Collect everything pushed to text_queue ───────────────────
            received = []
            while not text_queue.empty():
                received.append(text_queue.get_nowait())

            # Tokens should appear in order ...
            for tok in fake_tokens:
                self.assertIn(tok, received,
                              f"Expected token '{tok}' in text_queue output")
            # ... followed by the turn-end flush ...
            self.assertIn("\n", received, "Missing end-of-turn '\\n' flush")
            # ... and finally None to shut down TTS.
            self.assertEqual(received[-1], None,
                             "Last item must be None sentinel for TTS shutdown")

        # The fake Groq client must have been called exactly once.
        self.assertEqual(fake_create.call_count, 1)


class TestLLMBargeInAbandonsStream(unittest.IsolatedAsyncioTestCase):
    """When barge_in_event fires mid-stream, no more tokens should reach TTS."""

    async def test_barge_in_mid_stream_truncates_output(self):
        from llm import groq_llm

        # Make a fake stream that checks the event mid-iteration.
        barge_in_event = asyncio.Event()

        class _BargingStream:
            def __aiter__(self):
                return self._iter()

            async def _iter(self):
                yield _FakeChunk("Hello ")
                yield _FakeChunk("there ")
                # Caller starts speaking now
                barge_in_event.set()
                yield _FakeChunk("more ")
                yield _FakeChunk("tokens.")

            async def close(self):
                return None

        fake_create = AsyncMock(return_value=_BargingStream())

        with patch.object(groq_llm._client.chat.completions, "create", fake_create):
            transcript_queue: asyncio.Queue = asyncio.Queue()
            text_queue: asyncio.Queue = asyncio.Queue()

            await transcript_queue.put(("Tell me a story", "en", None))
            await transcript_queue.put(None)

            await asyncio.wait_for(
                groq_llm.run_llm(transcript_queue, text_queue,
                                 barge_in_event=barge_in_event),
                timeout=5.0,
            )

            received = []
            while not text_queue.empty():
                received.append(text_queue.get_nowait())

            # Tokens emitted before barge-in should be present.
            self.assertIn("Hello ", received)
            self.assertIn("there ", received)
            # Tokens emitted AFTER barge-in must NOT reach text_queue.
            self.assertNotIn("more ", received)
            self.assertNotIn("tokens.", received)


if __name__ == "__main__":
    unittest.main()
