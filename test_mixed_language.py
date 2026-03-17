import sys
import os

# Add current directory to path
sys.path.append(os.getcwd())

from contracts.policy import ResponsePolicyEngine

def test_mixed_language_governance():
    policy = ResponsePolicyEngine()
    
    # Mixed languages that SHOULD fail
    test_cases_fail = [
        ("Mujhe admission link nahi mil raha", False), # Hinglish, 1 English word
        ("I want to apply but mera link nahi chal raha", False), # Hinglish, 4 English words
        ("What is the fee for admission fees dena hai", False), # Hinglish mix
        ("Necesito ayuda with my courses", False), # Spanglish
        ("I don't know why it is not working par main kya karu", False), # Hinglish tail
        ("GED registration link please link nahi mil raha", False), # Hinglish
    ]
    
    # Valid English that SHOULD pass
    test_cases_pass = [
        ("What are the specific registration requirements for the cosmetology diploma program?", True),
        ("The admission fee is quite high actually.", True),
        ("Can you tell me about the duration of the GED course?", True),
        ("I am calling regarding the enrollment process for next month.", True),
        ("Is there any financial aid available for international students?", True),
    ]
    
    test_cases = test_cases_fail + test_cases_pass
    
    print("\n--- Mixed Language Governance Test ---")
    all_passed = True
    for text, expected in test_cases:
        # Simulate detected_lang if we want, but let's test pure text first
        is_eng = policy._is_english(text)
        status = "PASS" if is_eng == expected else "FAIL"
        if status == "FAIL":
            all_passed = False
        print(f"[{status}] Text: '{text}' | Expected: {expected} | Actual: {is_eng}")
        
        # Also print density for analysis
        from re import findall
        words = findall(r'\b\w+\b', text.lower())
        common = [w for w in words if w in policy.COMMON_ENGLISH_WORDS]
        density = len(common) / len(words) if words else 0
        print(f"      Density: {density:.2f} ({len(common)}/{len(words)})")
    
    if all_passed:
        print("\n[SUCCESS] All mixed language tests passed!")
    else:
        print("\n[FAILURE] Some tests failed.")

if __name__ == "__main__":
    test_mixed_language_governance()
