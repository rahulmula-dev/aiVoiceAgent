import json
import os
import time
import logging as std_logging
from datetime import datetime
from typing import List, Dict, Any

# Configure a basic logger for internal CallLogger errors
logger = std_logging.getLogger("CallLogger")

from .voice_logger import mask_phone_number

class CallLogger:
    """
    Captures the entire lifecycle of a call in a structured JSON format.
    Tracks events, latency, and metadata.
    """
    def __init__(self, call_id: str, caller_number: str = "Unknown", agent_version: str = "1.0.0"):
        self.call_id = call_id
        self.start_time = datetime.now()
        self.caller_number = self._anonymize_number(caller_number)
        self.agent_version = agent_version
        self.status = "in-progress"
        self.reason = "unknown"  # Termination reason: user_hangup, error, timeout, agent_ended
        self.events: List[Dict[str, Any]] = []
        self._summary_written = False  # Guard: ensure summary is only written once
        self._final_log_written = False  # IMMUTABILITY GUARD
        
        # Log directory and files
        self.log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
        os.makedirs(self.log_dir, exist_ok=True)
        self.events_file = os.path.join(self.log_dir, f"call_{self.call_id}.events.jsonl")
        
        # Log initialization
        self.log_event("orchestrator", "call_logger_initialized", 
                       meta={"agent_version": self.agent_version})
        
        # PILLAR 3: Ghost File Rule - Removed mid-call/initialization writes for Audit Integrity
        # save_log(status="initialized") is now removed to preserve immutability.

    def _anonymize_number(self, number: str) -> str:
        """Helper to mask the phone number using centralized logic (MEDIUM-P3-02)."""
        return mask_phone_number(number)

    def log_event(self, event_type: str, event_name: str, latency_ms: int = None, meta: Dict[str, Any] = None, trace_id: str = None):
        """
        Logs a single event with a timestamp.
        Auto-injects call_id and trace_id for traceability.
        """
        event_entry = {
            "timestamp": datetime.now().isoformat(),
            "type": event_type,
            "event": event_name,
            "call_id": self.call_id  # Auto-inject ID
        }
        
        if latency_ms is not None:
            event_entry["latency_ms"] = latency_ms
            
        if trace_id:
            event_entry["trace_id"] = trace_id
            
        if meta:
            event_entry.update(meta)
        
        self.events.append(event_entry)

        # PRD [HIGH-P3-02]: Append-only event stream for crash resilience
        try:
            with open(self.events_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(event_entry) + "\n")
        except Exception as e:
            logger.error(f"Failed to append to event stream {self.events_file}: {e}")

    def _calculate_percentiles(self, latencies: List[int], percentiles: List[float] = None) -> Dict[str, int]:
        """
        Calculates requested percentiles for a list of latencies using the nearest-rank method.
        Default: p50, p90, p95, p99.
        """
        if percentiles is None:
            percentiles = [0.50, 0.90, 0.95, 0.99]
            
        if not latencies:
            return {f"p{int(p*100)}": 0 for p in percentiles}
        
        import math
        sorted_lats = sorted(latencies)
        n = len(sorted_lats)
        
        results = {}
        for p in percentiles:
            # Nearest-rank formula: i = ceil(P/100 * N)
            idx = max(0, min(n - 1, math.ceil(p * n) - 1))
            results[f"p{int(p*100)}"] = sorted_lats[idx]
            
        return results

    def generate_summary_line(self, status: str = None, reason: str = None):
        """
        Generates a saturated one-liner summary for logs.
        Idempotent: only writes once per CallLogger instance, regardless of how many callers invoke it.
        
        Args:
            status: Call status (e.g., "completed", "error")
            reason: Termination reason (e.g., "user_hangup", "silence_termination", "error")
        """
        # GUARD: Only write once. The first caller wins (cleanup() sets the real reason).
        # Subsequent calls from server finally blocks are no-ops.
        if self._summary_written:
            logger.debug(f"Summary already written for {self.call_id}. Skipping duplicate.")
            return
        self._summary_written = True

        try:
            if status:
                self.status = status
            if reason:
                self.reason = reason
                
            end_time = datetime.now()
            duration = round((end_time - self.start_time).total_seconds(), 2)
            
            # Use a shallow copy to avoid "list changed size" errors from background tasks
            events_snapshot = list(self.events)
            
            # Calculate turns (user spoken events)
            user_turns = len([e for e in events_snapshot if e.get("type") == "stt" and e.get("event") == "user_transcript_final"])
            
            # --- LATENCY AGGREGATION (Modular Stats) ---
            def get_lats(e_type, e_name):
                return [e["latency_ms"] for e in events_snapshot 
                        if e.get("type") == e_type and e.get("event") == e_name and "latency_ms" in e]

            llm_latencies = get_lats("orchestrator", "llm_response_start")
            stt_latencies = get_lats("stt", "user_transcript_final")
            rag_latencies = get_lats("retrieval", "rag_search_latency")
            tts_latencies = get_lats("tts", "audio_stream_start")
            
            # TELEMETRY MONITORING: Warn if core subsystems are missing events (indicates broken hooks)
            if user_turns > 0: # Only check if the user actually spoke
                for name, lats in [("LLM", llm_latencies), ("STT", stt_latencies), ("RAG", rag_latencies), ("TTS", tts_latencies)]:
                    if not lats:
                        logger.warning(f"[TELEMETRY_MISS] Subsystem {name} reported 0 latency events for call {self.call_id}")

            avg_latency = int(sum(llm_latencies) / len(llm_latencies)) if llm_latencies else 0
            
            summary = {
                "id": self.call_id,
                "start": self.start_time.isoformat() + "Z",  # ISO 8601 with Z suffix
                "dur": duration,
                "turns": user_turns,
                "lat_avg": avg_latency,
                "stats": {
                    "llm": self._calculate_percentiles(llm_latencies),
                    "stt": self._calculate_percentiles(stt_latencies),
                    "rag": self._calculate_percentiles(rag_latencies),
                    "tts": self._calculate_percentiles(tts_latencies)
                },
                "status": self.status,
                "reason": self.reason
            }
            
            summary_line = f"CALL_SUMMARY: {json.dumps(summary)}"
            
            # Output to main log (voice_agent.log) - full JSON format
            std_logging.getLogger("CallSummary").info(summary_line)
            
            # Also write ONLY this line to a separate summary file (call_summary.log)
            try:
                log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
                os.makedirs(log_dir, exist_ok=True)
                summary_file = os.path.join(log_dir, "call_summary.log")
                
                with open(summary_file, "a", encoding="utf-8") as f:
                    timestamp = datetime.now().isoformat()
                    f.write(f"{timestamp} | {summary_line}\n")
            except Exception as file_err:
                logger.error(f"Failed to write to call_summary.log: {file_err}")
                
        except Exception as e:
            logger.error(f"Failed to generate summary line for {self.call_id}: {e}")

    def save_log(self, status: str = "completed", session_obj: Any = None):
        """
        Writes the final JSON log to the logs/ directory.
        Compiles all PRD-required top-level metadata.
        Uses Atomic Write & Final Seal pattern (IMMUTABILITY).
        """
        if self._final_log_written:
            logger.debug(f"Final log already sealed for {self.call_id}. Skipping.")
            return

        # Only seal if status is 'completed' or 'error' (terminal states)
        if status not in ["completed", "error"]:
            logger.debug(f"Save suppressed: Immutability requirement prohibits mid-call writes (status: {status})")
            return

        try:
            self._final_log_written = True
            self.status = status
            
            end_time = datetime.now()
            duration = round((end_time - self.start_time).total_seconds(), 2)
            
            # Use a shallow copy to avoid "list changed size" errors
            events_snapshot = list(self.events)
            
            structured_turns = []
            if session_obj and hasattr(session_obj, 'structured_turns'):
                structured_turns = session_obj.structured_turns
            
            # --- PRD METADATA SATIATION (Forensic Fix) ---
            kb_version_id = "unknown"
            chunk_ids = []
            sentiment = "Neutral"
            termination_reason = self.reason
            confidence_scores = []
            
            if session_obj:
                if hasattr(session_obj, 'call_context'):
                    kb_version_id = session_obj.call_context.kb_version_id or "unknown"
                    chunk_ids = getattr(session_obj.call_context, 'chunk_ids_used', [])
                
                sentiment = getattr(session_obj, 'sentiment_label', "Neutral")
                termination_reason = getattr(session_obj, 'termination_reason', self.reason)
                
                # PRD HIGH-P3-01: Prioritize crash-proof session state for confidence history
                confidence_scores = getattr(session_obj, 'confidence_scores', [])
                
                # FALLBACK LOGIC: If session data is missing or unknown, scrape the events array
                if not kb_version_id or kb_version_id == "unknown":
                    v_ids = [e.get("kb_version_id") for e in events_snapshot 
                            if e.get("type") == "retrieval" and e.get("kb_version_id") and e.get("kb_version_id") != "unknown"]
                    if v_ids: 
                        kb_version_id = v_ids[0]

                if not chunk_ids:
                    chunk_ids = []
                    for e in events_snapshot:
                        if e.get("type") == "retrieval":
                            cid = e.get("top_chunk_id")
                            if cid and cid not in chunk_ids:
                                chunk_ids.append(cid)

                if not confidence_scores:
                    confidence_scores = [e.get("confidence_score") for e in events_snapshot 
                                        if e.get("type") == "brain" and "confidence_score" in e]

            # Latency Metrics Summary
            def get_lat_stats(e_type, e_name):
                lats = [e["latency_ms"] for e in events_snapshot 
                        if e.get("type") == e_type and e.get("event") == e_name and "latency_ms" in e]
                return self._calculate_percentiles(lats)

            log_data = {
                "call_id": self.call_id,
                "kb_version_id": kb_version_id,
                "chunk_ids": chunk_ids,
                "confidence_scores": confidence_scores,
                "sentiment_label": sentiment,
                "termination_reason": termination_reason,
                "latency_metrics": {
                    "llm": get_lat_stats("orchestrator", "llm_response_start"),
                    "stt": get_lat_stats("stt", "user_transcript_final"),
                    "rag": get_lat_stats("retrieval", "rag_search_latency"),
                    "tts": get_lat_stats("tts", "audio_stream_start")
                },
                "caller_number": self.caller_number,
                "start_time": self.start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "duration_seconds": duration,
                "status": self.status,
                "structured_turns": [t.dict() if hasattr(t, 'dict') else t for t in structured_turns],
                "events": events_snapshot
            }
            
            # 5. SYNC TO S3 (CRITICAL-P3-01)
            from utils.s3_storage import S3Storage
            s3 = S3Storage()
            
            # Upload Events File
            if os.path.exists(self.events_file):
                s3.upload_file(self.events_file, f"events/{os.path.basename(self.events_file)}")
                
            # Upload Summary File
            summary_file = os.path.join(self.log_dir, "call_summary.log")
            if os.path.exists(summary_file):
                # We don't delete the aggregate summary file yet, or we rename it per call
                s3_summary_key = f"summaries/{self.call_id}_summary.log"
                # For aggregate logs, we might just want to copy or upload a snapshot
                s3.upload_file(summary_file, s3_summary_key, delete_local=False)

            logger.info(f"Audit trace for {self.call_id} synced to S3.")
            # File path for the final sealed log
            log_file = os.path.join(self.log_dir, f"call_{self.call_id}.json")

            # Atomic Write Pattern: Write to temp, then rename
            temp_file = log_file + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(log_data, f, indent=2)
            
            # Atomic rename (Post-hoc seal)
            if os.path.exists(log_file):
                os.remove(log_file) # Should not exist due to single-write rule
            os.rename(temp_file, log_file)
            
            logger.info(f"Call log sealed and saved: {log_file}")

            # Optional: Remove the event stream if the final log is successfully sealed
            try:
                if os.path.exists(self.events_file):
                    os.remove(self.events_file)
            except Exception as clean_err:
                 logger.debug(f"Could not remove event stream: {clean_err}")

        except Exception as e:
            self._final_log_written = False # Reset flag if write fails so we can retry during cleanup
            logger.error(f"Failed to save call log for {self.call_id}: {e}")
        except Exception as e:
            logger.error(f"Failed to save call log for {self.call_id}: {e}")
