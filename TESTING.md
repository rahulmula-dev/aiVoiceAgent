# CILA AI Agent - Local Testing Guide

This guide explains how to test the AI agent's logic (STT -> Brain -> TTS) locally without incurring any Twilio charges.

## ✅ Zero-Cost "Chat Mode"

We have implemented a **Mock Telephony Layer** that allows you to interact with the AI via a simple text chat interface. This completely bypasses Twilio, Deepgram (STT), and ElevenLabs (TTS), so it costs nothing to run.

**It tests:**
- The `VoiceOrchestrator` logic
- The `Brain` (LLM & RAG)
- State Machine transitions
- Conversation flow & latency (simulated)

**It DOES NOT test:**
- Actual voice quality
- Twilio webhook latency
- Real STT/TTS accuracy

### How to Run

1. **Ensure your environment is set up**:
   - Make sure `.env` has your `GEMINI_API_KEY`.
   - Run `pip install -r requirements.txt` if you haven't recently.

2. **Start the Test Interface**:
   Run the helper script:
   ```powershell
   .\start_test_chat.ps1
   ```
   
   **OR** Manually:
   
   1. Start the server:
      ```bash
      python run_server.py
      ```
   2. Open your browser to:
      [http://localhost:8000/chat-ui](http://localhost:8000/chat-ui)

## 🎤 Zero-Cost "Voice Mode" (Browser Sandbox)

We also have a browser-based microphone tester that mimics the Twilio stream directly. This allows you to test **speech-to-speech** latency and quality without a phone.

1. **Start the Server**: `python run_server.py`
2. **Open the Sandbox**:
   [http://localhost:8000/static/tester.html](http://localhost:8000/static/tester.html)
3. **Click "Start Call"** and speak into your microphone.


## 📞 Full Production Mode (Twilio)

When you are ready for a real voice test:

1. Run the server normally: `python run_server.py`
2. Run ngrok: `ngrok http 8000` (Update `.env` with new URL)
3. Run the Twilio Dev Phone: `.\run_phone.ps1`
   - Or call your Twilio number directly.

---

## 🛠 Troubleshooting

**"Connection Error" in Chat UI:**
- Ensure the server is running (`python run_server.py`).
- Check the terminal for error messages.
- Refresh the page.

**LLM Errors:**
- Check your `GEMINI_API_KEY` in `.env`.
- Check if you have hit the rate limit for the free Gemini API.
