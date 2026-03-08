
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).parent.parent))

from contracts.policy import ResponsePolicyEngine

policy = ResponsePolicyEngine()

test_cases = [
    # text, expected_intent
    ("uh", "AMBIGUOUS"),
    ("um", "AMBIGUOUS"),
    ("hmm", "AMBIGUOUS"),
    ("ah", "AMBIGUOUS"),
    ("mhm", "AMBIGUOUS"),
    ("uh um", "PROCEED"), # Multiple might indicates actual speech attempt
    ("Okay", "PROCEED"), # Common affirmation should proceed
    ("What are the fees?", "PROCEED"), # Actual question
]

print(f"{'Text':<20} | {'Expected':<15} | {'Actual':<15} | {'Result'}")
print("-" * 60)

for text, expected in test_cases:
    actual = policy.classify_intent(text)
    result = "PASS" if actual == expected else "FAIL"
    print(f"{text:<20} | {expected:<15} | {actual:<15} | {result}")
