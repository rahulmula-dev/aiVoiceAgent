import os
import asyncio
import logging
from utils.connection_pool import WebSocketPool
from stt.transcriber import Transcriber

logger = logging.getLogger("STTPool")

async def create_transcriber() -> Transcriber:
    transcriber = Transcriber()
    # Connect blocking inside the initialization routine
    connected = await transcriber.connect()
    if not connected:
        return None
    return transcriber

async def close_transcriber(transcriber: Transcriber):
    if transcriber:
        await transcriber.close()

async def check_health_transcriber(transcriber: Transcriber) -> bool:
    try:
        if not transcriber or not transcriber._ws_is_open():
            return False
        # CRITICAL: Reject if error has occurred or listen loop crashed
        if getattr(transcriber, '_has_critical_error', False):
            return False
        if not getattr(transcriber, '_is_listening', False):
            return False
        # Send a Deepgram KeepAlive metadata event
        # Safety check: ensure method exists before calling
        if hasattr(transcriber, 'send_keepalive'):
            await transcriber.send_keepalive()
        return True
    except Exception as e:
        logger.debug(f"Health check failed for transcriber: {e}")
        return False

def reset_transcriber(transcriber: Transcriber):
    if transcriber:
        transcriber.reset_state()

from contracts.config import config

# Global Singleton Pool
stt_pool = WebSocketPool(
    name="Deepgram_STT_Pool",
    create_connection_func=create_transcriber,
    close_connection_func=close_transcriber,
    health_check_func=check_health_transcriber,
    reset_connection_func=reset_transcriber,
    pool_size=config.stt_pool_size,
    min_connections=config.stt_min_connections,
    health_check_interval_s=5  # Reduced to 5s to stay well within Deepgram's silence window (net0001)
)

class PooledTranscriber:
    """Proxy pattern to allow VoiceOrchestrator to seamlessly use pooled STT components."""
    def __init__(self, pool, delegate: Transcriber):
        self._pool = pool
        self._delegate = delegate

    def set_callback(self, callback):
        self._delegate.set_callback(callback)

    def set_listener_error_callback(self, callback):
        self._delegate.set_listener_error_callback(callback)

    async def connect(self):
        # The connection is already established and health-checked by the pool.
        # Returning True immediately to prevent redundant health checks from slowing down the call start.
        return True

    async def send_audio(self, chunk):
        await self._delegate.send_audio(chunk)

    async def close(self):
        """Intercepts the orchestration tier's close() command and instead releases it back to the pool."""
        if self._delegate:
            await self._pool.release(self._delegate)
            self._delegate = None
