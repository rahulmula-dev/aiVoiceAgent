# Switch to Main Branch and Verify

- [x] Switch to `main` branch and pull latest changes
- [x] Install dependencies from `requirements.txt`
- [x] Verify `.env` configuration
- [x] List files to confirm entry point (`run_server.py`) <!-- id: 0 -->
- [x] Fix IndentationError in `orchestrator/manager.py` (duplicate block)
- [x] Fix SyntaxError in `orchestrator/manager.py` (missing except)
- [x] Fix IndentationError in `orchestrator/brain.py` (retry logic)
- [x] Fix SyntaxError in `orchestrator/brain.py` (invalid elif)
- [x] Update Pinecone dependency (`pinecone-client` -> `pinecone`) [Verified]
- [x] Restart server (PID killed, script updated)
- [x] Launch Chat UI and Phone UI for testing
- [-] Fix chat disconnection (AttributeError: session_transcript) [Verified]
- [-] Fix AttributeError: 'VoiceOrchestrator' object has no attribute 'chat_history' [Verified]
- [-] Fix WebSocketDisconnect and CRM create_ticket arguments [Verified]
- [x] Fix Sandbox Audio Quality (Linear16/16kHz sync) <!-- id: 7 -->
- [x] Fix Browser Sandbox Crash (base64 overflow fix) <!-- id: 8 -->
- [x] Test the application (health check or test script) <!-- id: 2 -->

- [x] Restart server and run sandbox (manual request) <!-- id: 3 -->
- [x] Test voice call in Twilio Dev Phone sandbox <!-- id: 4 -->
    - [x] Verify ngrok tunnel is active and matches .env
    - [x] Perform live call test to Twilio number (via Dev Phone)

- [x] Test voice call in Browser Sandbox (tester.html) <!-- id: 5 -->

- [x] Optimize Call Latency (RAG & TTS) <!-- id: 6 -->
    - [x] Remove redundant RAG search in `brain.py`
    - [x] Implement persistent HTTP client in `synthesizer.py`
- [x] Debug Silent Agent (Session 45f748af) <!-- id: 9 -->
    - [x] Fix `tester.html` JS crash (missing element)
    - [x] Add identity loop timeout in `manager.py`
    - [x] Manual User Testing (Voice Sandbox).

- [x] Implement English-Only Voice Guardrail <!-- id: 10 -->
    - [x] Add `validate_response` to `brain.py`
    - [x] Update `generate_and_speak` in `manager.py` with refusal script
    - [x] Verify escalation and CRM ticket on violation

- [x] Fix Sandbox Response Failure (TTS Connection Stale) <!-- id: 11 -->
    - [x] Implement retry logic in `synthesizer.py`
    - [x] Tune connection pool (shorter keep-alive)
    - [x] Verify multi-turn interaction in sandbox

- [x] Sandbox Accessibility & Guardrail Fixes <!-- id: 12 -->
    - [x] Clean up `_on_transcript` callback (remove redundant connection logic)
    - [x] Fix state machine to allow speaking during `ESCALATION`
    - [x] Verify agent listens after guardrail refusal
    - [x] Verify non-English/Policy refusal is audible

- [x] Fix Guardrail Race Condition & Silence <!-- id: 14 -->
    - [x] Implement explicit task cancellation in `generate_and_speak`
    - [x] Add "First Chunk" logging in `synthesizer.py`
    - [x] Verify refusal audio production in logs

- [x] Fix Refusal Silence & Continuation <!-- id: 15 -->
    - [x] Remove `CALL_END` trigger in `manager.py` after refusals
    - [x] Add timeout/robustness to `speak_refusal`
    - [x] Verify call stays open after language refusal

- [x] Integrate and Verify New Gemini API Key (Final update with ...voxz40) <!-- id: 13 -->
    - [x] Implement non-English Detection Strategy
    - [x] Update Deepgram to `language=en` for phonetic forcing (Option B)
    - [x] Implement robust state-aware consecutive empty frame counting (Threshold: 4)
    - [x] Add explicit JSON logging for [NON-ENGLISH SPEECH DETECTED]
    - [x] Ensure `speak_refusal` records events to JSON artifacts
- [x] Final System Verification
    - [x] Verify English conversation stability (no false refusals)
    - [x] Verify sustained Hindi refusal trigger (caught within ~3s)
    - [x] Verify JSON log auditability for refusals
    - [x] Verify Brain refuses non-English/gibberish input via prompt
- [x] GitHub Synchronization <!-- id: 16 -->
    - [x] Push all refined logic and fixes to `main` branch
- [x] Perform Total System Log Cleanup <!-- id: 17 -->
    - [x] Purge `logs/` directory (Except locked files)
    - [x] Purge root debugging logs (Scripts deleted, some logs locked)
    - [x] Purge `recordings/` directory
- [x] Implement Smart RAG Loader (Loader & Guardrail) <!-- id: 18 -->
    - [x] Create `retrieval/loader.py`
    - [x] Implement `retrieve_context` with safety filters
    - [x] Verify blocking logic with sensitive query
- [x] Pull Sprint-2 from team-ccl repository <!-- id: 19 -->
    - [x] Stash local changes
    - [x] Fetch from remote `destination`
    - [x] Checkout and sync local `sprint-2` branch
- [x] Commit and Push Sprint-2 Changes <!-- id: 20 -->
    - [x] Stage `ingest.py` and `loader.py`
    - [x] Commit with message
    - [x] Push to `origin` AND `destination` (team-ccl)
- [x] Implement Confidence Gatekeeper (Loader Thresholds) <!-- id: 21 -->
    - [x] Audit `loader.py` for confidence logic
    - [x] Implement `DEFAULT_THRESHOLD = 0.58`
    - [x] Implement `CATEGORY_THRESHOLDS` fallback
    - [x] Return `low_confidence` status for weak matches
    - [x] Commit and push all final changes to `origin` and `destination`
    - [x] Unify `vector_store.py` with audited `loader.py` logic
    - [x] Push "Hit & Miss" Demo script to Team Repo
    - [x] Re-Pull latest Sprint-2 changes from Team Repo (All sync complete)
    - [x] Fix RAG Over-Blocking (Collateral Damage)
        - [x] Refine safety keywords in `ingest.py`
        - [x] Adjust safety blocking threshold in `vector_store.py`
        - [x] Unify grounding threshold in `brain.py`
        - [x] Re-ingest data and verify

# Sprint 3: RAG & Knowledge Management - Logs & Scaling

- [x] Task 1: Standardize Event Tracing & Logging <!-- id: 22 -->
    - [x] Update `contracts/schemas.py` (`trace_id`, `kb_version_id`)
    - [x] Refactor `agent_logging/call_logger.py` (Auto-inject `call_id`)
    - [x] Update `retrieval/vector_store.py` (Log `kb_version_id`)
    - [x] Restart Server and Verify in Live Call Log
    - [x] Commit and Push to `sprint-3` (team-ccl)
    - [x] Sync with Remote (Pull complete)

- [x] Task 2: CRM Integration & Fault Tolerance <!-- id: 23 -->
    - [x] Implement `crm/client.py` with Idempotency (deduplicate by `call_id`)
    - [x] Add Persistence Layer: Retry Limit + Dead-Letter Queue (`logs/crm_dlq.json`)
    - [x] Fix Orchestrator "Fire-and-Forget" (Add `done_callback` for logging)
    - [x] Verify with `verify_crm_resilience.py` (Simulate 503 -> DLQ)

- [x] Task 3: AI Explainability (Decision Logs) <!-- id: 24 -->
    - [x] Implement structured logging in `brain.py` (`decision_trace` event)
    - [x] Log `intent`, `confidence_score`, `chunks_used`
    - [x] Verify human-readable logs in `voice_agent.log`
    - [x] Verify JSON logs in `logs/call_*.json`

- [x] Task 4: Admin Toggles (Feature Flags) <!-- id: 25 -->
    - [x] Implement `contracts/config.py` with `FeatureConfig`
    - [x] Add overrides: `OV_DISABLE_INTAKE`, `OV_FORCE_ESCALATION`
    - [x] Verify production safety (overrides ignored in PROD)
    - [x] Verify override behavior in `tests/test_feature_overrides.py`

- [x] Sprint 3 Conclusion <!-- id: 26 -->
    - [x] Final System Audit (All Tasks Verified)
    - [x] Manual User Testing (Voice Sandbox)

# Sprint 4: Deployment & Optimization
- [x] Sync Dev Branch <!-- id: 27 -->
    - [x] Fetch `team-ccl/dev`
    - [x] Checkout local `dev` branch
    - [x] Update dependencies

- [x] Task 1: Staging Deployment Setup & Guardrails <!-- id: 28 -->
    - [x] Audit Environment Config (`contracts/config.py`) [Verified]
    - [x] Implement Health Routes (`/healthz`, `/readyz`)
    - [x] Implement Intake Kill Switch at Connection Level
    - [x] Verify Kill Switch Behavior (Simulation)
