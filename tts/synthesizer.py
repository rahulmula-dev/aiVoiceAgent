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
    def __init__(self):
        self.api_key = os.getenv("DEEPGRAM_API_KEY")
        if not self.api_key:
            logger.error("DEEPGRAM_API_KEY is missing")
            raise ValueError("DEEPGRAM_API_KEY not found")
        
        # Deepgram Aura Options
        self.url = "https://api.deepgram.com/v1/speak?model=aura-asteria-en&encoding=mulaw&sample_rate=8000&container=none"
        self._client = None

    async def _get_client(self):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def speak(self, text_input):
        """
        Converts text to audio bytes using Deepgram Aura.
        Yields bytes asynchronously as they arrive.
        """
        logger.debug(f"Synthesizing audio stream ({len(text_input)} chars)")
        
        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {"text": text_input}
        
        try:
            client = await self._get_client()
            async with client.stream("POST", self.url, headers=headers, json=payload) as response:
                response.raise_for_status()
                logger.debug("Deepgram TTS stream opened.")
                
                async for chunk in response.aiter_bytes(chunk_size=1024):
                    if chunk:
                        yield chunk
                            
        except Exception as e:
            logger.error(f"TTS Failed: {e}")
            return

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

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
