
import wave
import audioop
import os
from datetime import datetime
import logging

logger = logging.getLogger("CallRecorder")

class CallRecorder:
    def __init__(self, session_id: str, recordings_dir: str = "recordings", encoding="mulaw", sample_rate=8000):
        """
        Records the call audio to a standard WAV file.
        Converst Twilio's Mu-Law (PCMU) to Linear PCM (WAV compatible) if needed.
        """
        self.session_id = session_id
        self.recordings_dir = recordings_dir
        self.encoding = encoding
        self.sample_rate = sample_rate
        self.filename = None
        self.wav_file = None
        
        # Ensure directory exists
        try:
            os.makedirs(self.recordings_dir, exist_ok=True)
        except Exception as e:
            logger.error(f"Failed to create recordings directory: {e}")

    def start(self):
        """Initializes the WAV file writer."""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.filename = os.path.join(self.recordings_dir, f"{self.session_id}_{timestamp}.wav")
            
            self.wav_file = wave.open(self.filename, 'wb')
            self.wav_file.setnchannels(1)  # Mono
            self.wav_file.setsampwidth(2)  # 2 bytes per sample (16-bit PCM)
            self.wav_file.setframerate(self.sample_rate)
            
            logger.info(f"🔴 Recording started: {self.filename} ({self.encoding} @ {self.sample_rate}Hz)")
        except Exception as e:
            logger.error(f"Failed to start recording: {e}")
            self.wav_file = None

    def write_chunk(self, audio_data: bytes):
        """
        Writes a chunk of audio to the file. 
        Input must match self.encoding.
        """
        if self.wav_file and audio_data:
            try:
                if self.encoding == "mulaw":
                    # Convert Mu-Law to 16-bit PCM Linear
                    pcm_data = audioop.ulaw2lin(audio_data, 2)
                else:
                    # Assume linear16 (already PCM)
                    pcm_data = audio_data
                
                self.wav_file.writeframes(pcm_data)
            except Exception as e:
                pass

    def close(self):
        """Closes the file handle."""
        if self.wav_file:
            try:
                self.wav_file.close()
                logger.info(f"💾 Recording saved: {self.filename}")
            except Exception as e:
                logger.error(f"Error closing recording: {e}")
            self.wav_file = None
