# Central Logging Module for GD College AI Voice Agent
import logging
import json
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from contextvars import ContextVar

# Context variables to hold call-specific metadata
# These are thread-local and task-local (compatible with asyncio)
# Context variables to hold call-specific metadata
# These are thread-local and task-local (compatible with asyncio)
ctx_call_sid = ContextVar("call_sid", default="N/A")
ctx_phone_number = ContextVar("phone_number", default="N/A")

import hashlib

class SecurityError(Exception):
    """Raised when security constraints are violated (e.g. raw PII in logs)."""
    pass

class ContextFilter(logging.Filter):
    """
    Injects context variables (Session ID, Phone Number) into the log record.
    """
    def filter(self, record):
        record.session_id = ctx_call_sid.get()
        record.phone_number = ctx_phone_number.get()
        return True

class NoWarningFilter(logging.Filter):
    """Suppresses WARNING-level records; passes DEBUG, INFO, ERROR, CRITICAL."""
    def filter(self, record):
        return record.levelno != logging.WARNING

class JSONFormatter(logging.Formatter):
    """
    Custom formatter to output logs in JSON format for production audit trails.
    """
    def format(self, record):
        log_record = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "session_id": getattr(record, "session_id", "N/A"),
            "phone_number": getattr(record, "phone_number", "N/A"),
            "module": record.name,
            "message": record.getMessage(),
        }
        
        # [SECURITY-ISS-115] HARD FAIL: Trigger SecurityError if raw phone number detected in message
        # Validates against a basic E.164-ish pattern (starting with + and multiple digits)
        raw_phone_pattern = r'\+\d{7,15}'
        import re
        if re.search(raw_phone_pattern, log_record["message"]):
             # We only allow the masked versions which contain '*'
             if '*' not in log_record["message"]:
                 raise SecurityError(f"CRITICAL SECURITY VIOLATION: Raw PII (Phone Number) detected in log message! Redact immediately.")

        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_record)

def setup_global_logging():
    """
    Configures the ROOT logger with context injection, console output, and rotating file output.
    All loggers (logging.getLogger(__name__)) will inherit these settings.
    """
    if not os.path.exists('logs'):
        os.makedirs('logs')

    root_logger = logging.getLogger()
    log_level = os.getenv("LOG_LEVEL", "DEBUG").upper()
    root_logger.setLevel(getattr(logging, log_level, logging.DEBUG))
    
    # Avoid duplicate handlers
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # Create the context filter instance
    context_filter = ContextFilter()
    no_warning_filter = NoWarningFilter()

    # 1. Console Handler (Live Debugging)
    c_handler = logging.StreamHandler()
    c_handler.addFilter(context_filter)  # Add filter to handler
    c_handler.addFilter(no_warning_filter)
    c_format = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | [%(session_id)s] [%(phone_number)s] | %(name)s | %(message)s'
    )
    c_handler.setFormatter(c_format)
    root_logger.addHandler(c_handler)

    # 2. Rotating File Handler (Production Audit)
    # 5MB per file, keeping up to 5 old logs
    f_handler = RotatingFileHandler(
        'logs/voice_agent.log', 
        maxBytes=10*1024*1024, 
        backupCount=10,
        encoding='utf-8'
    )
    f_handler.addFilter(context_filter)  # Add filter to handler
    f_handler.addFilter(no_warning_filter)
    f_handler.setFormatter(JSONFormatter())
    root_logger.addHandler(f_handler)

    # 3. Silence noisy external libraries
    # These libraries log excessively at INFO level (e.g., HTTP requests, connection details)
    # Bump them to WARNING to reduce log noise
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("s3transfer").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    return root_logger

# Initialize global logging automatically when this module is imported
setup_global_logging()

def mask_phone_number(number: str) -> str:
    """
    Standardizes phonemask across the system (MEDIUM-P3-02).
    Keeps the country code prefix and the last 4 digits.
    Example: +14035551234 -> +1******1234
    Example: +447912345678 -> +44******5678
    """
    if not number or number in ["N/A", "Unknown", "unknown"]:
        return number
    
    # Strip non-digit characters for length calculation (except '+')
    digits_only = "".join(c for c in number if c.isdigit())
    if len(digits_only) <= 4:
        return "****"
    
    # E.164 logic: prefix is typically everything beyond the last 10 digits
    # (assuming a 10-digit national number structure for most regions)
    prefix_len = max(1, len(digits_only) - 10)
    
    # SHA-256 Anonymization (ISS-117)
    # We provide a masked version for human readability and a SHA-256 hash for forensic correlation
    prefix = ""
    if digits_only.startswith("+"):
        prefix = "+" + digits_only[:prefix_len]
        suffix = digits_only[-4:]
    else:
        prefix = digits_only[:prefix_len]
        suffix = digits_only[-4:]
        
    masked = f"{prefix}******{suffix}"
    
    # Generate SHA-256 for non-reversible correlation (forensics)
    # salt = os.getenv("PII_SALT", "default_salt")
    # pii_hash = hashlib.sha256(f"{digits_only}:{salt}".encode()).hexdigest()[:12]
    # return f"{masked} (hash:{pii_hash})"
    
    return masked

def bind_call_context(sid, phone):
    """
    Binds the Call SID and Phone Number to the current async context.
    Call this at the start of every Twilio WebSocket handshake.
    """
    # PILLAR 3: Anonymize globally (MEDIUM-P3-02)
    masked_phone = mask_phone_number(phone)
    ctx_call_sid.set(sid)
    ctx_phone_number.set(masked_phone)
    # This info log will now automatically show the SID and Masked Phone!
    logging.getLogger("AuditLogging").info(f"Context bound for new call (SID: {sid}).")
