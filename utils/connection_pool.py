"""
utils/connection_pool.py — Pre-warmed STT + TTS connection pools.

Goal: eliminate per-call TCP/TLS handshake latency so the greeting starts
sooner after dial-in.

STT (Deepgram WebSocket)
------------------------
Deepgram STT sessions are stateful per-call (the stream carries one call's
audio from start to finish, then is consumed). The pool keeps N idle
connections open before any call arrives. When a call starts:
  1. `pool.acquire()` hands out a pre-connected socket instantly.
  2. A background task immediately opens a replacement so the pool is
     ready for the next call.
  3. When the call ends, the socket is closed (consumed, not returned).

TTS (ElevenLabs HTTP)
---------------------
ElevenLabs uses HTTPS streaming. Currently, each `synthesize_and_stream()`
call creates a fresh `httpx.AsyncClient`, which pays TCP+TLS per call.
A single shared client maintains an internal keep-alive connection pool;
all synthesis calls reuse connections after the first one.

Usage:
    # At server startup (in run_server.py):
    from orchestrator.factory import warmup_pools
    await warmup_pools()

    # In orchestrator — pools are injected automatically via the factory.
"""

import asyncio
import json
import time
from contextlib import asynccontextmanager

import httpx
import websockets

import config


# ─────────────────────────────────────────────────────────────────────────────
# Deepgram STT pool
# ─────────────────────────────────────────────────────────────────────────────

_STT_URL = (
    "wss://api.deepgram.com/v1/listen"
    f"?model={config.DEEPGRAM_MODEL}"
    f"&encoding={config.AUDIO_ENCODING}"
    f"&sample_rate={config.SAMPLE_RATE}"
    "&channels=1"
    "&punctuate=true"
    "&interim_results=true"
    "&vad_events=true"
    "&endpointing=300"
    "&utterance_end_ms=1000"
    "&no_delay=true"
    "&language=multi"
)

_KEEPALIVE_INTERVAL_S = 3.0


class _IdleSTTConn:
    """Wrapper around a Deepgram WebSocket that keeps itself alive while idle."""

    def __init__(self, ws, keepalive_task: asyncio.Task):
        self.ws = ws
        self._keepalive_task = keepalive_task
        self.created_at = time.monotonic()

    def stop_keepalive(self) -> None:
        """Cancel the idle keepalive — run_stt has its own."""
        self._keepalive_task.cancel()

    async def close(self) -> None:
        self._keepalive_task.cancel()
        try:
            await self._keepalive_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await self.ws.close()
        except Exception:
            pass


class DeepgramPool:
    """
    Pool of pre-opened Deepgram STT WebSocket connections.

    Context-manager interface mirrors `websockets.connect()` so `run_stt` can
    swap from `async with websockets.connect(...) as ws:` to
    `async with stt_pool.acquire() as ws:` with zero change to the body.
    """

    def __init__(self, size: int = 2):
        self._size = size
        self._pool: asyncio.Queue = asyncio.Queue()
        self._running = False

    async def warmup(self) -> None:
        """Open `size` connections concurrently. Call once at server startup."""
        self._running = True
        print(f"[POOL/STT] Pre-warming {self._size} Deepgram connections...")
        results = await asyncio.gather(
            *[self._open_one() for _ in range(self._size)],
            return_exceptions=True,
        )
        opened = sum(1 for r in results if not isinstance(r, Exception))
        print(f"[POOL/STT] {opened}/{self._size} connections ready")

    async def _open_one(self) -> None:
        """Open one Deepgram connection, attach a keepalive, put it in the pool."""
        try:
            ws = await websockets.connect(
                _STT_URL,
                subprotocols=["token", config.DEEPGRAM_API_KEY],
            )
            keepalive_task = asyncio.create_task(self._idle_keepalive(ws))
            await self._pool.put(_IdleSTTConn(ws, keepalive_task))
        except Exception as e:
            print(f"[POOL/STT] Failed to open connection: {type(e).__name__}: {e}")

    @staticmethod
    async def _idle_keepalive(ws) -> None:
        """Keep an idle pooled socket alive with periodic KeepAlive frames."""
        while True:
            await asyncio.sleep(_KEEPALIVE_INTERVAL_S)
            try:
                await ws.send(json.dumps({"type": "KeepAlive"}))
            except Exception:
                return

    @asynccontextmanager
    async def acquire(self):
        """
        Yield a live Deepgram WebSocket.

        Fast path: pool has a pre-warmed socket → return immediately.
        Slow path: pool empty (burst/startup) → open a fresh connection.

        Either way, a replacement is opened asynchronously in the background
        so the pool is ready for the next call.
        """
        conn: _IdleSTTConn | None = None
        try:
            conn = self._pool.get_nowait()
        except asyncio.QueueEmpty:
            pass

        if conn is not None:
            # Hand off to run_stt. Cancel the idle keepalive; run_stt has its own.
            conn.stop_keepalive()
            try:
                await conn._keepalive_task
            except (asyncio.CancelledError, Exception):
                pass
            age_ms = round((time.monotonic() - conn.created_at) * 1000)
            print(f"[POOL/STT] Acquired pre-warmed connection (idle {age_ms}ms)")
            try:
                yield conn.ws
            finally:
                # Close the consumed connection and replenish the pool.
                try:
                    await conn.ws.close()
                except Exception:
                    pass
                if self._running:
                    asyncio.create_task(self._open_one())
        else:
            # Fallback: open a fresh connection directly (burst or first call).
            print("[POOL/STT] Pool empty — opening fresh connection (burst/startup)")
            ws = await websockets.connect(
                _STT_URL,
                subprotocols=["token", config.DEEPGRAM_API_KEY],
            )
            try:
                yield ws
            finally:
                try:
                    await ws.close()
                except Exception:
                    pass
                if self._running:
                    asyncio.create_task(self._open_one())

    async def shutdown(self) -> None:
        """Drain + close all idle connections. Call on server shutdown."""
        self._running = False
        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
                await conn.close()
            except asyncio.QueueEmpty:
                break
        print("[POOL/STT] Shutdown complete")


# ─────────────────────────────────────────────────────────────────────────────
# ElevenLabs TTS pool
# ─────────────────────────────────────────────────────────────────────────────

class ElevenLabsPool:
    """
    Shared httpx.AsyncClient for ElevenLabs synthesis.

    httpx.AsyncClient maintains an internal TCP keep-alive pool. By sharing one
    client instead of creating a new one per synthesis call, subsequent requests
    reuse existing connections and skip TCP+TLS renegotiation.

    The greeting (first synthesis per call) benefits most: with warmup the
    connection to ElevenLabs is already open.
    """

    def __init__(self, max_connections: int = 5):
        limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_connections,
            keepalive_expiry=30,
        )
        self._client = httpx.AsyncClient(timeout=30, limits=limits)

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def warmup(self) -> None:
        """
        Fire a cheap authenticated request to pre-establish the TCP+TLS
        connection so the first synthesis call on any call is fast.
        """
        try:
            headers = {"xi-api-key": config.ELEVENLABS_API_KEY}
            resp = await self._client.get(
                "https://api.elevenlabs.io/v1/models",
                headers=headers,
                timeout=10,
            )
            print(f"[POOL/TTS] ElevenLabs connection warmed (HTTP {resp.status_code})")
        except Exception as e:
            print(f"[POOL/TTS] Warmup request failed (non-fatal): {type(e).__name__}: {e}")
            print("[POOL/TTS] First synthesis will pay the full TCP+TLS cost")

    async def shutdown(self) -> None:
        await self._client.aclose()
        print("[POOL/TTS] Shutdown complete")


# ─────────────────────────────────────────────────────────────────────────────
# Top-level bundle
# ─────────────────────────────────────────────────────────────────────────────

class ConnectionPools:
    """Owns both pools; provides a single warmup/shutdown surface."""

    def __init__(self, stt_size: int = 2):
        self.stt = DeepgramPool(size=stt_size)
        self.tts = ElevenLabsPool()

    async def warmup(self) -> None:
        await asyncio.gather(self.stt.warmup(), self.tts.warmup())

    async def shutdown(self) -> None:
        await asyncio.gather(self.stt.shutdown(), self.tts.shutdown())
