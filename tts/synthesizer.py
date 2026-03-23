import os
import httpx
import logging as std_logging
import asyncio
from dotenv import load_dotenv
from contracts.interfaces import TTSEngine

load_dotenv()

# Configure logging
logger = std_logging.getLogger("Synthesizer")

class TTSException(Exception):
    """Custom exception for TTS failures."""
    pass

# Global shared client for Deepgram TTS Keep-Alive pooling across calls
_SHARED_CLIENT: httpx.AsyncClient = None

class Synthesizer(TTSEngine):
    def __init__(self, encoding="mulaw", sample_rate=8000):
        self.api_key = os.getenv("DEEPGRAM_API_KEY")
        if not self.api_key:
            logger.error("DEEPGRAM_API_KEY is missing")
            raise ValueError("DEEPGRAM_API_KEY not found")
        
        if os.getenv("DPA_CANADA_ACTIVE", "false").lower() != "true":
            logger.warning("[DEV] DPA_CANADA_ACTIVE not set. Bypassing residency check for TTS local testing.")
        
        self.url = f"https://api.deepgram.com/v1/speak?model=aura-asteria-en&encoding={encoding}&sample_rate={sample_rate}&container=none"
        self._active_texts = {} # call_id -> current_text
        self._stop_signals = set() # call_id

    async def _get_client(self):
        global _SHARED_CLIENT
        # Pillar 3: Persistence for Latency Reduction
        if _SHARED_CLIENT is None or _SHARED_CLIENT.is_closed:
            # Optimize connection pool for high concurrency
            pool_size = int(os.getenv("ELEVENLABS_POOL_SIZE", "30")) # Share the same limit
            limits = httpx.Limits(max_keepalive_connections=pool_size, max_connections=pool_size, keepalive_expiry=5.0)

            # --- [LOCAL TESTING TIMERS] ---
            _SHARED_CLIENT = httpx.AsyncClient(timeout=10.0, limits=limits)
            logger.debug("Persistent TTS HTTP shared client initialized.")
        return _SHARED_CLIENT

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
                # Task HIGH-P4-02: Bound AGGREGATE TTFA (Connect + Headers + First Byte)
                start_ttfa = asyncio.get_event_loop().time()
                
                async with client.stream("POST", self.url, headers=headers, json=payload) as response:
                    response.raise_for_status()
                    
                    # Convert to manual iterator to enforce TTFA on the first byte only
                    stream_iter = response.aiter_bytes(chunk_size=1024).__aiter__()
                    
                    # --- [DYNAMIC ENVIRONMENT-AWARE BUDGETS] ---
                    from contracts.config import config
                    TTFA_BUDGET = config.ttfa_budget
                    
                    try:
                        # Calculate remaining budget for TTFA
                        elapsed = asyncio.get_event_loop().time() - start_ttfa

                        if elapsed >= TTFA_BUDGET:
                             raise TTSException(f"TTFA Budget exhausted during connection ({elapsed:.2f}s/{TTFA_BUDGET}s)")

                        remaining = TTFA_BUDGET - elapsed
                        chunk = await asyncio.wait_for(stream_iter.__anext__(), timeout=remaining)
                        if chunk:
                            yield chunk
                    except (asyncio.TimeoutError, StopAsyncIteration):
                        # Calculate final total for log
                        total_elapsed = asyncio.get_event_loop().time() - start_ttfa
                        raise TTSException(f"TTFA Breach: Deepgram TTS took too long to provide first byte ({total_elapsed:.2f}s > {TTFA_BUDGET}s)")

                    # Subsequent chunks stream with the default httpx 0.3s per-read timeout
                    async for chunk in stream_iter:
                        if call_id in self._stop_signals:
                            logger.debug(f"Stop signal received for {call_id}. Interrupting TTS.")
                            break
                        if chunk:
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
                # [FIX]: Ensure global client is cleared on fatal error
                import tts.synthesizer
                tts.synthesizer._SHARED_CLIENT = None
                logger.error(f"TTS POST Failed: {e}")
                raise TTSException(f"TTS Service Unreachable: {e}")

    async def play_fallback_audio(self, websocket, streamSid: str = None):
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
                    if streamSid:
                        media_msg["streamSid"] = streamSid
                        
                    await websocket.send_text(json.dumps(media_msg))
                    
                    # Throttle to 15ms to mimic real-time playback
                    await asyncio.sleep(0.015)
            logger.debug("Fallback audio streaming complete.")
        except Exception as e:
            import traceback
            logger.error(f"Error streaming fallback audio: {e}\n{traceback.format_exc()}")

    def stop_current_speech(self, call_id: str) -> str:
        """
        Stops TTS playback immediately.
        Returns last spoken partial text.
        """
        self._stop_signals.add(call_id)
        text = self._active_texts.pop(call_id, "")
        logger.info(f"Interrupted speech for {call_id}: '{text}'")
        return text

    def reset_state(self):
        """TTSEngine Interface compatibility: clear cache/buffers between calls."""
        self._stop_signals.clear()
        self._active_texts.clear()

    async def close(self):
        """
        Does NOT close the shared HTTP Keep-Alive client.
        We just clean up internal states.
        """
        self.reset_state()
        logger.debug("Synthesizer instance returned (shared HTTP pool remains active).")
        
    @classmethod
    async def shutdown_client(cls):
        global _SHARED_CLIENT
        if _SHARED_CLIENT and not _SHARED_CLIENT.is_closed:
            await _SHARED_CLIENT.aclose()
            logger.info("Persistent TTS HTTP shared client closed.")

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
