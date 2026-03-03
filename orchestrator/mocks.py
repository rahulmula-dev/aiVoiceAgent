from typing import AsyncGenerator, Callable, Any, Optional
from orchestrator.interfaces import STTProvider, TTSProvider

class MockSTT(STTProvider):
    def __init__(self):
        self.callback = None

    async def connect(self) -> bool:
        return True

    def set_callback(self, callback: Callable[[str], Any]):
        self.callback = callback

    async def send_audio(self, audio_chunk: bytes):
        pass

    async def close(self):
        pass

class MockTTS(TTSProvider):
    async def speak(self, text: str, call_id: Optional[str] = None) -> AsyncGenerator[bytes, None]:
        # Return the text as bytes
        yield text.encode('utf-8')

    def stop_current_speech(self, call_id: str) -> str:
        return "Speech stopped"

    async def close(self):
        pass
