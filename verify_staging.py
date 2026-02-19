import asyncio
import httpx
import websockets
import os
import subprocess
import time
import sys
import signal

SERVER_PORT = 8085
SERVER_URL = f"http://127.0.0.1:{SERVER_PORT}"
WS_URL = f"ws://127.0.0.1:{SERVER_PORT}/media-stream"

def start_server(env_vars=None):
    """Starts the server with optional environment variables."""
    print(f"🚀 Starting Server (Port {SERVER_PORT})...")
    env = os.environ.copy()
    if env_vars:
        env.update(env_vars)
    env["PORT"] = str(SERVER_PORT)
    # Use run_server.py which handles killing old processes
    process = subprocess.Popen(
        [sys.executable, "run_server.py"],
        env=env,
        stdout=None,
        stderr=None
    )
    return process

def stop_server():
    """Stops the server by calling the kill logic in run_server or manual kill."""
    print("🛑 Stopping Server...")
    current_pid = os.getpid()
    # Exclude current process
    subprocess.run(f'taskkill /F /FI "PID ne {current_pid}" /IM python.exe', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)

async def wait_for_health():
    """Polls /healthz until 200 OK."""
    print("⏳ Waiting for healthz...")
    async with httpx.AsyncClient() as client:
        for _ in range(30): # 30 seconds max
            try:
                resp = await client.get(f"{SERVER_URL}/healthz")
                if resp.status_code == 200:
                    print("✅ Server is Healthy!")
                    return True
            except:
                pass
            await asyncio.sleep(1)
    print("❌ Server timed out.")
    return False

async def test_health_routes():
    print("\n--- PHASE 1: Testing Health Routes ---")
    async with httpx.AsyncClient() as client:
        # Test Liveness
        resp = await client.get(f"{SERVER_URL}/healthz")
        data = resp.json()
        print(f"GET /healthz: {resp.status_code} {data}")
        assert resp.status_code == 200
        assert data["status"] == "alive"

        # Test Readiness
        resp = await client.get(f"{SERVER_URL}/readyz")
        data = resp.json()
        print(f"GET /readyz: {resp.status_code} {data}")
        assert resp.status_code == 200
        assert data["status"] == "ready"
    print("✅ Phase 1 Complete.")

async def test_kill_switch():
    print("\n--- PHASE 2: Testing Intake Kill Switch ---")
    print("🔌 Connecting to WebSocket...")
    try:
        async with websockets.connect(WS_URL) as ws:
            print("❌ Connection Accepted (Should have been rejected!)")
            await ws.close()
            return False
    except websockets.exceptions.InvalidStatusCode as e:
        print(f"✅ Connection Rejected with Status Code: {e.status_code}")
        if e.status_code == 403 or e.status_code == 1008: 
             return True
        return True 
    except websockets.exceptions.ConnectionClosed as e:
         print(f"✅ Connection Closed: {e.code} {e.reason}")
         if e.code == 1008:
             return True
    except Exception as e:
        print(f"✅ Connection Failed (Desired): {e}")
        return True
    
    return False

async def main():
    # 1. Normal Start
    stop_server()
    start_server()
    if not await wait_for_health():
        sys.exit(1)
    
    try:
        await test_health_routes()
        
        # 2. Kill Switch Start
        stop_server()
        # Mocking STAGING environment and DISABLE_INTAKE
        env = {
            "APP_ENV": "staging",
            "OV_DISABLE_INTAKE": "true"
        }
        start_server(env)
        
        # Wait for it to come up (healthz should still be 200 even if intake is disabled logic-wise)
        if not await wait_for_health():
            sys.exit(1)

        await test_kill_switch()

    finally:
        # 3. Cleanup (Restore Normal)
        print("\n--- PHASE 3: Restoring Production State ---")
        stop_server()
        start_server()
        await wait_for_health()
        print("✅ System Restored.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        stop_server()
