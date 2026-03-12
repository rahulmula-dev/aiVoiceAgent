# Root Server Runner - GD College AI Voice Agent
import os
import sys
import uvicorn
from dotenv import load_dotenv

# Ensure the root directory is in sys.path for modular imports to work
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

# Load environment variables
load_dotenv()

from telephony.server import app

if __name__ == "__main__":
    # Change the default port to 8001 to avoid conflicts on 8000
    PORT = int(os.getenv("PORT", 8001))
    print(f"\n>>> Starting AI Voice Agent Server at http://localhost:{PORT}")
    print(f">>> TEST CHAT UI AVAILABLE AT: http://localhost:{PORT}/chat-ui")
    print(f">>> Root Directory: {ROOT_DIR}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
