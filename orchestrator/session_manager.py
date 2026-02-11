import logging
import asyncio
from typing import Dict, Optional
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from .session import Session, SessionState

logger = logging.getLogger("SessionManager")

class SessionManager:
    def __init__(self, ttl_minutes: int = 5):
        self.sessions: Dict[str, Session] = {}
        self.ttl_minutes = ttl_minutes
        self._collector_task = None

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
                    if now - session.last_accessed > timedelta(minutes=self.ttl_minutes):
                        to_delete.append(sid)
                
                for sid in to_delete:
                    logger.warning(f"ZOMBIE COLLECTION: Pruning hanging session {sid}")
                    self.end_session(sid)
            except Exception as e:
                logger.error(f"Error in Zombie Collector: {e}")

    @asynccontextmanager
    async def session_scope(self, session_id: str, call_id: str):
        """
        Async Context Manager for guaranteed session lifecycle (Pillar 3).
        Example: async with manager.session_scope(sid, cid) as session:
        """
        session = self.get_or_create_session(session_id, call_id)
        try:
            yield session
        finally:
            # Note: We typically don't end the session here automatically 
            # because some cleanup (like CRM logs) might happen after the WS closes.
            # But we ensure it's touched.
            session.touch()

    def get_or_create_session(self, session_id: str, call_id: str) -> Session:
        if session_id not in self.sessions:
            session = Session(session_id=session_id, call_id=call_id)
            self.sessions[session_id] = session
            logger.info(f"Session created: {session_id} (Call: {call_id})")
        return self.sessions[session_id]

    def get_session(self, session_id: str) -> Optional[Session]:
        session = self.sessions.get(session_id)
        if session:
            session.touch()
        return session

    def update_state(self, session_id: str, new_state: SessionState):
        session = self.get_session(session_id)
        if session:
            old_state = session.current_state
            session.current_state = new_state
            logger.info(f"State transition {session_id}: {old_state} -> {new_state}")

    def end_session(self, session_id: str):
        """Final state enforcement and removal."""
        session = self.sessions.get(session_id)
        if session:
            session.current_state = SessionState.ENDED
            session.end_time = datetime.now()
            duration = (session.end_time - session.start_time).total_seconds()
            logger.info(f"Session ended: {session_id} (Duration: {duration}s)")
            del self.sessions[session_id]
        else:
            logger.debug(f"Attempted to end non-existent session: {session_id}")

# Global instance for shared use (Singleton-ish)
default_session_manager = SessionManager()
