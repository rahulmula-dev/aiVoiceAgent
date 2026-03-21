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
        return self.env in ["development", "staging", "test"]

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

# Global instance for easy access if needed, though injection is preferred
config = FeatureConfig()
