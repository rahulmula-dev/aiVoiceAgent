import os
import httpx
import logging as std_logging
import asyncio
from dotenv import load_dotenv
from orchestrator.interfaces import TTSProvider

load_dotenv()

# Configure logging
logger = std_logging.getLogger("Synthesizer")

class TTSException(Exception):
    """Custom exception for TTS failures."""
    pass

class Synthesizer(TTSEngine):
    def __init__(self, encoding="mulaw", sample_rate=8000):
        self.api_key = os.getenv("DEEPGRAM_API_KEY")
        if not self.api_key:
            logger.error("DEEPGRAM_API_KEY is missing")
            raise ValueError("DEEPGRAM_API_KEY not found")
        
        # Deepgram Aura Options
        self.url = f"https://api.deepgram.com/v1/speak?model=aura-asteria-en&encoding={encoding}&sample_rate={sample_rate}&container=none"
        self._client = None
        self._active_texts = {} # call_id -> current_text
        self._stop_signals = set() # call_id

    async def _get_client(self):
        # Pillar 3: Persistence for Latency Reduction
        if self._client is None or self._client.is_closed:
            # Optimize connection pool
            # Set keepalive_expiry to 5s to avoid using stale connections
            limits = httpx.Limits(max_keepalive_connections=5, max_connections=10, keepalive_expiry=5.0)
            self._client = httpx.AsyncClient(timeout=10.0, limits=limits)
            logger.debug("Persistent TTS HTTP client initialized.")
        return self._client

    async def speak(self, text_input, call_id=None):
        """
        Converts text to audio bytes using Deepgram Aura.
        Yields bytes asynchronously as they arrive.
        PRD §5: 0 retries (max_retries = 1 attempt total).
        """
        if not text_input or not text_input.strip():
            return
        
        if call_id:
            self._active_texts[call_id] = text_input
            self._stop_signals.discard(call_id)

        logger.debug(f"Synthesizing audio stream ({len(text_input)} chars)")
        
        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {"text": text_input}
        
        # PRD §5: Zero retries (total 1 attempt)
        max_retries = 1
        for attempt in range(max_retries):
            try:
                client = await self._get_client()
                async with client.stream("POST", self.url, headers=headers, json=payload) as response:
                    response.raise_for_status()
                    logger.debug(f"Deepgram TTS stream opened.")
                    
                    first_chunk = True
                    async for chunk in response.aiter_bytes(chunk_size=1024):
                        if call_id in self._stop_signals:
                            logger.debug(f"Stop signal received for {call_id}. Interrupting TTS.")
                            break
                        if chunk:
                            if first_chunk:
                                logger.debug(f"First audio chunk received from Deepgram (Size: {len(chunk)})")
                                first_chunk = False
                            yield chunk
                # If we successfully finished the stream, break the retry loop
                if not (call_id in self._stop_signals):
                    break
                else:
                    # Clear active text if we stopped
                    self._active_texts.pop(call_id, None)
                    break
                            
            except Exception as e:
                logger.error(f"TTS Failure after {max_retries} attempt: {e}")
                # Pillar 3: Force client recreation on next session if it failed
                if self._client:
                    await self._client.aclose()
                    self._client = None
                raise TTSException(f"TTS API Failure: {e}")

    async def play_fallback_audio(self, websocket):
        """
        Streams a local pre-recorded audio file to the WebSocket.
        Chunks data into 160-byte segments (20ms of audio @ 8kHz mulaw).
        Mimics a real-time stream via Base64-encoded media messages.
        """
        asset_path = os.path.join("assets", "fallback.mulaw")
        if not os.path.exists(asset_path):
            logger.warning(f"Fallback asset missing at {asset_path}. Cannot play fallback.")
            return

        import base64
        import json
        
        logger.info(f"Streaming fallback audio from {asset_path}...")
        try:
            with open(asset_path, "rb") as f:
                while True:
                    # 160 bytes = 20ms @ 8000Hz (1 byte = 1 sample in mulaw)
                    chunk = f.read(160)
                    if not chunk:
                        break
                    
                    # Wrap in Twilio media format
                    media_msg = {
                        "event": "media",
                        "media": {
                            "payload": base64.b64encode(chunk).decode("utf-8")
                        }
                    }
                    await websocket.send_text(json.dumps(media_msg))
                    
                    # Throttle to 20ms to mimic real-time playback
                    await asyncio.sleep(0.02)
            logger.debug("Fallback audio streaming complete.")
        except Exception as e:
            logger.error(f"Failed to play fallback audio: {e}")

    def stop_current_speech(self, call_id: str) -> str:
        """
        Stops TTS playback immediately.
        Returns last spoken partial text.
        """
        self._stop_signals.add(call_id)
        text = self._active_texts.pop(call_id, "")
        logger.info(f"Interrupted speech for {call_id}: '{text}'")
        return text

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
