import sys
import os

# Add current directory to path
sys.path.append(os.getcwd())

from contracts.policy import PRDScripts, ResponsePolicyEngine

def test():
    print("--- 1. Testing PRDScripts.INTERRUPTION removal ---")
    try:
        phrase = PRDScripts.INTERRUPTION
        print(f"FAIL: PRDScripts.INTERRUPTION still exists: {phrase}")
    except AttributeError:
        print("PASS: PRDScripts.INTERRUPTION is undefined.")
    except Exception as e:
        print(f"FAIL: Error checking INTERRUPTION: {e}")

    print("\n--- 2. Testing AMBIGUOUS intent ---")
    policy = ResponsePolicyEngine()
    
    cases = [
        ("", "AMBIGUOUS"),
        ("the", "AMBIGUOUS"),
        ("mhm", "AMBIGUOUS"),
        ("tell me about fees", "PROCEED")
    ]
    
    for text, expected in cases:
        result = policy.classify_intent(text)
        if result == expected:
            print(f"PASS: '{text}' -> {result}")
        else:
            print(f"FAIL: '{text}' -> {result} (Expected: {expected})")

    print("\n--- 3. Testing Partial Match logic ---")
    cases = [
        ("visastatus", "HARD_REFUSAL_IMMIGRATION"),
        ("salarypayment", "HARD_REFUSAL_INTERNAL_STAFF")
    ]
    for text, expected in cases:
        result = policy.classify_intent(text)
        if result == expected:
            print(f"PASS: '{text}' -> {result}")
        else:
            print(f"FAIL: '{text}' -> {result} (Expected: {expected})")

if __name__ == "__main__":
    test()
