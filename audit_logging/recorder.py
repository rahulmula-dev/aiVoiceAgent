
import wave
import audioop
import os
from datetime import datetime
import logging

logger = logging.getLogger("CallRecorder")

class CallRecorder:
    def __init__(self, session_id: str, recordings_dir: str = "recordings"):
        """
        Records the call audio to a standard WAV file.
        Converst Twilio's Mu-Law (PCMU) to Linear PCM (WAV compatible).
        """
        self.session_id = session_id
        self.recordings_dir = recordings_dir
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
            self.wav_file.setnchannels(1)  # Mono (Mixed User + AI)
            self.wav_file.setsampwidth(2)  # 2 bytes per sample (16-bit PCM)
            self.wav_file.setframerate(8000) # 8kHz (Standard Telephony)
            
            logger.info(f"🔴 Recording started: {self.filename}")
        except Exception as e:
            logger.error(f"Failed to start recording: {e}")
            self.wav_file = None

    def write_chunk(self, ulaw_data: bytes):
        """
        Writes a chunk of audio to the file. 
        Input must be G.711 Mu-Law bytes (standard Twilio format).
        """
        if self.wav_file and ulaw_data:
            try:
                # Convert Mu-Law to 16-bit PCM Linear
                # 2 is the sample width for 16-bit
                pcm_data = audioop.ulaw2lin(ulaw_data, 2)
                self.wav_file.writeframes(pcm_data)
            except Exception as e:
                # Don't spam logs if recording fails, just ignore
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
