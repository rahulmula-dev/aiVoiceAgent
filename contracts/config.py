import os
import logging
from dataclasses import dataclass

logger = logging.getLogger("Config")

@dataclass
class FeatureConfig:
    """
    Centralized configuration for feature toggles and manual overrides.
    Enforces access control based on environment.
    """
    
    @property
    def env(self) -> str:
        return os.getenv("APP_ENV", "production").lower()

    @property
    def is_dev_or_staging(self) -> bool:
        return self.env in ["development", "staging", "test"]

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
