import os
import logging
from dataclasses import dataclass
from dotenv import load_dotenv

logger = logging.getLogger("Config")

@dataclass
class FeatureConfig:
    """
    Centralized configuration for feature toggles and manual overrides.
    Enforces access control based on environment.
    """
    
    @classmethod
    def reload_dynamic_flags(cls) -> bool:
        """
        Forces a reload of the .env file without restarting the Python process.
        Returns the updated boolean state of INTAKE_ENABLED.
        """
        logger.info("Initiating hot-reload of environment variables...")
        # override=True forces the new values from .env to overwrite current os.environ
        load_dotenv(override=True) 
        
        intake_state = os.getenv("INTAKE_ENABLED", "true").lower() == "true"
        logger.warning(f"Config Reloaded. INTAKE_ENABLED is now: {intake_state}")
        return intake_state

    @property
    def env(self) -> str:
        return os.getenv("APP_ENV", "production").lower()

    @property
    def is_dev_or_staging(self) -> bool:
        """
        True if we are in a purely local testing environment.
        User wants Staging/Production to follow strict PRD rules.
        """
        return self.env in ["development", "test"]

    @property
    def is_intake_enabled(self) -> bool:
        """Dynamic check for intake enablement."""
        return os.getenv("INTAKE_ENABLED", "true").lower() == "true"

    @property
    def override_intake(self) -> bool:
        """
        If True, disables processing of user input (Input Intake).
        Only allowed in non-production environments.
        """
        val = os.getenv("OV_DISABLE_INTAKE", "false").lower() == "true"
        if val and not self.is_dev_or_staging:
            logger.warning("Attempted to use OV_DISABLE_INTAKE in production. Ignoring.")
            return False
        return val

    @property
    def override_escalation(self) -> bool:
        """
        If True, forces immediate escalation for all requests.
        Only allowed in non-production environments.
        """
        val = os.getenv("OV_FORCE_ESCALATION", "false").lower() == "true"
        if val and not self.is_dev_or_staging:
            logger.warning("Attempted to use OV_FORCE_ESCALATION in production. Ignoring.")
            return False
        return val

    @property
    def primary_model(self) -> str:
        return os.getenv("PRIMARY_LLM_MODEL", "gemini-2.5-flash")

    @property
    def fast_model(self) -> str:
        return os.getenv("FAST_LLM_MODEL", "gemini-1.5-flash-8b")

    @property
    def is_degradation_mode(self) -> bool:
        """
        If True, the system operates in 'Fast Response' mode, switching to lightweight models.
        """
        return os.getenv("OV_DEGRADATION_MODE", "false").lower() == "true"

    @property
    def language_max_strikes(self) -> int:
        """
        Number of non-English violations before graceful call termination.
        Strike 1 → first warning, Strike N-1 → final warning, Strike N → terminate.
        Configurable via LANGUAGE_MAX_STRIKES env var. Default: 3 (2 warnings + terminate).
        """
        return int(os.getenv("LANGUAGE_MAX_STRIKES", "3"))

    @property
    def override_retrieval(self) -> bool:
        """
        If True, disables RAG retrieval (Brain acts as pure LLM).
        Only allowed in non-production environments.
        """
        val = os.getenv("OV_DISABLE_RETRIEVAL", "false").lower() == "true"
        if val and not self.is_dev_or_staging:
            logger.warning("Attempted to use OV_DISABLE_RETRIEVAL in production. Ignoring.")
            return False
        return val

    # --- ENVIRONMENT-AWARE THRESHOLDS (PRD vs DEV) ---
    
    @property
    def ttfa_budget(self) -> float:
        """Time to First Audio (TTFA) budget."""
        # PRD: 0.3s | DEV: 15.0s
        return 0.3 if not self.is_dev_or_staging else 15.0

    @property
    def stt_connect_timeout(self) -> float:
        """STT WebSocket connection timeout."""
        # PRD: 0.5s | DEV: 5.0s
        return 0.5 if not self.is_dev_or_staging else 5.0

    @property
    def stt_max_attempts(self) -> int:
        """STT Connection retry limit."""
        # PRD: 2 total | DEV: 3 total
        return 2 if not self.is_dev_or_staging else 3

    @property
    def turn_latency_circuit_break_s(self) -> float:
        """Maximum allowed latency for a single turn before circuit break."""
        # PRD: 5.0s | DEV: 35.0s
        return 5.0 if not self.is_dev_or_staging else 35.0

    @property
    def max_inbound_calls(self) -> int:
        """Concurrency limit for inbound calls."""
        # PRD: 30 | DEV: 1 (or whatever is in env)
        if not self.is_dev_or_staging:
            return 30
        return int(os.getenv("DEV_MAX_INBOUND_CALLS", "30"))

    @property
    def rag_search_timeout(self) -> float:
        """Maximum time allowed for a RAG search."""
        # PRD: 2.0s | DEV: 15.0s
        return 2.0 if not self.is_dev_or_staging else 15.0

    @property
    def stt_pool_size(self) -> int:
        """Maximum size of the STT connection pool."""
        # PRD: 30 | DEV: 5 (or from env)
        if not self.is_dev_or_staging:
            return 30
        return int(os.getenv("DEEPGRAM_POOL_SIZE", "5"))

    @property
    def stt_min_connections(self) -> int:
        """Minimum warmed connections to maintain in the STT pool."""
        # PRD: 10 | DEV: 2 (or from env)
        if not self.is_dev_or_staging:
            return 10
        return int(os.getenv("DEEPGRAM_MIN_CONNECTIONS", "2"))

# Global instance for easy access if needed, though injection is preferred
config = FeatureConfig()
