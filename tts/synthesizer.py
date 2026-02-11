import os
import httpx
import logging as std_logging
import asyncio
from dotenv import load_dotenv
from orchestrator.interfaces import TTSProvider

load_dotenv()

# Configure logging
logger = std_logging.getLogger("Synthesizer")

from contracts.interfaces import TTSEngine

class Synthesizer(TTSEngine):
    def __init__(self, encoding="mulaw", sample_rate=8000):
        self.api_key = os.getenv("DEEPGRAM_API_KEY")
        if not self.api_key:
            logger.error("DEEPGRAM_API_KEY is missing")
            raise ValueError("DEEPGRAM_API_KEY not found")
        
        # Deepgram Aura Options
        self.url = f"https://api.deepgram.com/v1/speak?model=aura-asteria-en&encoding={encoding}&sample_rate={sample_rate}&container=none"
        self._client = None

    async def _get_client(self):
        # Pillar 3: Persistence for Latency Reduction
        if self._client is None or self._client.is_closed:
            # Optimize connection pool
            # Set keepalive_expiry to 5s to avoid using stale connections
            limits = httpx.Limits(max_keepalive_connections=5, max_connections=10, keepalive_expiry=5.0)
            self._client = httpx.AsyncClient(timeout=10.0, limits=limits)
            logger.debug("Persistent TTS HTTP client initialized.")
        return self._client

    async def speak(self, text_input):
        """
        Converts text to audio bytes using Deepgram Aura.
        Yields bytes asynchronously as they arrive.
        """
        if not text_input or not text_input.strip():
            return

        logger.debug(f"Synthesizing audio stream ({len(text_input)} chars)")
        
        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {"text": text_input}
        
        max_retries = 2
        for attempt in range(max_retries):
            try:
                client = await self._get_client()
                async with client.stream("POST", self.url, headers=headers, json=payload) as response:
                    response.raise_for_status()
                    logger.debug(f"Deepgram TTS stream opened (Attempt {attempt+1}).")
                    
                    first_chunk = True
                    async for chunk in response.aiter_bytes(chunk_size=1024):
                        if chunk:
                            if first_chunk:
                                logger.debug(f"First audio chunk received from Deepgram (Size: {len(chunk)})")
                                first_chunk = False
                            yield chunk
                # If we successfully finished the stream, break the retry loop
                break
                            
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError) as e:
                logger.warning(f"TTS Connection Issue (Attempt {attempt+1}/{max_retries}): {e}")
                # Force client recreate on next attempt
                if self._client:
                    await self._client.aclose()
                    self._client = None
                if attempt == max_retries - 1:
                    logger.error(f"TTS Failed after {max_retries} attempts.")
                    raise
            except Exception as e:
                logger.error(f"TTS Unexpected Error: {e}")
                return

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            logger.info("Persistent TTS HTTP client closed.")

if __name__ == "__main__":
    # Test
    async def test():
        synth = Synthesizer()
        print("Synthesizing test audio...")
        count = 0
        async for chunk in synth.speak("Hello! This is a test."):
            count += 1
            if count == 1:
                print("First chunk received!")
        await synth.close()
        print(f"Total chunks: {count}")

    asyncio.run(test())
