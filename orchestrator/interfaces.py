from abc import ABC, abstractmethod
from typing import AsyncGenerator, Callable, Any

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
    async def speak(self, text: str) -> AsyncGenerator[bytes, None]:
        pass

    @abstractmethod
    async def close(self):
        pass
