import asyncio
import websockets
import json
import urllib.request
import urllib.error

async def test_ws():
    url = "wss://api.deepgram.com/v1/listen?model=nova-2&encoding=mulaw&sample_rate=8000&interim_results=true&smart_format=true&endpointing=500&detect_language=true"
    headers = {"Authorization": "Token 4f47d854d24c0901384b02a92bcf814fe9545a34"}
    try:
        ws = await websockets.connect(url, additional_headers=headers)
        print("WS Connected!")
        await ws.close()
    except Exception as e:
        print("WS Failed:", type(e), e)

def test_http():
    url = "https://api.deepgram.com/v1/listen?model=nova-2&encoding=mulaw&sample_rate=8000&interim_results=true&smart_format=true&endpointing=500&detect_language=true"
    req = urllib.request.Request(url, method="POST", headers={"Authorization": "Token 4f47d854d24c0901384b02a92bcf814fe9545a34"})
    try:
        with urllib.request.urlopen(req, data=b"fakeaudio") as response:
            print("HTTP Response:", response.read())
    except urllib.error.HTTPError as e:
        print("HTTP Failed:", e.code, e.read().decode('utf-8'))

asyncio.run(test_ws())
test_http()
