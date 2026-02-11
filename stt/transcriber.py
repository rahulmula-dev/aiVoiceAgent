import os
import json
import asyncio
import websockets
import base64
import logging as std_logging
from dotenv import load_dotenv

from orchestrator.interfaces import STTProvider

# Configure logging
logger = std_logging.getLogger("Transcriber")

load_dotenv()

class Transcriber(STTProvider):
    def __init__(self, on_transcript_callback=None, encoding="mulaw", sample_rate=8000):
        self.api_key = os.getenv("DEEPGRAM_API_KEY")
        if not self.api_key:
            raise ValueError("DEEPGRAM_API_KEY missing in .env")
        
        self.on_transcript_callback = on_transcript_callback
        self.model = os.getenv("DEEPGRAM_MODEL", "nova-2-phone")
        self.encoding = encoding
        self.sample_rate = sample_rate
        self.ws = None
        self._keep_alive_task = None

    def set_callback(self, callback):
        self.on_transcript_callback = callback

    async def connect(self):
        """
        Connects to Deepgram using raw websockets for maximum stability.
        Optimized for WiFi jitter and telephony audio (8kHz mulaw).
        """
        params = [
            "model=nova-2",
            f"encoding={self.encoding}",
            f"sample_rate={self.sample_rate}",
            "interim_results=true",
            "smart_format=true",
            "endpointing=300",
            "language=multi"  # Allow Deepgram to use multi-language detection for better 0.0-confidence filtering
        ]
        
        url = f"wss://api.deepgram.com/v1/listen?{'&'.join(params)}"
        headers = {
            "Authorization": f"Token {self.api_key}"
        }

        try:
            # Using additional_headers for websockets v15+ compatibility
            self.ws = await websockets.connect(url, additional_headers=headers)
            logger.debug(f"SUCCESS - Deepgram Connected (Model: nova-2)")
            try:
                with open("deepgram_debug.txt", "a", encoding="utf-8") as f:
                    f.write("DEBUG: WebSocket Connected Successfully\n")
            except: pass

            asyncio.create_task(self._listen())
            # NOTE: Removed custom KeepAlive JSON task as it may interfere with the binary audio stream 
            # and cause Deepgram session resets.
            return True
        except Exception as e:
            logger.error(f"ERROR - Deepgram Connection Failed: {e}")
            return False

    async def _listen(self):
        """Internal loop to process transcription results"""
        try:
            with open("deepgram_debug.txt", "a", encoding="utf-8") as f:
                f.write("DEBUG: Listen Loop Started\n")
            
            async for message in self.ws:
                # Log raw message receipt
                with open("deepgram_debug.txt", "a", encoding="utf-8") as f:
                    f.write(f"RAW MSG: {message}\n")

                data = json.loads(message)
                
                # HEARTBEAT / METADATA CHECK
                msg_type = data.get("type")
                if msg_type == "Metadata":
                    logger.debug(f"DG Metadata Received (ID: {data.get('request_id')})")
                    continue

                if "channel" in data:
                    alt = data["channel"]["alternatives"][0]
                    sentence = alt["transcript"]
                    conf = alt.get("confidence", 0)
                    is_final = data.get("is_final", False)

                    if sentence and sentence.strip() and is_final:
                        log_msg = f">>> DG RAW: '{sentence}' (Conf: {conf:.2f}, Final: {is_final})\n"
                        logger.debug(log_msg.strip())
                        # If we had a call_logger injected here, we would use it.
                        # For now, manager.py logs the final transcript.
                        # But we can add a placeholder for future injection.
                        try:
                            with open("deepgram_debug.txt", "a", encoding="utf-8") as f:
                                f.write(log_msg)
                        except: pass
                    
                    # Process final transcripts
                    if is_final:
                        speech_final = data.get("speech_final", False)
                        
                        if len(sentence) > 0:
                            # Non-empty transcript - pass to manager
                            logger.debug(f"USER FINAL: {sentence}")
                            asyncio.create_task(self.on_transcript_callback(sentence, conf))
                        elif conf == 0.0 and speech_final:
                            # Empty + 0.0 + speech_final = Pass for state-aware detection
                            logger.debug(f"[EMPTY 0.0 + SPEECH_FINAL] Passing to manager")
                            asyncio.create_task(self.on_transcript_callback("", 0.0))
                else:
                    # DEBUG: Print EVERYTHING else
                    # print(f"DEBUG - DG Event: {data}")
                    pass

        except Exception as e:
            logger.error(f"ERROR - Deepgram Listener Exception: {e}")

    async def send_audio(self, audio_chunk):
        """Sends raw bytes up to Deepgram with enhanced diagnostics"""
        if self.ws:
            try:
                # Initialize diagnostics counters
                if not hasattr(self, '_packet_counter'):
                    self._packet_counter = 0
                    self._total_packets = 0
                    self._silent_packets = 0
                    self._voice_packets = 0
                    logger.debug("🎤 AUDIO DIAGNOSTICS: Monitoring microphone input...")
                
                self._packet_counter += 1
                self._total_packets += 1
                
                # Mu-law vs Linear16 Digital Silence markers
                if self.encoding == "mulaw":
                    non_silence_bytes = [b for b in audio_chunk if b not in [0xff, 0x7f]]
                else:
                    # For Linear16 (Little Endian), check for non-zero values
                    non_silence_bytes = [b for b in audio_chunk if b != 0x00]
                
                # Classify packet
                if len(non_silence_bytes) > len(audio_chunk) * 0.1:  # 10% activity threshold
                    self._voice_packets += 1
                    if not hasattr(self, '_first_voice_detected'):
                        self._first_voice_detected = True
                        logger.debug(f"🎤 VOICE DETECTED! ({self.encoding} @ {self.sample_rate}Hz, Packet #{self._packet_counter})")
                else:
                    self._silent_packets += 1
                
                # Periodic diagnostic report (every 100 packets = ~2 seconds)
                if self._packet_counter % 100 == 0:
                    voice_percent = (self._voice_packets / self._total_packets) * 100
                    logger.debug(
                        f"🎤 AUDIO REPORT: Packets={self._total_packets}, "
                        f"Voice={self._voice_packets} ({voice_percent:.1f}%), "
                        f"Silent={self._silent_packets}"
                    )
                    
                    # Warning if no voice detected
                    if self._voice_packets == 0:
                        logger.warning(
                            f"⚠️ MICROPHONE WARNING: No voice detected in {self._total_packets} packets. "
                            f"Check microphone permissions and volume!"
                        )

                await self.ws.send(audio_chunk)
            except Exception as e:
                logger.error(f"ERROR - Failed to send audio: {e}")

    async def close(self):
        """Gracefully closes the connection"""
        if self._keep_alive_task:
            self._keep_alive_task.cancel()
        if self.ws:
            try:
                # Send CloseStream to prompt Deepgram for final transcripts
                await self.ws.send(json.dumps({"type": "CloseStream"}))
                # Wait briefly for final results to be received in the _listen loop
                await asyncio.sleep(1.0) 
                await self.ws.close()
                logger.info("STOP - Deepgram Connection Closed.")
            except:
                pass
            finally:
                self.ws = None
