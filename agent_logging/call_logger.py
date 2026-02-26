import json
import os
import time
import logging as std_logging
from datetime import datetime
from typing import List, Dict, Any

# Configure a basic logger for internal CallLogger errors
logger = std_logging.getLogger("CallLogger")

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
        
        # Log initialization
        self.log_event("orchestrator", "call_logger_initialized", 
                       meta={"agent_version": self.agent_version})
        
        # PILLAR 3: Ghost File Rule - Ensure disk allocation immediately
        self.save_log(status="initialized")

    def _anonymize_number(self, number: str) -> str:
        """Helper to mask the middle of a phone number for privacy."""
        if len(number) < 8:
            return number
        return number[:3] + "***" + number[-2:]

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
            
            # Calculate average LLM latency
            llm_latencies = [e["latency_ms"] for e in events_snapshot if e.get("type") == "orchestrator" and e.get("event") == "llm_response_start" and "latency_ms" in e]
            avg_latency = int(sum(llm_latencies) / len(llm_latencies)) if llm_latencies else 0
            
            summary = {
                "id": self.call_id,
                "start": self.start_time.isoformat() + "Z",  # ISO 8601 with Z suffix
                "dur": duration,
                "turns": user_turns,
                "lat_avg": avg_latency,
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

    def save_log(self, status: str = "completed"):
        """
        Writes the final JSON log to the logs/ directory.
        """
        try:
            self.status = status
            
            end_time = datetime.now()
            duration = round((end_time - self.start_time).total_seconds(), 2)
            
            # Use a shallow copy to avoid "list changed size" errors
            events_snapshot = list(self.events)
            
            structured_turns = []
            try:
                from orchestrator.session_manager import default_session_manager
                session = default_session_manager.get_session(self.call_id)
                if session and hasattr(session, 'structured_turns'):
                    # Convert to dict if they are Pydantic objects, but they are just simple dicts in the implementation
                    structured_turns = session.structured_turns
            except Exception as sm_err:
                logger.debug(f"Could not retrieve structured_turns for log: {sm_err}")
            
            log_data = {
                "call_id": self.call_id,
                "start_time": self.start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "duration_seconds": duration,
                "caller_number": self.caller_number,
                "status": self.status,
                "structured_turns": structured_turns,
                "events": events_snapshot
            }
            
            # Write to logs/call_{id}.json
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, f"call_{self.call_id}.json")
            
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(log_data, f, indent=2)
            
            logger.info(f"Call log saved: {log_file}")
        except Exception as e:
            logger.error(f"Failed to save call log for {self.call_id}: {e}")
