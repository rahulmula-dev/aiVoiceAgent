# Orchestrator Factory - Provider Instantiation Layer
"""
Factory pattern to decouple telephony from concrete STT/TTS implementations.
This allows the telephony layer to remain agnostic to specific providers.
"""

from stt.transcriber import Transcriber
from tts.synthesizer import Synthesizer
from orchestrator.manager import VoiceOrchestrator
from orchestrator.session_manager import SessionManager
from agent_logging import CallLogger
from typing import Optional, Any
import logging
import random
import asyncio

logger = logging.getLogger("OrchestratorFactory")

async def create_default_orchestrator(
    session_id: str,
    call_logger: Optional[CallLogger] = None, 
    session_manager: Optional[SessionManager] = None,
    websocket: Optional[any] = None,
    session_metadata: Optional[dict] = None
) -> VoiceOrchestrator:
    """
    Factory method to create a VoiceOrchestrator with pre-warmed pool providers.
    
    Args:
        call_logger: Optional CallLogger instance for call event tracking
        session_manager: Optional shared SessionManager
        
    Returns:
        VoiceOrchestrator: Fully configured orchestrator with default providers
        
    Example:
        >>> manager = await create_default_orchestrator(call_logger)
        >>> await manager.handle_audio_stream(websocket)
    """
    import os
    from stt.stt_pool import stt_pool, PooledTranscriber
    from tts.elevenlabs_pool import elevenlabs_pool, PooledTTSEngine

    # 1. Acquire STT (Deepgram Websockets)
    stt_timeout = 0.5 # Strict timeout for POOLED acquisition (PRD §5)
    try:
        raw_stt = await stt_pool.acquire(timeout=stt_timeout)
    except Exception as e:
        logger.warning(f"STT Pool Acquisition Failed: {e}. Falling back to fresh connection (Latency risk).")
        # Safety Valve: Jitter to prevent "Thundering Herd" (CRITICAL-WS-01)
        await asyncio.sleep(random.uniform(0.1, 0.5))
        
        from stt.stt_pool import create_transcriber
        try:
            # CTO Polish: Longer timeout for fresh fallback attempt (Hail Mary)
            raw_stt = await asyncio.wait_for(create_transcriber(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.error("STT Fallback (Fresh Connection) timed out after 5.0s.")
            raise e
        except Exception as fe:
            logger.error(f"STT Fallback failed: {fe}")
            raise e
    logger.info("Orchestrator Factory: STT provider ready")
    stt_provider = PooledTranscriber(stt_pool, raw_stt)
    
    # Pillar 2: Residency Guard - ensure provider knows the session context
    if hasattr(raw_stt, 'session_metadata'):
        raw_stt.session_metadata = session_metadata or {}

    # 2. Acquire TTS
    tts_provider_name = os.getenv("TTS_PROVIDER", "deepgram").lower()
    if tts_provider_name == "elevenlabs":
        tts_timeout = int(os.getenv("ELEVENLABS_SESSION_TIMEOUT_MS", "300")) / 1000.0
        try:
            raw_tts = await elevenlabs_pool.acquire(timeout=tts_timeout)
        except Exception as e:
            # WS-02: CRM Fallback & Soft Landing (CRITICAL Audit Point)
            logger.error(f"TTS Pool Exhausted: {e}. Triggering CRM Ticket & Fallback Audio.")
            from crm.client import crm_client
            asyncio.create_task(crm_client.create_ticket(
                title="Dropped Call - Resource Exhaustion",
                description=f"TTS Pool Exhaustion for session {session_id}. Error: {e}",
                priority="HIGH"
            ))
            
            # trigger a play_fallback_audio (or equivalent) before the exception is raised
            if websocket:
                temp_synth = Synthesizer()
                await temp_synth.play_fallback_audio(websocket)
            
            # If TTS checkout fails, release STT back to pool
            await stt_provider.close()
            raise e
        tts_provider = PooledTTSEngine(elevenlabs_pool, raw_tts)
    else:
        # Default testing path via Deepgram HTTP Sync Limit upgraded
        tts_provider = Synthesizer()
    
    # Return configured orchestrator with dependency injection
    return VoiceOrchestrator(
        stt_provider=stt_provider,
        tts_provider=tts_provider,
        call_logger=call_logger,
        session_manager=session_manager
    )


def create_custom_orchestrator(
    stt_provider_class,
    tts_provider_class,
    call_logger: Optional[CallLogger] = None,
    **provider_kwargs
) -> VoiceOrchestrator:
    """
    Factory method for creating an orchestrator with custom providers.
    
    This enables easy swapping of STT/TTS providers without modifying
    the telephony or orchestrator layers.
    
    Args:
        stt_provider_class: Class implementing STTProvider interface
        tts_provider_class: Class implementing TTSProvider interface
        call_logger: Optional CallLogger instance
        **provider_kwargs: Additional kwargs to pass to provider constructors
        
    Returns:
        VoiceOrchestrator: Configured orchestrator with custom providers
        
    Example:
        >>> from external.google_stt import GoogleSTT
        >>> from external.elevenlabs_tts import ElevenLabsTTS
        >>> manager = create_custom_orchestrator(GoogleSTT, ElevenLabsTTS, call_logger)
    """
    stt = stt_provider_class(**provider_kwargs.get('stt_config', {}))
    tts = tts_provider_class(**provider_kwargs.get('tts_config', {}))
    
    return VoiceOrchestrator(
        stt_provider=stt,
        tts_provider=tts,
        call_logger=call_logger
    )
