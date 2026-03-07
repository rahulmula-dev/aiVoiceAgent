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
        self._listener_error_callback = None

    def set_callback(self, callback):
        self.on_transcript_callback = callback

    def set_listener_error_callback(self, callback):
        """Optional callback when the Deepgram _listen loop crashes. Callback receives (exception)."""
        self._listener_error_callback = callback

    async def connect(self):
        """
        Connects to Deepgram using raw websockets for maximum stability.
        Optimized for WiFi jitter and telephony audio (8kHz mulaw).
        PRD §5: 1 retry within ≤500ms per attempt.
        """
        params = [
            "model=nova-2",        # nova-2: best multilingual support
            f"encoding={self.encoding}",
            f"sample_rate={self.sample_rate}",
            "interim_results=true",
            "smart_format=true",
            "endpointing=1000",
            "replace=GED:GD",
            "replace=male:Nail",
            "replace=Male:Nail",
        ]

        # Task 3: Architect Rule - detect_language is unsupported for streaming (causes HTTP 400)
        # We use 'en' as the default engine to prevent 400 Errors.
        params.append("language=en")

        url = f"wss://api.deepgram.com/v1/listen?{'&'.join(params)}"
        headers = {
            "Authorization": f"Token {self.api_key}"
        }

        logger.info(f"[DEEPGRAM] Connecting: encoding={self.encoding} rate={self.sample_rate}")
        try:
            with open("deepgram_debug.txt", "a", encoding="utf-8") as f:
                f.write(f"CONNECT ATTEMPT: encoding={self.encoding} sample_rate={self.sample_rate}\n")
        except: pass

        # PRD §5 RETRY LOOP: 2 attempts, ≤500ms each
        MAX_ATTEMPTS = 2
        ATTEMPT_TIMEOUT = 0.5  # 500ms
        last_error = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                self.ws = await asyncio.wait_for(
                websockets.connect(url, additional_headers=headers),
                    timeout=ATTEMPT_TIMEOUT
                )
                logger.info(f"[DEEPGRAM] Connected OK (attempt {attempt}, encoding={self.encoding}, rate={self.sample_rate})")
                try:
                    with open("deepgram_debug.txt", "a", encoding="utf-8") as f:
                        f.write(f"CONNECTED OK (attempt {attempt}): encoding={self.encoding} sample_rate={self.sample_rate}\n")
                except: pass

                asyncio.create_task(self._listen())
                return True
            except (asyncio.TimeoutError, Exception) as e:
                last_error = e
                logger.warning(f"[DEEPGRAM] Connection attempt {attempt}/{MAX_ATTEMPTS} FAILED: {e}")
                try:
                    with open("deepgram_debug.txt", "a", encoding="utf-8") as f:
                        f.write(f"CONNECT ATTEMPT {attempt} FAILED: {e}\n")
                except: pass
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(0.05)  # 50ms back-off before retry

        logger.error(f"[DEEPGRAM] All {MAX_ATTEMPTS} connection attempts failed. Last error: {last_error}")
        return False

    async def _listen(self):
        """Internal loop to process transcription results with STT Partial watchdog."""
        STT_PARTIAL_TIMEOUT = 0.45 # PRD: 450ms ceiling
        try:
            while True:
                try:
                    # Pillar 2: Anti-Freeze Watchdog (450ms)
                    message = await asyncio.wait_for(self.ws.recv(), timeout=STT_PARTIAL_TIMEOUT)
                except asyncio.TimeoutError:
                    # Watchdog Check: If we sent voice recently but got no transcript, circuit break.
                    if hasattr(self, '_last_voice_timestamp'):
                        now = asyncio.get_event_loop().time()
                        silence_duration = now - self._last_voice_timestamp
                        
                        # If we've sent voice in the last 450ms but no response, this is a hang
                        # We use 0.45s as the PRD ceiling.
                        if silence_duration > STT_PARTIAL_TIMEOUT and silence_duration < 2.0:
                            logger.error(f"[CIRCUIT BREAK] STT partial timeout: {silence_duration:.3f}s exceeds {STT_PARTIAL_TIMEOUT}s ceiling.")
                            raise ConnectionError("STT Partial Timeout Circuit Break")
                    continue

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
                    detected_lang = data.get("language") # Task 3: Architect Rule - use STT metadata

                    if sentence and sentence.strip():
                        # Mark that we received data to reset any internal watchdog timers
                        self._last_transcript_time = asyncio.get_event_loop().time()
                        
                        log_msg = f">>> DG RAW: '{sentence}' (Conf: {conf:.2f}, Final: {is_final}, Lang: {detected_lang})\n"
                        logger.debug(log_msg.strip())
                    
                    # Process transcripts
                    if len(sentence) > 0:
                        # TELEMETRY: Calculate STT Latency
                        stt_latency = 0.0
                        if hasattr(self, '_last_voice_timestamp'):
                            stt_latency = asyncio.get_event_loop().time() - self._last_voice_timestamp
                        
                        # Pass to manager
                        if is_final:
                            logger.debug(f"USER FINAL: {sentence} (Lang: {detected_lang}, STT Latency: {stt_latency:.3f}s)")
                        
                        asyncio.create_task(
                            self.on_transcript_callback(
                                sentence, 
                                confidence=conf, 
                                stt_latency=stt_latency,
                                is_final=is_final,
                                detected_lang=detected_lang
                            )
                        )
                    elif conf == 0.0 and is_final:
                        logger.debug(f"[EMPTY 0.0 + IS_FINAL] Unrecognized phonemes (Lang: {detected_lang})")
                        asyncio.create_task(
                            self.on_transcript_callback("", 0.0, is_final=True, detected_lang=detected_lang)
                        )
                else:
                    # DEBUG: Print EVERYTHING else
                    # print(f"DEBUG - DG Event: {data}")
                    pass

        except Exception as e:
            logger.error(f"ERROR - Deepgram Listener Exception: {e}")
            if self._listener_error_callback:
                try:
                    cb = self._listener_error_callback(e)
                    if asyncio.iscoroutine(cb):
                        asyncio.create_task(cb)
                except Exception as cb_err:
                    logger.error(f"STT listener error callback failed: {cb_err}")

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
                    # TELEMENTRY: Mark the timestamp of the most recent voice activity
                    self._last_voice_timestamp = asyncio.get_event_loop().time()
                    
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
                raise ConnectionError(f"STT Connection Dropped: {e}")
        else:
            raise ConnectionError("STT Connection is not established or was closed.")

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
