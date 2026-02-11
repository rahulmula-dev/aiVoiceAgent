# Central Logging Module for GD College AI Voice Agent
import logging
import json
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from contextvars import ContextVar

# Context variables to hold call-specific metadata
# These are thread-local and task-local (compatible with asyncio)
ctx_call_sid = ContextVar("call_sid", default="N/A")
ctx_phone_number = ContextVar("phone_number", default="N/A")

class ContextFilter(logging.Filter):
    """
    Injects context variables (Session ID, Phone Number) into the log record.
    """
    def filter(self, record):
        record.session_id = ctx_call_sid.get()
        record.phone_number = ctx_phone_number.get()
        return True

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
    root_logger.setLevel(logging.INFO)
    
    # Avoid duplicate handlers
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # Create the context filter instance
    context_filter = ContextFilter()

    # 1. Console Handler (Live Debugging)
    c_handler = logging.StreamHandler()
    c_handler.addFilter(context_filter)  # Add filter to handler
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
    f_handler.setFormatter(JSONFormatter())
    root_logger.addHandler(f_handler)

    # 3. Silence noisy external libraries
    # These libraries log excessively at INFO level (e.g., HTTP requests, connection details)
    # Bump them to WARNING to reduce log noise
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    return root_logger

# Initialize global logging automatically when this module is imported
setup_global_logging()

def bind_call_context(sid, phone):
    """
    Binds the Call SID and Phone Number to the current async context.
    Call this at the start of every Twilio WebSocket handshake.
    """
    ctx_call_sid.set(sid)
    ctx_phone_number.set(phone)
    # This info log will now automatically show the SID and Phone!
    logging.getLogger("AuditLogging").info(f"Context bound for new call.")

def log_conversation_turn(session_id, role, text):
    """
    LEGACY: Appends a human-readable turn to conversation_log.txt.
    Maintained for backward compatibility. Consider using the context-aware logger directly.
    """
    try:
        with open("conversation_log.txt", "a", encoding="utf-8") as f:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            session_short = session_id[-10:] if session_id else "UNKNOWN"
            f.write(f"\n[{timestamp}] [Session: {session_short}] {role}: {text}")
    except Exception as e:
        logging.getLogger("ConversationLog").error(f"Error writing to conversation_log.txt: {e}")
