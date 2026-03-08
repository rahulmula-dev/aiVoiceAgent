import logging
import asyncio
import json
import os
from typing import Dict, Optional
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from .session import Session, SessionState

logger = logging.getLogger("SessionManager")

# Check for production Redis/Valkey URL
REDIS_URL = os.getenv("REDIS_URL")
try:
    if REDIS_URL:
        import redis
        redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        logger.info(f"Connected to Redis at {REDIS_URL}")
    else:
        redis_client = None
except Exception as e:
    logger.error(f"Failed to connect to Redis: {e}. Falling back to In-Memory.")
    redis_client = None

class SessionManager:
    def __init__(self, ttl_minutes: int = 5):
        self.sessions: Dict[str, Session] = {}
        self.orchestrators: Dict[str, object] = {} # session_id -> VoiceOrchestrator
        self.ttl_minutes = ttl_minutes
        self._collector_task = None
        self.use_redis = redis_client is not None

    def start_collector(self):
        """Start the background zombie collector task."""
        if not self._collector_task:
            self._collector_task = asyncio.create_task(self.zombie_collector())
            logger.info("Zombie Collector started.")

    async def zombie_collector(self):
        """Background task to prune inactive sessions (Pillar 3)."""
        while True:
            await asyncio.sleep(60) # Check every minute
            try:
                now = datetime.now()
                to_delete = []
                for sid, session in self.sessions.items():
                    if now - session.last_active > timedelta(minutes=self.ttl_minutes):
                        to_delete.append(sid)
                
                for sid in to_delete:
                    logger.warning(f"ZOMBIE COLLECTION: Pruning hanging session {sid}")
                    session = self.sessions.get(sid)
                    
                    # [ISS-162] Zombie Recovery: Ensure a "Session Pruned" ticket is sent
                    try:
                        from crm.client import CRMClient
                        crm = CRMClient()
                        
                        # Forensic Check: Was this a system failure or just a timeout?
                        history_text = "[ZOMBIE_RECOVERY]: Session timed out and was pruned by the system."
                        if session and session.conversation_history:
                            history_text = "\n".join([f"{m['role']}: {m['parts'][0]}" for m in session.conversation_history])
                        
                        # Create HIGH priority ticket for forensics
                        await crm.create_ticket(
                            transcript=history_text,
                            summary=f"CRITICAL: Session Pruned (Zombie Recovery) - {sid}",
                            sentiment="Negative",
                            call_id=sid,
                            title="Session Pruned (Zombie Recovery)",
                            structured_turns=getattr(session, 'structured_turns', None),
                            session_obj=session
                        )
                    except Exception as crm_e:
                        logger.error(f"[DLQ] Failed to create zombie CRM ticket for {sid}: {crm_e}")
                    
                    # [ISS-119] Full Cleanup: Stop active tasks and close providers
                    orchestrator = self.orchestrators.get(sid)
                    if orchestrator:
                        try:
                            # Use run_coroutine_threadsafe or similar if needed, 
                            # but collector is already an async task.
                            await orchestrator.cleanup()
                        except Exception as clean_e:
                            logger.error(f"Failed to cleanup stale orchestrator for {sid}: {clean_e}")
                    
                    # Call end_session which performs final state transition and removal
                    self.end_session(sid)
            except Exception as e:
                logger.error(f"Error in Zombie Collector: {e}")

    @asynccontextmanager
    async def session_scope(self, session_id: str, call_id: str, caller_number: str = "unknown"):
        """
        Async Context Manager for guaranteed session lifecycle (Pillar 3).
        Example: async with manager.session_scope(sid, cid) as session:
        """
        session = self.get_or_create_session(session_id, call_id, caller_number)
        try:
            yield session
        finally:
            # Note: We typically don't end the session here automatically 
            # because some cleanup (like CRM logs) might happen after the WS closes.
            # But we ensure it's touched.
            session.touch()

    def save_session(self, session: Session):
        """Forces a save of the session. In RAM, it's instant. In Redis, it writes to DB."""
        if self.use_redis:
            # Save as JSON string with TTL
            redis_client.setex(
                f"session:{session.session_id}", 
                self.ttl_minutes * 60, 
                session.json()
            )
        else:
            self.sessions[session.session_id] = session

    def get_or_create_session(self, session_id: str, call_id: str, caller_number: str = "unknown") -> Session:
        session = self.get_session(session_id)
        if not session:
            session = Session(session_id=session_id, call_id=call_id, caller_number=caller_number)
            self.save_session(session)
            from agent_logging import mask_phone_number
            logger.info(f"Session created: {session_id} (Call: {call_id}, From: {mask_phone_number(caller_number)}) [Redis: {self.use_redis}]")
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        if self.use_redis:
            raw_data = redis_client.get(f"session:{session_id}")
            if raw_data:
                session = Session.parse_raw(raw_data)
                session.touch()
                self.save_session(session) # Refresh TTL
                return session
            return None
        else:
            session = self.sessions.get(session_id)
            if session:
                session.touch()
            return session

    def update_state(self, session_id: str, new_state: SessionState):
        session = self.get_session(session_id)
        if session:
            old_state = session.current_state
            session.current_state = new_state
            self.save_session(session)
            logger.info(f"State transition {session_id}: {old_state} -> {new_state}")

    def end_session(self, session_id: str):
        """Final state enforcement and removal."""
        session = self.get_session(session_id)
        if session:
            session.current_state = SessionState.ENDED
            session.end_time = datetime.now()
            duration = (session.end_time - session.start_time).total_seconds()
            logger.info(f"Session ended: {session_id} (Duration: {duration}s)")
            
            if self.use_redis:
                redis_client.delete(f"session:{session_id}")
            elif session_id in self.sessions:
                del self.sessions[session_id]
        else:
            logger.debug(f"Attempted to end non-existent session: {session_id}")
        
        # Always remove from registry
        if session_id in self.orchestrators:
            del self.orchestrators[session_id]

    def register_orchestrator(self, session_id: str, orchestrator: object):
        """Registers an active orchestrator for zombie recovery/cleanup access."""
        self.orchestrators[session_id] = orchestrator
        logger.debug(f"Orchestrator registered for session: {session_id}")

    def unregister_orchestrator(self, session_id: str):
        """Unregisters an orchestrator."""
        if session_id in self.orchestrators:
            del self.orchestrators[session_id]
            logger.debug(f"Orchestrator unregistered for session: {session_id}")

# Global instance for shared use (Singleton-ish)
default_session_manager = SessionManager()
