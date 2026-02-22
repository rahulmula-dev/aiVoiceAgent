
import os
import asyncio
import httpx
from contracts.config import FeatureConfig

async def test_hot_reload():
    print("--- Testing Dynamic Config Reload ---")
    
    # 1. Setup Initial State (Enabled)
    os.environ["INTAKE_ENABLED"] = "true"
    os.environ["ADMIN_RELOAD_TOKEN"] = "test-token"
    
    print(f"Initial INTAKE_ENABLED: {FeatureConfig().is_intake_enabled}")
    
    # 2. Simulate .env file change (we just set os.environ for the mock test)
    # In reality, load_dotenv(override=True) reads from disk.
    # For this script, we'll manually call the reload which will read from the actual .env file.
    # So let's write to .env temporarily.
    
    original_env = ""
    if os.path.exists(".env"):
        with open(".env", "r") as f:
            original_env = f.read()
    
    try:
        # Disable intake in .env
        with open(".env", "a") as f:
            f.write("\nINTAKE_ENABLED=false\n")
        
        print("\nTriggering Hot-Reload via Admin Endpoint (Simulated)...")
        # We simulate the FastAPI endpoint logic here
        new_state = FeatureConfig.reload_dynamic_flags()
        
        print(f"New INTAKE_ENABLED State: {new_state}")
        
        if new_state == False:
            print("PASS: Hot-reload successfully disabled intake.")
        else:
            print("FAIL: Hot-reload did not update state.")

        # 3. Test Orchestrator Check
        from orchestrator.manager import FeatureConfig as FC
        config = FC()
        if config.is_intake_enabled == False:
            print("PASS: Orchestrator-level config check is also updated.")
        else:
            print("FAIL: Orchestrator-level config check is stale.")

    finally:
        # Restore .env
        if original_env:
            with open(".env", "w") as f:
                f.write(original_env)
        else:
            if os.path.exists(".env"):
                 os.remove(".env")

if __name__ == "__main__":
    asyncio.run(test_hot_reload())
