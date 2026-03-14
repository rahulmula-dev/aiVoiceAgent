import os
import json
import asyncio
import websockets
import logging as std_logging
from dotenv import load_dotenv

from orchestrator.interfaces import STTProvider

# Configure logging
logger = std_logging.getLogger("Transcriber")

load_dotenv()

DEBUG_MODE = os.getenv("DEBUG_MODE", "False").lower() == "true"

class ResidencyViolationException(Exception):
    """Raised when data residency constraints are violated."""
    pass

class Transcriber(STTProvider):
    def __init__(self, on_transcript_callback=None, encoding="mulaw", sample_rate=8000, session_metadata: dict = None):
        self.api_key = os.getenv("DEEPGRAM_API_KEY")
        if not self.api_key:
            raise ValueError("DEEPGRAM_API_KEY missing in .env")
        
        self.session_metadata = session_metadata or {}
        self.on_transcript_callback = on_transcript_callback
        self.model = os.getenv("DEEPGRAM_MODEL", "nova-2-phone")
        self.encoding = encoding
        self.sample_rate = sample_rate
        self.ws = None
        self._listener_error_callback = None
        
        # Diagnostics
        self._packet_counter = 0
        self._total_packets = 0
        self._silent_packets = 0
        self._voice_packets = 0
        self._is_listening = False
        self._last_voice_timestamp = 0.0
        self._last_heartbeat = 0.0
        self._heartbeat_task = None
        self._listen_task = None
        
        # [HEURISTIC-STT]
        self._max_partial = ""
        self._max_partial_conf = 0.0

    def set_callback(self, callback):
        self.on_transcript_callback = callback

    def set_listener_error_callback(self, callback):
        self._listener_error_callback = callback

    async def connect(self):
        params = [
            f"model={self.model}", 
            f"encoding={self.encoding}",
            f"sample_rate={self.sample_rate}",
            "interim_results=true",
            "smart_format=true",
            "endpointing=1500",
            "utterance_end_ms=1500",
            "language=en"
        ]

        domain = "api.deepgram.com"
        is_canadian = self.session_metadata.get("region") == "CA"
        if os.getenv("DPA_CANADA_ACTIVE", "false").lower() == "true" or is_canadian:
            domain = "api.ca.deepgram.com"

        url = f"wss://{domain}/v1/listen?{'&'.join(params)}"
        headers = {"Authorization": f"Token {self.api_key}"}

        # --- [STRICT PRD vs TESTING RULES] ---
        # 1. PRODUCTION Rule: 1 retry within ≤500ms total.
        # 2. TESTING Rule: Higher timeout for network jitter over Ngrok/Home WiFi.
        
        # [PRD Rule - Commented]
        # MAX_ATTEMPTS = 1
        # ATTEMPT_TIMEOUT = 0.5
        
        # [TESTING Rule - Active]
        MAX_ATTEMPTS = 3
        ATTEMPT_TIMEOUT = 5.0
        last_error = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                self.ws = await asyncio.wait_for(
                    websockets.connect(url, additional_headers=headers),
                    timeout=ATTEMPT_TIMEOUT
                )
                self._is_listening = True
                self._start_heartbeat()
                
                # Residency Check
                if (os.getenv("DPA_CANADA_ACTIVE", "false").lower() == "true" or is_canadian) and "api.ca.deepgram.com" not in url:
                    raise ResidencyViolationException("Data Residency Violation: CA data MUST use CA endpoint.")

                self._listen_task = asyncio.create_task(self._listen())
                return True
            except Exception as e:
                last_error = e
                logger.warning(f"[DEEPGRAM] Connect {attempt}/{MAX_ATTEMPTS} failed: {e}")
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(0.5)

        logger.error(f"[DEEPGRAM] Failed to connect: {last_error}")
        return False

    async def _listen(self):
        try:
            async for message in self.ws:
                data = json.loads(message)
                msg_type = data.get("type")
                
                if msg_type == "Metadata":
                    continue
                
                if msg_type == "Error":
                    logger.error(f"[DEEPGRAM ERROR] {data.get('message')}: {data.get('description')}")
                    if "1011" in str(data.get('message', '')) or "1011" in str(data.get('description', '')):
                        if self._listener_error_callback:
                            await self._trigger_error(ConnectionError(f"Deepgram 1011: {data.get('description')}"))
                    continue

                if not self.on_transcript_callback:
                    continue

                if "channel" in data:
                    channel_data = data["channel"]
                    # [TRANSITION FIX] Deepgram sends channel as a list in Nova-2 responses
                    if isinstance(channel_data, list):
                        if not channel_data: continue
                        channel_data = channel_data[0]
                    
                    if not channel_data.get("alternatives"):
                        continue
                        
                    alt = channel_data["alternatives"][0]
                    sentence = alt.get("transcript", "")
                    conf = alt.get("confidence", 0)
                    is_final = data.get("is_final", False)
                    detected_lang = data.get("language")

                    # Latching Heuristic
                    if not is_final:
                        if len(sentence) > len(self._max_partial):
                            self._max_partial = sentence
                            self._max_partial_conf = conf
                    else:
                        if self._max_partial and len(sentence) < len(self._max_partial) * 0.7 and self._max_partial_conf > 0.6:
                            sentence = self._max_partial
                            conf = self._max_partial_conf
                        elif not sentence and self._max_partial:
                            sentence = self._max_partial
                            conf = self._max_partial_conf
                        self._max_partial = ""
                        self._max_partial_conf = 0.0

                    if sentence.strip() or (is_final and conf == 0.0):
                        stt_latency = 0.0
                        if self._last_voice_timestamp > 0:
                            stt_latency = asyncio.get_event_loop().time() - self._last_voice_timestamp
                        
                        await self.on_transcript_callback(
                            sentence, 
                            confidence=conf, 
                            stt_latency=stt_latency,
                            is_final=is_final,
                            detected_lang=detected_lang
                        )

        except Exception as e:
            logger.error(f"Transcriber _listen Exception: {e}")
            if self._listener_error_callback:
                await self._trigger_error(e)
        finally:
            self._is_listening = False
            self._stop_heartbeat()
            logger.info("Transcriber _listen terminated.")

    async def _trigger_error(self, err):
        if self._listener_error_callback:
            try:
                res = self._listener_error_callback(err)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.error(f"Error in listener_error_callback: {e}")

    async def send_audio(self, audio_chunk):
        if not self.ws or not self._is_listening:
            raise ConnectionError("STT Not Connected")
        
        try:
            self._packet_counter += 1
            self._total_packets += 1
            
            # VAD (15% threshold)
            if self.encoding == "mulaw":
                non_silence = [b for b in audio_chunk if b not in [0xff, 0x7f]]
            else:
                non_silence = [b for b in audio_chunk if b != 0x00]
            
            if len(non_silence) > len(audio_chunk) * 0.15:
                self._voice_packets += 1
                self._last_voice_timestamp = asyncio.get_event_loop().time()
            else:
                self._silent_packets += 1

            if self._packet_counter % 100 == 0:
                logger.info(f"🎤 AUDIO: Packets={self._total_packets}, Voice={self._voice_packets} ({ (self._voice_packets/self._total_packets)*100:.1f}%)")

            await self.ws.send(audio_chunk)
        except Exception as e:
            raise ConnectionError(f"STT Send Failed: {e}")

    def _start_heartbeat(self):
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    def _stop_heartbeat(self):
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

    async def _heartbeat_loop(self):
        try:
            while self._is_listening:
                await asyncio.sleep(1.0) # Aggressive 1s heartbeat
                if self.ws and not getattr(self.ws, 'closed', True):
                    try:
                        await self.send_keepalive()
                        self._last_heartbeat = asyncio.get_event_loop().time()
                    except Exception:
                        break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"STT Heartbeat Error: {e}")

    async def send_keepalive(self):
        """Sends official JSON KeepAlive to satisfy Deepgram Nova-2."""
        if self.ws and not getattr(self.ws, 'closed', True):
            try:
                # Official JSON KeepAlive ONLY. 
                # [FIX]: Removed audio wedge - it triggers VAD and causes transcripts to hang.
                await self.ws.send(json.dumps({"type": "KeepAlive"}))
            except Exception as e:
                logger.debug(f"STT send_keepalive failed: {e}")
                raise

    def reset_state(self):
        self.on_transcript_callback = None
        self._listener_error_callback = None
        self._packet_counter = 0
        self._total_packets = 0
        self._voice_packets = 0
        self._silent_packets = 0
        self._last_voice_timestamp = 0.0

    async def close(self):
        self._is_listening = False
        self._stop_heartbeat()
        if self.ws:
            try: await self.ws.close()
            except: pass
        if self._listen_task:
            try: self._listen_task.cancel()
            except: pass
