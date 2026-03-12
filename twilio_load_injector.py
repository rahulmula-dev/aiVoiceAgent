import os
import asyncio
import httpx
import uuid
from dotenv import load_dotenv

# Load credentials from .env
load_dotenv()

TARGET_PHONE = os.getenv("TWILIO_PHONE_NUMBER") 
FROM_PHONE = "+18563936660"  

LOCAL_WEBHOOK_URL = "http://localhost:8000/voice"

async def simulate_direct_webhook(client: httpx.AsyncClient, index: int):
    # Stagger requests logically by index (0ms, 100ms, 200ms...) 
    # This prevents Windows TCP Drops AND bypasses Ngrok free-tier connection rate limits!
    await asyncio.sleep(index * 0.1)
    
    fake_sid = "CA" + str(uuid.uuid4()).replace("-", "")[:30]
    data = {"CallSid": fake_sid, "From": FROM_PHONE, "To": TARGET_PHONE}
    
    try:
        response = await client.post(LOCAL_WEBHOOK_URL, data=data, timeout=30.0)
        if "All our lines are busy at the moment, but I will arrange a callback." in response.text:
            return "REJECTED", f"🛑 [Call {index:02d}] Server REJECTED capacity (Sent Busy Signal)"
        else:
            return "ACCEPTED", f"🟢 [Call {index:02d}] Server ACCEPTED caller! (Sent Connect Stream)"
    except Exception as e:
        return "ERROR", f"⚠️ [Call {index:02d}] Error: {repr(e)}"

async def run_load_test(worker_count: int):
    print(f"\n🚀 Engaging {worker_count} Concurrent Local Webhook Simulations...")
    
    async with httpx.AsyncClient() as client:
        tasks = [simulate_direct_webhook(client, i) for i in range(1, worker_count + 1)]
        results = await asyncio.gather(*tasks)
        
        accepted = sum(1 for status, msg in results if status == "ACCEPTED")
        rejected = sum(1 for status, msg in results if status == "REJECTED")
        
        results.sort(key=lambda x: x[1])
        for status, msg in results:
            print(msg)
            
        print(f"\n📈 Server Webhook Evaluation Complete!")
        print(f"Total Requests: {worker_count}")
        print(f"🟢 Server ACCEPTED: {accepted}")
        print(f"🛑 Server REJECTED: {rejected}")

if __name__ == "__main__":
    import sys
    print("====== LOCAL WEBHOOK LOAD INJECTOR (S4-7 Validation) ======")
    if len(sys.argv) > 1:
        choice = sys.argv[1]
    else:
        choice = input("\nEnter number of concurrent calls to inject (e.g., 35): ")
    try:
        worker_count = int(choice)
        if worker_count > 0:
            asyncio.run(run_load_test(worker_count))
    except ValueError:
        pass
