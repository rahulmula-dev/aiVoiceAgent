import os
import json
import base64
import asyncio
import websockets
import logging as std_logging
from contracts.interfaces import TTSEngine

logger = std_logging.getLogger("ElevenLabsTTS")

class ElevenLabsTTSException(Exception):
    pass

class ElevenLabsSynthesizer(TTSEngine):
    def __init__(self, voice_id="21m00Tcm4TlvDq8ikWAM", model_id="eleven_turbo_v2_5", sample_rate=8000):
        self.api_key = os.getenv("ELEVENLABS_API_KEY")
        self.voice_id = os.getenv("ELEVENLABS_VOICE_ID", voice_id)
        self.model_id = os.getenv("ELEVENLABS_MODEL_ID", model_id)
        self.sample_rate = sample_rate
        
        self.ws = None
        self._active_texts = {}
        self._stop_signals = set()
        
        self._audio_queue = asyncio.Queue()
        self._receive_task = None

    async def connect(self):
        """Pre-warms the ElevenLabs WebSocket connection"""
        if not self.api_key:
            # Not failing hard to allow using Deepgram fallback gracefully if requested
            logger.warning("ELEVENLABS_API_KEY not found. Cannot connect TTS.")
            return False
            
        url = f"wss://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}/stream-input?model_id={self.model_id}&output_format=ulaw_{self.sample_rate}"
        
        headers = {
            "xi-api-key": self.api_key
        }
        
        try:
            self.ws = await asyncio.wait_for(websockets.connect(url, additional_headers=headers), timeout=5.0)
            
            # Send initial configuration immediately to pre-warm the voice profile
            init_msg = {
                "text": " ",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.8},
                "xi_api_key": self.api_key, 
            }
            await self.ws.send(json.dumps(init_msg))
            
            self._receive_task = asyncio.create_task(self._listen_for_audio())
            logger.info(f"[ELEVENLABS TTS] Connected & Pre-warmed voice {self.voice_id}")
            return True
        except Exception as e:
            logger.error(f"[ELEVENLABS TTS] Connection failed: {e}")
            return False

    async def _listen_for_audio(self):
        """Background task to pull audio out of WebSocket into the queue"""
        try:
            while self.ws and not self.ws.closed:
                message = await self.ws.recv()
                data = json.loads(message)
                
                if data.get("audio"):
                    audio_bytes = base64.b64decode(data["audio"])
                    await self._audio_queue.put(audio_bytes)
                    
                if data.get("isFinal"):
                    await self._audio_queue.put(b"__FINAL__")
                    
        except websockets.exceptions.ConnectionClosed:
            logger.debug("[ELEVENLABS TTS] WS Closed")
        except Exception as e:
            logger.error(f"[ELEVENLABS TTS] Listen error: {e}")
            
    async def flush_queue(self):
        """Flushes the audio queue between turns"""
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def speak(self, text_input, call_id=None):
        if not text_input or not text_input.strip() or not self.ws:
            return
            
        if call_id:
            self._stop_signals.discard(call_id)
            self._active_texts[call_id] = text_input

        # Clear any old audio in the queue
        await self.flush_queue()

        try:
            msg = {
                "text": text_input + " ",
                "try_trigger_generation": True
            }
            await self.ws.send(json.dumps(msg))
            
            # Flush signal
            await self.ws.send(json.dumps({"text": ""}))
            
            # Retrieve audio from queue
            while True:
                if call_id in self._stop_signals:
                    # [STAB-02] Log Point 2: completion source = stop_signal
                    logger.info(f"[STAB-02][tts_completion_source] source=stop_signal call_id={call_id}")
                    break

                try:
                    chunk = await asyncio.wait_for(self._audio_queue.get(), timeout=3.0)
                    if chunk == b"__FINAL__":
                        # [STAB-02] Log Point 2: completion source = sentinel_isFinal
                        logger.info(f"[STAB-02][tts_completion_source] source=sentinel_isFinal call_id={call_id}")
                        break
                    yield chunk
                except asyncio.TimeoutError:
                    # [STAB-02] Log Point 2: completion source = timeout_3s
                    logger.error(f"[STAB-02][tts_completion_source] source=timeout_3s call_id={call_id}")
                    raise ElevenLabsTTSException("Timeout waiting for audio chunk")
                    
        except Exception as e:
            logger.error(f"ElevenLabs error: {e}")
            raise ElevenLabsTTSException(f"TTS Error: {e}")
        finally:
            if call_id and call_id in self._stop_signals:
                self._active_texts.pop(call_id, None)

    def stop_current_speech(self, call_id: str) -> str:
        self._stop_signals.add(call_id)
        if self.ws and not getattr(self.ws, 'closed', True):
            asyncio.create_task(self.ws.send(json.dumps({"text": "", "flush": True})))
            
        text = self._active_texts.pop(call_id, "")
        return text
        
    def reset_state(self):
        self._stop_signals.clear()
        self._active_texts.clear()
        # Empty queue
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def send_keepalive(self):
        """
        Active health check (HIGH-WS-01). 
        Sends a ping frame. If no pong received within 2s, marks WS closed.
        """
        if self.ws and not getattr(self.ws, 'closed', True):
            try:
                # Use websockets library standard ping
                # It will automatically await the pong or raise a ConnectionClosed
                pong_waiter = await self.ws.ping()
                await asyncio.wait_for(pong_waiter, timeout=2.0)
                logger.debug("[ELEVENLABS TTS] Active Health Check: PONG received.")
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"[ELEVENLABS TTS] Active Health Check FAILED: {e}. Marking connection dead.")
                # Mark as closed so the pool monitor reclaims it
                if self.ws:
                    self.ws.closed = True 

    async def play_fallback_audio(self, websocket, streamSid: str = None):
        """
        Streams a local pre-recorded audio file to the WebSocket.
        (WS-02: Compliance Fallback)
        """
        # Load logic from common Synthesizer utility to maintain consistency
        from tts.synthesizer import Synthesizer
        temp_synth = Synthesizer()
        await temp_synth.play_fallback_audio(websocket, streamSid)

    async def close(self):
        if self._receive_task:
            self._receive_task.cancel()
        if self.ws:
            try:
                await self.ws.close()
            except:
                pass
            finally:
                self.ws = None
