import sys
import os
sys.path.append(os.getcwd())
import unittest
import asyncio
import json
from unittest.mock import MagicMock, AsyncMock, patch
from orchestrator.manager import VoiceOrchestrator
from contracts.state import CallState

class TestTelephonyVAD(unittest.IsolatedAsyncioTestCase):
    async def test_telephony_speech_event_triggers_interruption(self):
        """
        [HIGH-P5-01] Ensure 'speech' event from Twilio triggers immediate stop.
        """
        # Mock dependencies
        stt = MagicMock()
        stt.connect = AsyncMock(return_value=True)
        stt.send_audio = AsyncMock()
        stt.close = AsyncMock()
        
        tts = MagicMock()
        tts.stop_current_speech = MagicMock(return_value="partial text")
        
        call_logger = MagicMock()
        call_logger.call_id = "test_call_id"
        
        # Initialize orchestrator
        manager = VoiceOrchestrator(stt_provider=stt, tts_provider=tts, call_logger=call_logger)
        
        # Mock websocket
        mock_ws = AsyncMock()
        mock_ws.query_params = MagicMock()
        mock_ws.query_params.get.return_value = "1234567890"
        
        # Simulation of events from Twilio
        start_event = {
            "event": "start",
            "start": {
                "streamSid": "test_sid",
                "callSid": "test_call_sid"
            }
        }
        speech_event = {
            "event": "speech",
            "speech": {
                "track": "inbound",
                "confidence": 0.95
            }
        }
        
        receive_queue = asyncio.Queue()
        await receive_queue.put({"type": "websocket.receive", "text": json.dumps(start_event)})
        # Add a delay to ensure start is processed
        await receive_queue.put("SLEEP") 
        await receive_queue.put({"type": "websocket.receive", "text": json.dumps(speech_event)})
        await receive_queue.put({"type": "websocket.disconnect"})
        
        async def mock_receive():
            val = await receive_queue.get()
            if val == "SLEEP":
                await asyncio.sleep(0.1)
                return await mock_receive()
            return val
        
        mock_ws.receive = mock_receive
        
        # Mock the session_scope
        manager.session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.start_time.timestamp.return_value = 123456789.0
        mock_session.session_id = "test_session_id"
        mock_session.caller_number = "1234567890"
        
        mock_session_context = MagicMock()
        mock_session_context.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_context.__aexit__ = AsyncMock(return_value=None)
        manager.session_manager.session_scope.return_value = mock_session_context
        
        # Patch external classes
        with patch('orchestrator.manager.CRMClient'), \
             patch('orchestrator.manager.CallRecorder'), \
             patch.object(manager, '_send_clear_message', new_callable=AsyncMock) as mock_clear, \
             patch.object(manager, 'cleanup', new_callable=AsyncMock), \
             patch.object(manager, '_monitor_silence', return_value=AsyncMock()):
            
            # Mock generate_and_speak to simulate transitions
            async def mock_gas(text, **kwargs):
                print(f"DEBUG: generate_and_speak called with '{text}'")
                manager.state.transition_to(CallState.SPEAKING)
                # Ensure response_task is set to something not done
                manager.response_task = asyncio.create_task(asyncio.sleep(10))
            
            with patch.object(manager, 'generate_and_speak', side_effect=mock_gas):
                # Run handle_audio_stream
                try:
                    await asyncio.wait_for(manager.handle_audio_stream(mock_ws), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                finally:
                    if manager.response_task:
                        manager.response_task.cancel()

        # Verify stop_current_speech was called
        # manager.sid should have been updated by 'start' event
        self.assertEqual(manager.sid, "test_sid")
        tts.stop_current_speech.assert_called_with("test_sid")
        mock_clear.assert_called()
        self.assertEqual(manager.state.get_state(), CallState.INTERRUPTED)
        print("\nPASS: Telephony 'speech' event successfully triggered interruption.")

if __name__ == "__main__":
    unittest.main()
