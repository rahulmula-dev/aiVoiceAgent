import json
import os
import logging
from datetime import datetime
from typing import Optional, Dict, Any

# Configure dedicated audit logger
audit_logger = logging.getLogger("AuditTrail")

class AuditLogger:
    """
    Logs all access events for sensitive endpoints.
    Format: JSONL for easy forensic ingestion.
    """
    LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "access_audit.jsonl")

    @classmethod
    def log_access(cls, endpoint: str, role: str, status: str, action: str, ip: str = "unknown"):
        """Records a single audit entry."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "role": str(role),
            "endpoint": endpoint,
            "action": action,
            "status": status,
            "ip": ip
        }
        
        # 1. Stdout Log for observability
        audit_logger.info(f"[AUDIT] {entry['role']} -> {entry['endpoint']} ({entry['status']})")
        
        # 2. JSONL Log for forensic trail (Disk)
        try:
            os.makedirs(os.path.dirname(cls.LOG_FILE), exist_ok=True)
            with open(cls.LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            # Fallback to system logger if disk write fails
            logging.error(f"Audit Log Failure: {e}")
