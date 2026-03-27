"""
test_e2e_governance.py
======================
End-to-end governance check: runs every transcript through the full stack
(Interceptor → Policy Engine) exactly as manager.py does in production.

Verifies:
  • Non-English (short & long, many languages) → REFUSED
  • English questions (college domain) → PROCEED + correct intent
  • Mixed/Hinglish → REFUSED by policy density chain
  • Canadian edge cases (French names/streets) → PROCEED
  • 3-strike sequence → correct scripts + terminate flag on strike 3

Run:
    python test_e2e_governance.py
"""

from __future__ import annotations
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from contracts.language_interceptor import LanguageGovernanceInterceptor
from contracts.policy import ResponsePolicyEngine, PRDScripts

G  = "\033[92m"   # green
R  = "\033[91m"   # red
Y  = "\033[93m"   # yellow
C  = "\033[96m"   # cyan
B  = "\033[1m"    # bold
X  = "\033[0m"    # reset

policy = ResponsePolicyEngine()

def check(text, dg_lang=None, ic=None):
    """
    Run text through the interceptor (if provided) then the policy engine.
    Returns (proceed_to_llm, intent, refusal_text, terminate).
    """
    if ic is None:
        ic = LanguageGovernanceInterceptor("e2e")

    result = ic.check(text, deepgram_lang=dg_lang)
    if not result.proceed_to_llm:
        return False, "HARD_REFUSAL_LANGUAGE", result.refusal_text, result.terminate_call

    # Passed interceptor — run full policy classify
    intent = policy.classify_intent(text, detected_lang=dg_lang)
    refusal = policy.get_refusal_script(intent) if intent != "PROCEED" else None
    return True, intent, refusal, False


def _row(label, passed, detail=""):
    sym  = f"{G}PASS{X}" if passed else f"{R}FAIL{X}"
    det  = f"  {Y}{detail}{X}" if detail else ""
    print(f"  [{sym}]  {label}{det}")
    return passed


# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{B}{'='*72}{X}")
print(f"{B}  SECTION 1 — Non-English sentences must be REFUSED{X}")
print(f"{B}  (short words, full sentences, many languages){X}")
print(f"{B}{'='*72}{X}\n")

NON_ENGLISH = [
    # ── Short / single words ──────────────────────────────────────────────
    ("Bonjour",                 "fr",  "French — single word"),
    ("Hola",                    "es",  "Spanish — single word"),
    ("Namaste",                 "hi",  "Hindi — single word"),
    ("Ciao",                    "it",  "Italian — single word"),
    ("Merhaba",                 "tr",  "Turkish — single word"),
    ("Привет",                  None,  "Russian — single word (Cyrillic)"),
    ("こんにちは",               None,  "Japanese — single word (Kanji)"),
    ("مرحبا",                   "ar",  "Arabic — single word"),
    ("ਸਤ ਸ੍ਰੀ ਅਕਾਲ",            None,  "Punjabi — single word (Gurmukhi script)"),

    # ── Medium sentences ──────────────────────────────────────────────────
    ("Bonjour, je voudrais des informations sur les programmes.",
                                "fr",  "French — medium sentence"),
    ("Hola, quisiera información sobre los programas disponibles.",
                                "es",  "Spanish — medium sentence"),
    ("Mujhe admission ke baare mein batao.",
                                "hi",  "Romanised Hindi — no English words"),
    ("Ich möchte mich über die Aufnahmebedingungen informieren.",
                                "de",  "German — medium sentence"),
    ("Voglio sapere come iscrivermi al corso di infermieristica.",
                                "it",  "Italian — medium sentence"),
    ("كيف يمكنني التسجيل في البرنامج الجامعي؟",
                                "ar",  "Arabic — medium sentence"),
    ("저는 간호학과에 등록하고 싶습니다.",
                                "ko",  "Korean — medium sentence"),

    # ── Long / complex sentences ──────────────────────────────────────────
    ("Je voudrais obtenir des informations complètes sur les frais de scolarité "
     "et les conditions d'admission pour le programme de soins infirmiers.",
                                "fr",  "French — long sentence"),
    ("Quisiera saber cuáles son los requisitos de admisión para el programa "
     "de negocios y cuáles son las fechas límite de solicitud.",
                                "es",  "Spanish — long sentence"),
    ("Mujhe yeh jaanna hai ki nursing program mein admission lene ke liye "
     "kya documents chahiye aur fees kitni hogi.",
                                "hi",  "Hinglish — long, mostly Hindi"),
    ("أريد معرفة متطلبات القبول في برنامج تمريض وما هي الرسوم الدراسية "
     "لكل فصل دراسي وكيف يمكنني التقديم.",
                                "ar",  "Arabic — long sentence"),
    ("ਮੈਨੂੰ ਨਰਸਿੰਗ ਪ੍ਰੋਗਰਾਮ ਵਿੱਚ ਦਾਖਲੇ ਦੀਆਂ ਲੋੜਾਂ ਬਾਰੇ ਜਾਣਕਾਰੀ ਚਾਹੀਦੀ ਹੈ।",
                                None,  "Punjabi — long sentence (Gurmukhi script)"),
]

ne_pass = ne_fail = 0
for text, dg_lang, label in NON_ENGLISH:
    proceed, intent, refusal, _ = check(text, dg_lang)
    refused = not proceed or intent == "HARD_REFUSAL_LANGUAGE"
    ok = _row(label, refused,
              f"intent={intent}" if not refused else f"refusal='{(refusal or '')[:60]}...'")
    if ok: ne_pass += 1
    else:  ne_fail += 1


# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{B}{'='*72}{X}")
print(f"{B}  SECTION 2 — English questions must PROCEED to Gemini{X}")
print(f"{B}  (college domain, casual speech, accented names){X}")
print(f"{B}{'='*72}{X}\n")

ENGLISH_PROCEED = [
    # ── Short / casual ────────────────────────────────────────────────────
    ("Hi",                      "en",  "Greeting — single word"),
    ("Yes",                     "en",  "Affirmation"),
    ("Okay",                    "en",  "Filler"),
    ("Yeah, okay.",             "en",  "Casual filler phrase"),

    # ── Name introductions with foreign-origin names ───────────────────────
    ("Hi, my name is Priya.",          "en",  "Indian name introduction"),
    ("My name is Jaspreet Dhaliwal.",  "en",  "Punjabi name introduction"),
    ("I am Ahmed, calling from Calgary.", "en", "Arabic name in English sentence"),
    ("This is María, I want to apply.", "en", "Spanish name introduction"),

    # ── College domain questions ──────────────────────────────────────────
    ("What are the tuition fees for the nursing program?",
                                "en",  "Fees query"),
    ("Can you tell me about the admission requirements for the business diploma?",
                                "en",  "Admissions query"),
    ("When does the fall intake start?",
                                "en",  "Intake date query"),
    ("Is the software program available online or only in-person?",
                                "en",  "Study mode query"),
    ("How do I apply for the esthetics certificate course?",
                                "en",  "Application query"),
    ("What documents do I need to submit for enrollment?",
                                "en",  "Documents query"),
    ("Does GD College offer payment plans for tuition?",
                                "en",  "Payment plan query"),
    ("I would like to know about the hairstyling diploma program.",
                                "en",  "Program info query"),
    ("What is the duration of the massage therapy program?",
                                "en",  "Duration query"),
    ("Are there any scholarships available for international students?",
                                "en",  "Scholarship query"),

    # ── Canadian edge cases — French proper nouns inside English ──────────
    ("I live on Rue de la Gare and I want to apply.",
                                "en",  "CA French street name in English"),
    ("I'm calling from Saint-Jean-sur-Richelieu about the nursing program.",
                                "en",  "Quebec city name in English"),
    ("My previous school was École Polytechnique, can I transfer credits?",
                                "en",  "French institution name in English"),

    # ── Accented / non-standard English ───────────────────────────────────
    ("I want informations about the fees, please.",
                                "en",  "ESL-style grammar, still English"),
    ("Tell me more about program and how I can joining.",
                                "en",  "Broken English — should proceed"),
]

en_pass = en_fail = 0
for text, dg_lang, label in ENGLISH_PROCEED:
    proceed, intent, refusal, _ = check(text, dg_lang)
    ok = _row(label, proceed and intent != "HARD_REFUSAL_LANGUAGE",
              f"intent={intent}")
    if ok: en_pass += 1
    else:  en_fail += 1


# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{B}{'='*72}{X}")
print(f"{B}  SECTION 3 — Policy hard refusals (non-college topics){X}")
print(f"{B}  (English but out-of-scope — correct refusal script returned){X}")
print(f"{B}{'='*72}{X}\n")

HARD_REFUSALS = [
    ("Can you help me with my visa application?",
     "HARD_REFUSAL_IMMIGRATION",  "Immigration query"),
    ("I need medical advice about my condition.",
     "HARD_REFUSAL_MEDICAL",      "Medical query"),
    ("I want to sue the college, I need a lawyer.",
     "HARD_REFUSAL_LEGAL",        "Legal query"),
    ("What is the salary of the teachers here?",
     "HARD_REFUSAL_INTERNAL_STAFF", "Internal staff query"),
    ("How does GD College compare to other universities?",
     "HARD_REFUSAL_COMPETITORS",  "Competitor comparison"),
    ("I want my money back, you overcharged me.",
     "HARD_REFUSAL_FINANCIAL_DISPUTES", "Financial dispute"),
    ("Please translate that into Spanish for me.",
     "HARD_REFUSAL_LANGUAGE_BYPASS", "Language bypass attempt"),
]

hr_pass = hr_fail = 0
for text, expected_intent, label in HARD_REFUSALS:
    proceed, intent, refusal, _ = check(text, "en")
    ok = _row(label, intent == expected_intent,
              f"got={intent}  script='{(refusal or '')[:55]}...'")
    if ok: hr_pass += 1
    else:  hr_fail += 1


# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{B}{'='*72}{X}")
print(f"{B}  SECTION 4 — 3-Strike sequence (same session){X}")
print(f"{B}  Bonjour x3 → correct scripts + terminate on strike 3{X}")
print(f"{B}{'='*72}{X}\n")

ic = LanguageGovernanceInterceptor("strike-sequence-test")
sequence = [
    ("Bonjour",                         "fr"),
    ("Je voudrais parler en français.",  "fr"),
    ("S'il vous plaît, parlez français.","fr"),
]

strike_pass = strike_fail = 0
for i, (text, dg_lang) in enumerate(sequence, 1):
    result = ic.check(text, deepgram_lang=dg_lang)
    expected_terminate = (i == 3)
    expected_script    = PRDScripts.REFUSAL_LANGUAGE_3 if i == 3 else PRDScripts.REFUSAL_LANGUAGE_1

    ok_script    = result.refusal_text == expected_script
    ok_terminate = result.terminate_call == expected_terminate
    ok_strike    = result.strike == i
    ok           = ok_script and ok_terminate and ok_strike

    detail = (
        f"strike={result.strike}  terminate={result.terminate_call}  "
        f"script='{(result.refusal_text or '')[:55]}...'"
    )
    passed = _row(f"Strike {i}", ok, detail)
    if passed: strike_pass += 1
    else:       strike_fail += 1

# Verify websocket.close() called exactly once
from unittest.mock import MagicMock
ic2   = LanguageGovernanceInterceptor("ws-close-test")
ws    = MagicMock()
for text, lang in sequence:
    r = ic2.check(text, deepgram_lang=lang)
    if r.terminate_call:
        ws.close()
ws_ok = ws.close.call_count == 1
passed = _row("websocket.close() called exactly once after strike 3", ws_ok,
              f"call_count={ws.close.call_count}")
if ws_ok: strike_pass += 1
else:      strike_fail += 1


# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{B}{'='*72}{X}")
print(f"{B}  SUMMARY{X}")
print(f"{B}{'='*72}{X}\n")

total_pass = ne_pass + en_pass + hr_pass + strike_pass
total_fail = ne_fail + en_fail + hr_fail + strike_fail
total      = total_pass + total_fail

def _bar(label, p, f):
    col = G if f == 0 else R
    print(f"  {label:<45} {col}{p}/{p+f}{X}")

_bar("Non-English refused (short + long sentences)", ne_pass, ne_fail)
_bar("English questions pass to Gemini",             en_pass, en_fail)
_bar("Hard refusals (out-of-scope English topics)",  hr_pass, hr_fail)
_bar("3-Strike sequence + websocket.close()",        strike_pass, strike_fail)
print()
col = G if total_fail == 0 else R
print(f"  {B}Total: {col}{total_pass}/{total} passed{X}")

if ne_fail > 0:
    print(f"\n  {Y}NOTE: Non-English FAILs above mean the guard layer (Lingua/FastText){X}")
    print(f"  {Y}passes those texts through. On EC2 with FastText, most of these will{X}")
    print(f"  {Y}be caught. Gemini's secondary filter is the final backstop.{X}")

print()
sys.exit(0 if total_fail == 0 else 1)
