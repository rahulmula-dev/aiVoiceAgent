import os
import asyncio
import logging
import time
import random
from typing import Optional, Callable, Any

logger = logging.getLogger("WebSocketPool")

class PoolExhaustedError(Exception):
    pass

class WebSocketPool:
    """
    Generalized WebSocket Connection Pool for Voice Services (STT/TTS).
    Maintains a pre-warmed queue of connections to eliminate setup latency.
    """
    def __init__(
        self,
        name: str,
        create_connection_func: Callable[[], Any],
        close_connection_func: Callable[[Any], Any],
        health_check_func: Callable[[Any], Any],
        reset_connection_func: Callable[[Any], None],
        pool_size: int,
        min_connections: int,
        health_check_interval_s: int
    ):
        self.name = name
        self.create_connection_func = create_connection_func
        self.close_connection_func = close_connection_func
        self.health_check_func = health_check_func
        self.reset_connection_func = reset_connection_func
        
        self.pool_size = pool_size
        self.min_connections = min_connections
        self.health_check_interval_s = health_check_interval_s
        
        self._pool: asyncio.Queue = asyncio.Queue(maxsize=pool_size)
        self._active_connections = set()
        self._health_task: Optional[asyncio.Task] = None
        
        # Metrics
        self.replacement_count = 0
        self._checkout_times = {} # conn -> checkout_time

    async def initialize(self):
        logger.info(f"[{self.name}] Initializing pool of size {self.pool_size}")
        
        # MEDIUM-WS-03: Batched initialization with jitter to prevent burst rate limiting
        batch_size = 10
        success_count = 0
        
        for i in range(0, self.pool_size, batch_size):
            if i > 0:
                jitter = random.uniform(0.1, 0.5)
                logger.info(f"[{self.name}] Initialization batch jitter: sleeping for {jitter:.2f}s")
                await asyncio.sleep(jitter)
                
            current_batch_size = min(batch_size, self.pool_size - i)
            tasks = [self.create_connection_func() for _ in range(current_batch_size)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for res in results:
                if isinstance(res, Exception) or res is None:
                    logger.error(f"[{self.name}] Failed to create connection during initialization: {res}")
                else:
                    self._pool.put_nowait(res)
                    success_count += 1
                
        if success_count == 0:
            if os.getenv("LOCAL_TEST", "false").lower() == "true":
                logger.error(f"[{self.name}] FAILED to initialize any connections. (Continuing anyway due to LOCAL_TEST=true)")
            else:
                raise Exception(f"[{self.name}] CRITICAL: Failed to initialize any connections")
            
        self._health_task = asyncio.create_task(self._health_monitor())
        self._lease_task = asyncio.create_task(self._lease_monitor())
        logger.info(f"[{self.name}] Pool initialized successfully with {success_count} connections")

    async def acquire(self, timeout: float = 5.0) -> Any:
        start_time = time.time()
        try:
            # PRD §5: Check health but minimize mid-call delay
            while True:
                # Use remaining time so the overall acquire respects the timeout
                elapsed = time.time() - start_time
                remaining = timeout - elapsed
                if remaining <= 0:
                    raise asyncio.TimeoutError()

                conn = await asyncio.wait_for(self._pool.get(), timeout=remaining)
                # Verify health before handing out
                if await self.health_check_func(conn):
                    self._active_connections.add(conn)
                    self._checkout_times[conn] = asyncio.get_event_loop().time()

                    # Emit wait time metric
                    wait_time_ms = (time.time() - start_time) * 1000
                    self._emit_metrics(wait_time_ms)
                    logger.info(
                        f"[{self.name}] [POOL-ACQUIRE] wait={wait_time_ms:.1f}ms "
                        f"active={len(self._active_connections)} idle={self._pool.qsize()}"
                    )
                    return conn
                else:
                    # Drop dead connection and try next one if time permits
                    logger.warning(f"[{self.name}] Dropped dead connection from pool during acquire")
                    asyncio.create_task(self._replace_connection(conn))
                    # Loop will try to get another one until timeout expires
        except asyncio.TimeoutError:
            logger.warning(f"[{self.name}] Pool exhausted, acquire timed out after {timeout}s")
            self._emit_metrics((time.time() - start_time) * 1000)
            raise PoolExhaustedError(f"Pool {self.name} exhausted after {timeout}s")

    async def release(self, conn: Any):
        held_ms = 0.0
        if conn in self._checkout_times:
            held_ms = (asyncio.get_event_loop().time() - self._checkout_times[conn]) * 1000
        if conn in self._active_connections:
            self._active_connections.discard(conn)
            self._checkout_times.pop(conn, None)
        logger.info(
            f"[{self.name}] [POOL-RELEASE] held={held_ms:.0f}ms "
            f"active={len(self._active_connections)} idle={self._pool.qsize()}"
        )
            
        # Reset state on return
        self.reset_connection_func(conn)
            
        if await self.health_check_func(conn):
            try:
                self._pool.put_nowait(conn)
            except asyncio.QueueFull:
                await self.close_connection_func(conn)
        else:
            await self._replace_connection(conn)
            
        self._emit_metrics()

    async def _replace_connection(self, dead_conn: Any = None):
        if dead_conn:
            await self.close_connection_func(dead_conn)
            
        try:
            conn = await self.create_connection_func()
            if conn:
                try:
                    self._pool.put_nowait(conn)
                    self.replacement_count += 1
                    logger.info(
                        f"[{self.name}] [POOL-RECONNECT] Dead connection replaced "
                        f"(total_replacements={self.replacement_count} idle={self._pool.qsize()})"
                    )
                except asyncio.QueueFull:
                    await self.close_connection_func(conn)
        except Exception as e:
            logger.error(f"[{self.name}] Failed to replace connection: {e}")

    async def _health_monitor(self):
        while True:
            await asyncio.sleep(self.health_check_interval_s)
            
            # 1. Drain and check ALL current idle connections in a burst
            num_to_check = self._pool.qsize()
            for _ in range(num_to_check):
                try:
                    conn = self._pool.get_nowait()
                except asyncio.QueueEmpty:
                    break
                
                is_alive = await self.health_check_func(conn)
                if is_alive:
                    try: self._pool.put_nowait(conn)
                    except asyncio.QueueFull: await self.close_connection_func(conn)
                else:
                    logger.warning(f"[{self.name}] Health check failed in monitor. Replacing.")
                    await self._replace_connection(conn)
                
                # Tiny yield to let acquire() slip in if it's waiting
                await asyncio.sleep(0.01)

            # 2. Ensure minimum connections are maintained
            current_idle = self._pool.qsize()
            total = current_idle + len(self._active_connections)
            if total < self.min_connections:
                needed = self.min_connections - total
                for _ in range(needed):
                    asyncio.create_task(self._replace_connection())
            
            self._emit_metrics()

    async def _lease_monitor(self):
        """
        Antigravity Mechanism: Sweeps orphaned connections (WS-03).
        Runs every 60s to reclaim connections held for > 300s.
        """
        while True:
            await asyncio.sleep(60)
            now = asyncio.get_event_loop().time()
            expired = []
            
            for conn in list(self._active_connections):
                checkout_time = self._checkout_times.get(conn)
                if checkout_time and (now - checkout_time) > 300:
                    expired.append(conn)
            
            for conn in expired:
                logger.error(f"[{self.name}] LEASE EXPIRED for connection held for >300s. Force-reclaiming.")
                # Force release
                await self.release(conn)

    def _emit_metrics(self, wait_time_ms: float = 0.0):
        active = len(self._active_connections)
        idle = self._pool.qsize()
        
        # Determine prefix for emitting
        prefix = "stt_pool" if "STT" in self.name or "Deepgram" in self.name else "tts_pool"
        session_str = "connections" if prefix == "stt_pool" else "sessions"
        
        # Telemetry per PRD
        logger.info(f"[METRIC] {prefix}_active_{session_str}={active}")
        logger.info(f"[METRIC] {prefix}_idle_{session_str}={idle}")
        logger.info(f"[METRIC] {prefix}_replacement_count={self.replacement_count}")
        if wait_time_ms > 0:
            logger.info(f"[METRIC] {prefix}_wait_time_ms={wait_time_ms:.2f}")

    async def close_pool(self):
        if self._health_task:
            self._health_task.cancel()
        if hasattr(self, '_lease_task') and self._lease_task:
            self._lease_task.cancel()
            
        while not self._pool.empty():
            conn = await self._pool.get()
            await self.close_connection_func(conn)
            
        for conn in list(self._active_connections):
            await self.close_connection_func(conn)
            self._active_connections.discard(conn)
            
        logger.info(f"[{self.name}] Pool closed")
