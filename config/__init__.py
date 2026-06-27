import os
from dotenv import load_dotenv

from retrieval.gd_college_data import gd_college_raw_data

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# API Keys
# ─────────────────────────────────────────────────────────────────────────────
DEEPGRAM_API_KEY   = os.getenv("DEEPGRAM_API_KEY")
GROQ_API_KEY       = os.getenv("GROQ_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

# ─────────────────────────────────────────────────────────────────────────────
# STT  →  currently: Deepgram Nova-3
# To swap: point stt/deepgram_stt.py to a new provider and keep the same
#          run_stt(audio_queue, transcript_queue, barge_in_event) signature.
# ─────────────────────────────────────────────────────────────────────────────
STT_PROVIDER   = "deepgram"
DEEPGRAM_MODEL = "nova-3"

# ─────────────────────────────────────────────────────────────────────────────
# LLM  →  currently: Groq llama-3.1-8b-instant (free)
# To swap: implement llm/<provider>_llm.py with the same
#          run_llm(transcript_queue, text_queue) signature.
# ─────────────────────────────────────────────────────────────────────────────
#
# LLM_PROVIDER:
#   "groq"   -> use llm/groq_llm.py   (default, current working setup)
#   "gemini" -> use llm/gemini_llm.py (Step 8 — requires GEMINI_API_KEY)
#
# Switch by setting LLM_PROVIDER=gemini in .env. Both implementations share
# the same run_llm() signature so the orchestrator code is provider-agnostic.
LLM_PROVIDER       = os.getenv("LLM_PROVIDER", "groq").lower()

# llama-3.1-8b-instant is Groq's worst tool-caller and is deprecating
# 2026-08-16. openai/gpt-oss-20b is a near-drop-in replacement with much
# better tool-calling reliability on the free tier.
# Other free-tier alternatives:
#   moonshotai/kimi-k2-instruct-0905  — best for agentic tool use
#   openai/gpt-oss-120b               — flagship replacement for llama-3.3-70b
GROQ_MODEL         = "llama-3.1-8b-instant"
GROQ_TEMPERATURE   = 0.3   # lower temp → fewer malformed tool calls

# ─────────────────────────────────────────────────────────────────────────────
# Gemini config (Step 8) — only used when LLM_PROVIDER=gemini
# ─────────────────────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# gemini-2.0-flash is the default — fast, cheap, free tier available.
# Other options: gemini-2.5-flash (newer, slightly slower) or gemini-1.5-flash.
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

def _build_corpus() -> str:
    """
    Format the GD College knowledge base into a categorized block for the
    system prompt. Pure-policy records (those with a hard_refusal_category)
    are skipped here and re-stated as explicit refusal rules further down,
    so the LLM treats them as behavior rather than retrievable knowledge.
    """
    by_cat: dict[str, list[str]] = {}
    for rec in gd_college_raw_data:
        if rec.get("hard_refusal_category"):
            continue
        by_cat.setdefault(rec["category"], []).append(rec["text"])

    lines: list[str] = []
    for cat in by_cat:
        lines.append(f"\n[{cat.upper()}]")
        for text in by_cat[cat]:
            lines.append(f"- {text}")
    return "\n".join(lines)


SYSTEM_PROMPT = (
    "You are the voice assistant for GD College in Calgary, Alberta — a "
    "beauty and cosmetology school. You answer caller questions about "
    "programs, admissions, fees, schedules, student services, and alumni "
    "support.\n\n"
    "VOICE RULES:\n"
    "- Keep replies to 1-2 sentences. The caller will ask follow-ups.\n"
    "- Never use markdown, asterisks, bullet points, dashes-as-bullets, or pound signs.\n"
    "- Speak in plain conversational English. Read numbers and dates naturally.\n\n"
    "KNOWLEDGE — Use ONLY the facts below. Never invent program names, "
    "prices, dates, salary figures, or policies.\n"
    + _build_corpus()
    + "\n\n"
    "COVERAGE GAPS — what to do when the caller's question isn't directly "
    "answered by the knowledge above. Always check HARD REFUSALS (further "
    "below) first; those override everything.\n"
    "- INAPPROPRIATE / OFFENSIVE: If the caller asks anything rude, sexual, "
    "violent, illegal, or otherwise inappropriate, respond ONLY with: "
    "\"I can't answer that.\" Do not engage further on that topic.\n"
    "- OFF-TOPIC BUT HARMLESS: If the caller asks something unrelated to GD "
    "College (weather, jokes, news, general trivia, math, other businesses), "
    "respond with: \"I can only help with GD College questions — is there "
    "anything about our programs or admissions I can assist with?\" Do not "
    "attempt to answer the off-topic question.\n"
    "- COLLEGE-RELATED BUT NOT IN KNOWLEDGE: If the caller asks something "
    "that IS about GD College but the knowledge above does not cover it "
    "(e.g. specific instructor names, exam dates, refund policy, dorms, "
    "campus tours, course content), apologize briefly, then ASK for their "
    "full name and phone number and promise that a college associate will "
    "call them back. Example phrasing: \"I don't have that specific "
    "information on hand. If you can give me your full name and phone "
    "number, a college associate will get back to you shortly.\" Once they "
    "give the details, read them back to confirm, then thank them. Do NOT "
    "invent facts to fill the gap.\n\n"
    "HARD REFUSALS (these override everything else):\n"
    "- IMMIGRATION/VISA: If the caller asks about visas, study permits, "
    "permanent residency, or any immigration matter, respond ONLY with: "
    "\"I can't help with immigration questions — please contact IRCC "
    "directly through canada.ca.\" Do not engage further on that topic.\n"
    "- LEGAL/HARASSMENT: If the caller mentions lawsuits, legal threats, "
    "or harassment, respond ONLY with: \"Please direct any legal matters "
    "to our legal department.\" Do not engage further.\n"
    "- CALL DURATION: Our automated calls are limited to 5 minutes for "
    "fair access. If the conversation approaches that, politely wrap up.\n"
)

GREETING = (
    "Hi! You've reached GD College in Calgary. I can answer questions "
    "about our programs, schedules, fees, and admissions. How can I help you today?"
)

# No tools in the inline-corpus design. Kept as an empty list so the
# tool-call branch in llm/groq_llm.py stays dormant and the architecture
# is preserved for future additions (e.g. a campus-tour booking tool).
TOOLS: list = []

# ─────────────────────────────────────────────────────────────────────────────
# TTS  →  currently: ElevenLabs (eleven_flash_v2_5 for low latency)
# To swap: implement tts/<provider>_tts.py with the same
#          run_tts(text_queue, twilio_ws, streamsid, barge_in_event) signature.
# ─────────────────────────────────────────────────────────────────────────────
TTS_PROVIDER             = "elevenlabs"
# Premade (default) voice IDs — accessible on Free tier. Library/shared voices
# return HTTP 402 on Free, so we default to one of the premades shipped with
# every account. Run `python list_voices.py` to see what your account has.
#   21m00Tcm4TlvDq8ikWAM  Rachel   (calm, female)
#   pNInz6obpgDQGcFmaJgB  Adam     (deep, male)
#   EXAVITQu4vr4xnSDxMaL  Sarah    (soft, female)
#   nPczCjzI2devNBz1zQrb  Brian    (warm, male)
#   9BWtsMINqrJLrRacOk9x  Aria     (expressive, female)
ELEVENLABS_VOICE_ID      = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")  # Rachel (premade)
ELEVENLABS_MODEL_ID      = "eleven_flash_v2_5"  # ~75ms latency
ELEVENLABS_OUTPUT_FORMAT = "ulaw_8000"           # raw mulaw 8kHz → Twilio-ready, no conversion needed

# ─────────────────────────────────────────────────────────────────────────────
# Audio  (Twilio phone calls use mulaw 8kHz)
# ─────────────────────────────────────────────────────────────────────────────
AUDIO_ENCODING = "mulaw"
SAMPLE_RATE    = 8000
BUFFER_SIZE    = 20 * 160  # 20 frames × 160 bytes each = standard mulaw chunk

# ─────────────────────────────────────────────────────────────────────────────
# Governance feature flag (Step 4)
#
# When True, VoiceOrchestrator creates a LanguageGovernanceInterceptor and
# ResponsePolicyEngine per call, and the LLM loop short-circuits restricted-
# topic queries with canned PRDScripts refusals instead of calling Groq.
#
# Set GOVERNANCE_ENABLED=true in .env (or the shell) to turn on. Default is
# False so the existing prompt-level guardrails keep working unchanged and a
# regression in the new code can't break a live demo.
# ─────────────────────────────────────────────────────────────────────────────
GOVERNANCE_ENABLED = os.getenv("GOVERNANCE_ENABLED", "true").lower() in ("1", "true", "yes", "on")

# ─────────────────────────────────────────────────────────────────────────────
# RAG feature flag (Step 7)
#
# When True, VoiceOrchestrator creates a KnowledgeBase per call and run_llm
# injects pgvector-retrieved context into each user message before calling
# the LLM. Falls back silently to the inline SYSTEM_PROMPT corpus if Postgres
# is unreachable or search returns LOW_CONFIDENCE_FALLBACK.
#
# Prerequisites before setting RAG_ENABLED=true:
#   1. docker-compose up -d postgres
#   2. uv run python -m retrieval.migrate_to_pgvector
#   3. PG_DATABASE_URL set in .env
#
# LOCAL_TEST=true (default) uses mock embeddings ([1.0]*1536) — no AWS needed.
# LOCAL_TEST=false requires AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY in .env.
# ─────────────────────────────────────────────────────────────────────────────
RAG_ENABLED     = os.getenv("RAG_ENABLED",  "false").lower() in ("1", "true", "yes", "on")
LOCAL_TEST      = os.getenv("LOCAL_TEST",   "true").lower()  in ("1", "true", "yes", "on")
PG_DATABASE_URL = os.getenv("PG_DATABASE_URL", "")

# ─────────────────────────────────────────────────────────────────────────────
# Concurrency gate (Step 9)
#
# When True, /voice checks a Redis counter before admitting a call.
# Calls beyond MAX_CONCURRENT_CALLS receive a "try again later" TwiML response
# and are never connected to the WebSocket pipeline.
#
# Prerequisites:
#   docker-compose up -d redis
#   REDIS_URL=redis://localhost:6379 in .env
#
# In docker-compose the URL is redis://redis:6379 (service name as hostname).
# Locally (running outside Docker) use redis://localhost:6379.
# ─────────────────────────────────────────────────────────────────────────────
CONCURRENCY_GATE_ENABLED = os.getenv("CONCURRENCY_GATE_ENABLED", "false").lower() in ("1", "true", "yes", "on")
REDIS_URL                = os.getenv("REDIS_URL", "redis://localhost:6379")
MAX_CONCURRENT_CALLS     = int(os.getenv("MAX_CONCURRENT_CALLS", "5"))
