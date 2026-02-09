# Orchestrator Factory - Provider Instantiation Layer
"""
Factory pattern to decouple telephony from concrete STT/TTS implementations.
This allows the telephony layer to remain agnostic to specific providers.
"""

from stt.transcriber import Transcriber
from tts.synthesizer import Synthesizer
from orchestrator.manager import VoiceOrchestrator
from logging import CallLogger
from typing import Optional


def create_default_orchestrator(call_logger: Optional[CallLogger] = None) -> VoiceOrchestrator:
    """
    Factory method to create a VoiceOrchestrator with default providers.
    
    This encapsulates the instantiation logic, preventing the telephony layer
    from directly depending on concrete STT/TTS implementations.
    
    Args:
        call_logger: Optional CallLogger instance for call event tracking
        
    Returns:
        VoiceOrchestrator: Fully configured orchestrator with default providers
        
    Example:
        >>> manager = create_default_orchestrator(call_logger)
        >>> await manager.handle_audio_stream(websocket)
    """
    # Instantiate default providers
    stt_provider = Transcriber()
    tts_provider = Synthesizer()
    
    # Return configured orchestrator with dependency injection
    return VoiceOrchestrator(
        stt_provider=stt_provider,
        tts_provider=tts_provider,
        call_logger=call_logger
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
