import asyncio
import websockets
import json

async def test_live_call():
    url = "ws://localhost:8001/chat?token=default-admin-token"
    
    try:
        async with websockets.connect(url) as websocket:
            print("Connected to Live Sandbox (Text Mode)...")
            
            # 1. Listen for the initial greeting
            try:
                response = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                print(f"Agent: {response}")
            except Exception as e:
                print(f"Error receiving greeting: {e}")
                
            # 2. Send the test question
            question = "Can you tell me about the program content of the CS?"
            print(f"\nUser: {question}")
            await websocket.send(question)
            
            # Simulate Barge-in: wait longer for the AI to start speaking
            await asyncio.sleep(5.0) 
            barge_in = "Course?"
            print(f"User (Barge-in): {barge_in}")
            await websocket.send(barge_in)
            
            # 3. Listen for the agent's response stream
            print("Listening for response...")
            full_response = ""
            
            while True:
                try:
                    message_str = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                    try:
                        message = json.loads(message_str)
                        if message.get("type") == "tts_chunk":
                            text = message.get("text", "")
                            print(f"CHUNK: {text}")
                            full_response += text + " "
                    except json.JSONDecodeError:
                        # Sometimes it might just send plain text back
                        print(f"RAW: {message_str}")
                        full_response += message_str + " "
                
                except asyncio.TimeoutError:
                    print("\n=> Response stream finished.")
                    break
                    
            print("\n" + "="*50)
            print("FINAL AGENT RESPONSE:")
            print(full_response.strip())
            print("="*50)
            
            if "Esthetics" in full_response or "MBA" in full_response or "Management" in full_response or "Class" in full_response:
                print("\n✅ SUCCESS: Agent successfully spoke the RAG answer over the live connection!")
            elif "lines are busy" in full_response:
                print("\n❌ FAIL: Agent is still hitting the API quota error.")
            else:
                print("\n⚠️ WARNING: Agent responded, but expected RAG keywords were missing.")
                
    except Exception as e:
         print(f"Connection failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_live_call())
