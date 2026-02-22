
import os
import asyncio
from crm.client import CRMClient, SecurityError

async def test_security_guard():
    print("--- Testing CRM Security Guard ---")
    
    # CASE 1: Staging with Prod Key (Should FAIL)
    os.environ["APP_ENV"] = "staging"
    os.environ["CRM_API_KEY"] = "sk_live_prod_key_123"
    print("Attempting init with Staging + Prod Key...")
    try:
        CRMClient()
        print("FAIL: Security Guard did not catch prod key in staging!")
    except SecurityError as e:
        print(f"PASS: Caught security violation: {e}")
    except Exception as e:
        print(f"FAIL: Unexpected error: {type(e).__name__}: {e}")

    # CASE 2: Staging with Test Key (Should PASS)
    os.environ["CRM_API_KEY"] = "sk_test_staging_key_456"
    print("\nAttempting init with Staging + Test Key...")
    try:
        CRMClient()
        print("PASS: Security Guard allowed test key in staging.")
    except Exception as e:
        print(f"FAIL: Unexpectedly blocked test key: {e}")

    # CASE 3: Production with Prod Key (Should PASS)
    os.environ["APP_ENV"] = "production"
    os.environ["CRM_API_KEY"] = "sk_live_actual_production_key"
    print("\nAttempting init with Production + Prod Key...")
    try:
        CRMClient()
        print("PASS: Allowed prod key in production.")
    except Exception as e:
        print(f"FAIL: Blocked prod key in production: {e}")

if __name__ == "__main__":
    asyncio.run(test_security_guard())
