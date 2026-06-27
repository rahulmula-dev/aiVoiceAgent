# Sandbox testing — call the agent without paying Twilio

The sandbox lets you exercise the entire voice pipeline (STT → LLM → TTS)
without making a real phone call. Two flavors:

| Flavor | What it is | Best for |
|---|---|---|
| **Browser sandbox** | Web page with Start/End Call buttons, real mic + speakers | Daily development — talk to the agent like it's a phone call |
| **Offline caller** | CLI that streams a WAV file in, captures the agent reply | Automated tests, CI, reproducible smoke tests |

**The Twilio code path is untouched.** Real Twilio still works exactly the
same — dial the number and it answers. The sandbox is purely additive.

> **Production removal.** All sandbox code lives in a single labelled block.
> Search for `SANDBOX BLOCK BEGIN` / `SANDBOX BLOCK END` in
> [telephony/server.py](../telephony/server.py) — delete everything between
> the banners, plus `static/sandbox.html`, `tools/sandbox_caller.py`,
> `tools/make_test_audio.py`, and this doc. Nothing else in the codebase
> references them.

---

## A. Browser sandbox — talk to the agent live

The browser page captures your microphone, streams the audio to the running
agent, and plays the agent's reply back through your speakers. Click "Start
Call" to begin and "End Call" to hang up. Saying a goodbye phrase
("goodbye", "thanks bye") also ends the call cleanly.

### How to use

1. Start the agent (same as for a real call):

   ```powershell
   docker-compose up -d postgres redis
   uv run run_server.py
   ```

2. Open this URL in your browser:

   **http://127.0.0.1:5000/sandbox**

3. Click **Start Call**, allow microphone access when prompted, and speak.

> You do NOT need ngrok or the Twilio CLI for the browser sandbox.

### How it works under the hood

- Browser captures 16 kHz PCM via `getUserMedia` + `AudioContext`
- Audio is sent over WebSocket `/ws/sandbox` as Twilio-style JSON frames
- `BrowserWebSocketAdapter` on the server transcodes 16 kHz PCM ↔ 8 kHz mulaw
- The orchestrator, STT, and TTS modules are unchanged — they think they're
  talking to Twilio

### Limitations vs real Twilio

- Bypasses the `/voice` HTTP webhook (so the Redis concurrency gate is not
  exercised — that's tested via real Twilio calls)
- No real PSTN codec / packet-loss behaviour
- Browsers must allow microphone access on `localhost`

---

## B. Offline caller — scripted WAV-based testing

The offline caller is a Python CLI that mimics Twilio's WebSocket protocol.
It pumps a pre-recorded WAV file to the agent and saves the agent's reply
to a WAV file. Useful for repeatable tests.

---

### Offline caller — daily workflow

#### 1. Start the agent (same as always)

```powershell
docker-compose up -d postgres redis
uv run run_server.py
```

Wait until you see `[MAIN] Waiting for Twilio calls...`.

> **You do NOT need ngrok or Twilio CLI for sandbox testing.** Those are
> only needed for real Twilio calls.

#### 2. Generate a test phrase (Windows only)

```powershell
uv run tools/make_test_audio.py --say "What are your business hours?"
```

Writes `tools/sample_audio/test.wav` using Windows SAPI.

You can use any pre-recorded WAV instead — any sample rate, mono or stereo,
must be 16-bit PCM. The sandbox auto-resamples to 8 kHz.

#### 3. Call the agent

```powershell
uv run tools/sandbox_caller.py --input tools/sample_audio/test.wav
```

Output looks like:

```
[SANDBOX] Loaded tools/sample_audio/test.wav  (18320 mulaw bytes  /  2.29s of audio)
[SANDBOX] streamSid=MZ7a3...  callSid=CA9b1...
[SANDBOX] Connected to ws://127.0.0.1:5000/
[SANDBOX] Sent start frame
[SANDBOX] Waiting 2.0s for greeting...
[SANDBOX] First agent audio at 412 ms
[SANDBOX] Streaming user audio (114 frames at 20 ms each)...
[SANDBOX] User audio sent — waiting 8.0s for agent reply...
[SANDBOX] Sent stop frame
[SANDBOX] Saved agent reply -> recordings/sandbox_reply.wav  (6.12s)
[SANDBOX] Playing agent reply...
```

On Windows the reply auto-plays. Pass `--no-play` to skip and just save.

---

### Useful flags for the offline caller

```powershell
# Custom output path
uv run tools/sandbox_caller.py --input test.wav --output recordings/turn_001.wav

# Wait longer for a complex answer (e.g. RAG lookup)
uv run tools/sandbox_caller.py --input test.wav --hangup-after 15

# Skip waiting for greeting (test handles a no-greeting scenario)
uv run tools/sandbox_caller.py --input test.wav --greeting-wait 0

# Stress test with timing jitter (simulate jittery PSTN connection)
uv run tools/sandbox_caller.py --input test.wav --jitter 10

# Point at a different server (e.g. staging)
uv run tools/sandbox_caller.py --input test.wav --ws ws://192.168.1.100:5000/
```

---

---

## Switching between sandbox and real Twilio

**There is no switch.** Both modes connect to the same running server. The
choice is just *which client* you use:

| Mode | How to start |
|---|---|
| Browser sandbox | Open `http://127.0.0.1:5000/sandbox` |
| Offline caller | `uv run tools/sandbox_caller.py --input <wav>` |
| Real Twilio | Dial **+18567165450** from the Dev Phone |

The agent server doesn't know the difference. Sandbox calls bypass `/voice`
and the Redis concurrency gate — which is fine because no sandbox calls
ever happen in production.

---

## What the sandbox covers

✅ Deepgram STT (real API call)
✅ Groq / Gemini LLM (real API call)
✅ ElevenLabs TTS (real API call)
✅ RAG knowledge base lookup
✅ Governance interceptor
✅ Audit + transcript logging
✅ Connection pooling
✅ Barge-in event signalling (sandbox sends `clear` frames just like Twilio)

## What the sandbox does *not* cover

❌ Real Twilio `/voice` webhook (signature validation, status callbacks)
❌ Redis concurrency gate (no `/voice` hit, no gate acquire)
❌ Real PSTN audio impairments (packet loss, codec transcoding)
❌ Twilio call billing / status callback events

For these, run one smoke test per milestone on real Twilio.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ConnectionRefusedError` | Agent server not running | `uv run run_server.py` in another terminal first |
| `No audio received from agent` | Server crashed mid-call, or the WAV is silent | Check server logs; verify your WAV has actual audio |
| Agent reply is garbled / chopmed | Output WAV was saved before the reply finished | Increase `--hangup-after` (default 8 s) |
| `WAV must be 16-bit PCM` | Source is 8-bit or 24-bit | Re-export from Audacity as Signed 16-bit PCM |
| `audioop` import error on Python 3.13 | `audioop` removed in 3.13 | Stay on 3.11 / 3.12, or install `audioop-lts` |
| SAPI script fails on macOS / Linux | `make_test_audio.py` is Windows-only | Record a WAV with `arecord` (Linux) or `say` (macOS) |

---

## Files

| Path | Purpose |
|---|---|
| `static/sandbox.html` | Browser UI — mic + speakers + Start/End buttons |
| `telephony/server.py` (SANDBOX BLOCK) | `/sandbox` page route, `/ws/sandbox` WebSocket, PCM↔mulaw adapter |
| `tools/sandbox_caller.py` | Offline WebSocket client — mimics Twilio frames from a WAV |
| `tools/make_test_audio.py` | Windows SAPI helper for generating test WAVs |
| `tools/sample_audio/` | Place to keep your test WAVs |
| `recordings/sandbox_reply.wav` | Default location for the captured agent reply |

---

*Last updated: 2026-06-25*
