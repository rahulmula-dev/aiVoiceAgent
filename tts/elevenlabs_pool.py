import os
import asyncio
import logging
from utils.connection_pool import WebSocketPool
from tts.elevenlabs_synthesizer import ElevenLabsSynthesizer

logger = logging.getLogger("TTSPool")

async def create_elevenlabs_tts() -> ElevenLabsSynthesizer:
    synthesizer = ElevenLabsSynthesizer()
    # Connect blocking inside the initialization routine
    connected = await synthesizer.connect()
    if not connected:
        return None
    return synthesizer

async def close_elevenlabs_tts(synthesizer: ElevenLabsSynthesizer):
    if synthesizer:
        await synthesizer.close()

async def check_health_elevenlabs_tts(synthesizer: ElevenLabsSynthesizer) -> bool:
    if not synthesizer or not synthesizer.ws or getattr(synthesizer.ws, 'closed', True):
        return False
    # Send a small KeepAlive or check state
    await synthesizer.send_keepalive()
    return True

def reset_elevenlabs_tts(synthesizer: ElevenLabsSynthesizer):
    if synthesizer:
        synthesizer.reset_state()

# Global Singleton Pool
elevenlabs_pool = WebSocketPool(
    name="ElevenLabs_TTS_Pool",
    create_connection_func=create_elevenlabs_tts,
    close_connection_func=close_elevenlabs_tts,
    health_check_func=check_health_elevenlabs_tts,
    reset_connection_func=reset_elevenlabs_tts,
    pool_size=int(os.getenv("ELEVENLABS_POOL_SIZE", "30")),
    min_connections=int(os.getenv("ELEVENLABS_MIN_CONNECTIONS", "10")),
    health_check_interval_s=int(os.getenv("ELEVENLABS_HEALTH_CHECK_INTERVAL_S", "30"))
)

class PooledTTSEngine:
    """Proxy pattern to allow VoiceOrchestrator to seamlessly use pooled TTS components."""
    def __init__(self, pool, delegate: ElevenLabsSynthesizer):
        self._pool = pool
        self._delegate = delegate

    async def speak(self, text, call_id=None):
        num_chunks = 0
        async for chunk in self._delegate.speak(text, call_id):
            yield chunk
            num_chunks += 1

    def stop_current_speech(self, call_id: str) -> str:
        return self._delegate.stop_current_speech(call_id)

    async def play_fallback_audio(self, websocket, streamSid: str = None):
        if self._delegate:
            await self._delegate.play_fallback_audio(websocket, streamSid)

    async def close(self):
        """Intercepts the orchestration tier's close() command and instead releases it back to the pool."""
        if self._delegate:
            await self._pool.release(self._delegate)
            self._delegate = None
