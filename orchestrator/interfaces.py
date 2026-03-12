from abc import ABC, abstractmethod
from typing import AsyncGenerator, Callable, Any, Optional

class STTProvider(ABC):
    @abstractmethod
    async def connect(self) -> bool:
        pass

    @abstractmethod
    def set_callback(self, callback: Callable[[str], Any]):
        pass

    @abstractmethod
    async def send_audio(self, audio_chunk: bytes):
        pass

    @abstractmethod
    async def close(self):
        pass

class TTSProvider(ABC):
    @abstractmethod
    async def speak(self, text: str, call_id: Optional[str] = None) -> AsyncGenerator[bytes, None]:
        pass

    @abstractmethod
    def stop_current_speech(self, call_id: str) -> str:
        pass

    @abstractmethod
    async def close(self):
        pass
