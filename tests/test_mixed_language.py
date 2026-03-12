
import sys
import os
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).parent.parent))

from contracts.policy import ResponsePolicyEngine

policy = ResponsePolicyEngine()

test_cases = [
    # --- PURE ENGLISH (Should PASS) ---
    ("Hello! I want to know about admissions.", None, True),
    ("Can you tell me the fee structure for the diploma course?", None, True),
    ("Yes, please help me with the process.", None, True),
    ("Admission details", None, True),
    ("Okay", None, True),

    # --- PURE FOREIGN (Should FAIL) ---
    ("Hola, como estas?", "es", False),
    ("Mujhe hindi mein baat karni hai", "hi", False),
    ("Bonjour", "fr", False),

    # --- MIXED/HINGLISH (Should FAIL) ---
    ("Mujhe admission information chahiye", None, False), # 2 keywords, 4 words -> 50% density
    ("Can you tell me aapka fees structure?", None, False), # 5 keywords, 7 words -> ~71% density (Wait, I added 'structure', so density is high, but 'aapka' is foreign)
    ("Aapka campus kahan hai?", None, False), # 2 keywords, 4 words -> 50% density
    ("I want to apply but thoda issue hai", None, False), # 4 keywords, 8 words -> 50% density
    ("Tell me about process and how to apply in Hindi", None, False), # 6 keywords, 10 words -> 60% density
]

print(f"{'Text':<50} | {'Expected':<10} | {'Actual':<10} | {'Result'}")
print("-" * 85)

for text, detected_lang, expected in test_cases:
    is_eng = policy._is_english(text, detected_lang=detected_lang)
    intent = policy.classify_intent(text, detected_lang=detected_lang)
    
    # Text is considered 'Passed' if it is English AND intent is PROCEED
    actual = is_eng and intent == "PROCEED"
    
    result = "PASS" if actual == expected else "FAIL"
    print(f"{text:<50} | {str(expected):<10} | {str(actual):<10} | {result} ({intent})")
