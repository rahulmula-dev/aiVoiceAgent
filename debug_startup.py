import sys
import os

# Set root dir
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

print(">>> Testing imports...")
try:
    from retrieval.vector_store import KnowledgeBase
    print("SUCCESS: KnowledgeBase imported")
    kb = KnowledgeBase()
    print("SUCCESS: KnowledgeBase initialized")
except Exception as e:
    print(f"FAILED: KnowledgeBase: {e}")
    import traceback
    traceback.print_exc()

try:
    from crm.client import CRMClient
    print("SUCCESS: CRMClient imported")
    crm = CRMClient()
    print("SUCCESS: CRMClient initialized")
except Exception as e:
    print(f"FAILED: CRMClient: {e}")
    import traceback
    traceback.print_exc()

try:
    from telephony.server import app
    print("SUCCESS: FastAPI app imported")
except Exception as e:
    print(f"FAILED: telephony.server: {e}")
    import traceback
    traceback.print_exc()

print(">>> Import test complete.")
