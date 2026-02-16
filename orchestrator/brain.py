import os
import re
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted  # Add this import
import logging
import asyncio
from retrieval.vector_store import KnowledgeBase  # New RAG module
from dotenv import load_dotenv

# Configure logging
logger = logging.getLogger("Brain")

load_dotenv()

from contracts.interfaces import LLMEngine

# Pillar 2: Anti-Freeze Timeouts
LLM_TIMEOUT = 12.0
RAG_TIMEOUT = 5.0

class Brain(LLMEngine):
    # 1. DEFINE SOURCE OF TRUTH FOR REFUSAL SCRIPT
    KB_MISS_SCRIPT = "I do not have that information. A staff member will follow up."

    def __init__(self, call_logger=None, crm_client=None):
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.call_logger = call_logger
        self.crm_client = crm_client
        
        # 1. Initialize Knowledge Base
        self.kb = KnowledgeBase()

        # --- ATTEMPT CONNECTION ---
        try:
            # 2. Define Instructions (Injecting the Constant)
            self.system_instruction = f"""
            You are CILA (often mis-transcribed as "Tina", "Sheila", or "Peter"), the friendly and professional AI Voice Agent for GD College in Calgary, Alberta.
            
            CORE INSTRUCTIONS:
            1. CONVERSATIONAL PRIORITY: 
               - If the user greets you (Hello, Hi, Hey) or asks "How are you?", respond conversationally and briefly: "I'm doing well, thank you! How can I help you with GD College today?"
               - This is a warm conversation. Do NOT use strict refusals for simple greetings or casual questions.

            2. LANGUAGE GUARDRAIL:
               - You are an English-only agent.
               - You MUST refuse if the input is CLEARLY another language (like sustained Hindi, Spanish, etc.) or if it is "phonetic gibberish" (nonsensical English words that result from forcing a non-English language through your English model).
               - DO NOT refuse broken English, slight repetitions, or conversational fillers.
               - Refusal Output: "I am currently designed to support English only. Please contact our admission office for assistance."

            3. COLLEGE KNOWLEDGE (RAG):
               - Answer questions about GD College using the provided [CONTEXT].
               - If the [CONTEXT] does not contain the answer to a college-specific query, say: "{self.KB_MISS_SCRIPT}"
               - IMPORTANT: Do NOT use this refusal for greetings or polite talk.

            4. CONCISE & ACCURATE:
               - Keep answers to 1 or 2 short sentences.
               - Never invent facts. If unsure about college details, refer to the follow-up phrase above.
            
            5. SOURCE OF TRUTH HIERARCHY:
               - [KB CONTEXT]: The DEFINITIVE source for general college information (programs, fees, dates).
               - [CRM DATA]: Use this ONLY for specific user status updates.
               - RESTRICTION: CRM data must NEVER override general KB facts (e.g. if CRM says "Fees: 0" because the student paid, strictly say "Your balance is 0", do NOT say "The college has no fees").

            
            6. LIMITS: No immigration, medical, or legal advice.
            """

            # 3. SAFETY SETTINGS (Relaxed to prevent blocked responses for harmless RAG queries)
            safety_settings = [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ]

            genai.configure(api_key=self.api_key)
            
            self.model_name = 'gemini-2.5-flash'
            self.model = genai.GenerativeModel(
                model_name=self.model_name,
                system_instruction=self.system_instruction,
                safety_settings=safety_settings
            )
            
            # print(f"--- USING FLASH LITE MODEL: {self.model_name} ---") <--- REMOVED
            # logger.info(...) <--- ALREADY THERE
            
            logger.info(f"Brain Init: Model={self.model_name}, Key=...{self.api_key[-5:] if self.api_key else 'NONE'}")
            # print(f"[SUCCESS] Brain Ready with RAG ({self.model_name})") <--- REMOVED
        except Exception as e:
            logger.error(f"Brain Init Failed: {e}", exc_info=True)
            # print(f"!!! Brain Init Error: {e}") <--- REMOVED

    def start_new_session(self):
        """
        Creates a fresh history list for a new caller.
        """
        return []

    async def generate_stream(self, text, history, caller_number=None):
        """
        Yields responses sentence-by-sentence for low-latency audio streaming.
        Accepts a history list (managed externally).
        """
        if history is None:
            yield "I am currently experiencing high traffic. Please try calling back later."
            return

        try:
            # 1. RETRIEVE KNOWLEDGE
            try:
                # KB returns (content, top_score)
                context_text, rag_score = await asyncio.wait_for(
                    asyncio.to_thread(self.kb.search, text, self.call_logger),
                    timeout=RAG_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.error(f"RAG Search timed out after {RAG_TIMEOUT}s")
                context_text = "No specific documents found due to timeout."
                rag_score = 0.0
            
            # Grounding: Only true if text exists AND score is decent (> 0.58)
            # Pinecone cosine similarity: 1.0 = exact, 0.7 = related, <0.6 = noise
            # Syncing with KnowledgeBase DEFAULT_THRESHOLD (0.58)
            has_grounding = bool(context_text and context_text != "No specific documents found." and rag_score > 0.58)
            
            # Logging accuracy fix: 'kb_hit' means GOOD hit, not just ANY hit
            
            if not context_text:
                context_text = "No specific documents found."
                has_grounding = False
                rag_score = 0.0
            
            logger.info(f"RAG Context for '{text}' (Score: {rag_score:.2f}): {context_text[:200]}...")

            # 1.5 DYNAMIC DATA CHECK (CRM)
            crm_context = ""
            crm_hit = False
            
            # A. Ticket ID Detected (Explicit)
            ticket_match = re.search(r"\bMOCK-\d{5}\b", text, re.IGNORECASE)
            
            # B. Intent Detection for Auto-ID (Implicit)
            # Keywords suggesting user wants status but didn't provide ID
            is_status_query = any(word in text.lower() for word in ["status", "application", "ticket", "update"])
            
            if ticket_match and self.crm_client:
                # CASE 1: Explicit ID
                ticket_id = ticket_match.group(0).upper()
                logger.info(f"Detected Ticket ID: {ticket_id}. Querying CRM...")
                try:
                    ticket_data = await self.crm_client.get_ticket_status(ticket_id)
                    if ticket_data:
                        crm_context = f"Ticket ({ticket_id}) Status: {ticket_data}"
                        crm_hit = True
                    else:
                        crm_context = f"Ticket ({ticket_id}) not found."
                except Exception as e:
                    logger.error(f"CRM Lookup Failed: {e}")
            
            elif is_status_query and caller_number and self.crm_client:
                # CASE 2: Auto-ID via Phone Number
                logger.info(f"Status Intent Detected. Auto-Identifying caller: {caller_number}")
                try:
                    ticket_data = await self.crm_client.get_ticket_by_phone(caller_number)
                    if ticket_data:
                        crm_context = f"Make sure to mention this found record!. Auto-Retrieved Ticket for {caller_number}: {ticket_data}"
                        crm_hit = True
                    else:
                        # Only mention if specifically asked, to avoid clutter
                        crm_context = f"No active tickets found associated with phone number {caller_number}."
                except Exception as e:
                     logger.error(f"CRM Auto-Lookup Failed: {e}")

            # LOGGING: Enforcement Log
            if self.call_logger:
                self.call_logger.log_event("brain", "source_enforcement", 
                                          meta={
                                              "kb_hit": has_grounding, 
                                              "crm_hit": crm_hit,
                                              "kb_score": rag_score
                                          })

            # Metadata for Policy Engine
            sent_metadata = {
                "rag_score": rag_score,
                "has_grounding": has_grounding
            }

            # 2. AUGMENT PROMPT with HIERARCHY
            rag_prompt = f"""
            [KB CONTEXT (General Info)]
            {context_text}
            
            [CRM DATA (Dynamic Info)]
            {crm_context}
            
            [USER QUESTION]
            {text}
            
            Answer the user based on the hierarchy: CRM for status, KB for general info.
            """
            
            # 3. APPEND USER MSG TO HISTORY (Internal)
            history.append({"role": "user", "parts": [rag_prompt]})

            # 4. STREAM GENERATE (with graceful quota handling)
            try:
                response_stream = await asyncio.wait_for(
                    self.model.generate_content_async(
                        contents=history,
                        stream=True
                    ),
                    timeout=LLM_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.error(f"Gemini API timed out after {LLM_TIMEOUT}s")
                yield "I am taking too long to think. Please ask again."
                return
            except ResourceExhausted as quota_error:
                # GRACEFUL HANDLING: Log single line instead of full traceback
                logger.warning("Gemini Quota Exceeded (429). Triggering fallback.")
                
                # Structured log event for error fallback
                if self.call_logger:
                    self.call_logger.log_event("brain", "error_fallback", 
                                               meta={"reason": "quota_exceeded_429"})
                
                # Structural change: yield tuple (text, metadata)
                yield ("I am currently at capacity, please try again later.", {"error": True})
                return
            
            full_ai_text = ""
            sentence_buffer = ""
            async for chunk in response_stream:
                try:
                    # Check if chunk has a valid candidate and part
                    if not chunk.candidates:
                        continue
                        
                    candidate = chunk.candidates[0]
                    # If blocked by safety or other reasons, log and skip
                    if candidate.finish_reason not in [0, 1]: # 0=UNSPECIFIED, 1=STOP
                        logger.warning(f"Chunk blocked by Gemini: {candidate.finish_reason}")
                        continue
                    
                    if not candidate.content.parts:
                        continue
                    
                    # Look for the first part that has text
                    for part in candidate.content.parts:
                        if hasattr(part, 'text') and part.text:
                            text_chunk = part.text.replace("*", "")
                            sentence_buffer += text_chunk
                            
                            # Split into sentences
                            if any(punct in sentence_buffer for punct in [". ", "? ", "! ", "\n"]):
                                # Extract complete sentences
                                parts = sentence_buffer.replace("\n", ". ").split(". ")
                                for i in range(len(parts) - 1):
                                    sentence = parts[i].strip()
                                    if sentence:
                                        full_ai_text += sentence + ". "
                                        yield (sentence + ".", sent_metadata)
                                sentence_buffer = parts[-1]
                except Exception as e:
                    logger.debug(f"Skipping malformed chunk: {e}")
                    continue

            # Yield remaining buffer
            final_sentence = sentence_buffer.strip()
            if final_sentence:
                full_ai_text += final_sentence
                yield (final_sentence, sent_metadata)
            
            # 5. APPEND AI RESPONSE TO HISTORY (After success)
            if full_ai_text.strip():
                history.append({"role": "model", "parts": [full_ai_text.strip()]})

        except ResourceExhausted as quota_error:
            # GRACEFUL HANDLING: Catch quota errors at stream iteration level too
            # GRACEFUL HANDLING: Catch quota errors at stream iteration level too
            logger.warning("Gemini Quota Exceeded (429) during streaming. Triggering fallback.")
            yield ("I am currently at capacity, please try again later.", {"error": True})
        except ResourceExhausted:
            logger.warning("Gemini Quota Exceeded during streaming.")
            yield "My AI brain has reached its free-tier limit. I will be back in a minute!"
        except Exception as e:
            # OTHER ERRORS: Still log full traceback for debugging
            logger.error(f"AI Stream Error: {e}", exc_info=True)

            # Error Recovery: Rollback the 'user' message so we don't break the [User, Model] alternation
            if history and history[-1].get("role") == "user":
                history.pop()

            if "429" in str(e) or "quota" in str(e).lower():
                yield ("I am currently overloaded with requests. Please try again in a few seconds.", {"error": True})
            elif "404" in str(e):
                yield "I am currently undergoing a structural update. Check back in a few minutes!"
            else:
                yield "I am having a moment of silence (Internal Error). Please try again later."

    async def generate_response(self, text, history=None):
        """
        Standard non-streaming response for tests and simple fallbacks.
        """
        if history is None:
            history = self.start_new_session()
            
            
        full_text = ""
        async for chunk, meta in self.generate_stream(text, history):
            full_text += chunk + " "
        return full_text.strip()

    def validate_response(self, text: str) -> bool:
        """
        Structural guardrail: Ensures output is English-oriented.
        Matches the logic in PolicyEngine for consistency.
        """
        if not text: return True
        try:
            # Ratio of ASCII characters to total length
            ascii_chars = sum(1 for c in text if ord(c) < 128)
            ratio = ascii_chars / len(text)
            return ratio >= 0.8 # Allow 20% for accents/emojis
        except:
            return True

    @classmethod
    def is_kb_refusal(cls, text: str) -> bool:
        """
        Utilities for the Orchestrator to detect if the Brain generated the mandatory refusal.
        Using a soft match is safer (in case of minor punctuation deviations).
        """
        if not text: return False
        
        # Use the single source of truth, removing punctuation/case for safer matching
        # "I do not have that information. A staff member will follow up." -> "i do not have that information"
        
        target = cls.KB_MISS_SCRIPT.lower().split(".")[0] # Check first sentence roughly
        return target in text.lower()

if __name__ == "__main__":
    # Simple standalone test
    import asyncio
    
    # Mock CRM for standalone test
    class MockCRM:
        async def get_ticket_status(self, ticket_id):
            if ticket_id == "MOCK-12345":
                return {"status": "In Progress (Mock)"}
            return None

    async def test():
        # Inject Mock CRM
        b = Brain(crm_client=MockCRM())
        
        print("\nTesting greeting (Brain only)...")
        res1 = await b.generate_response("Hello!")
        print(f"AI: {res1}")
        
        print("\nTesting retrieval (KB)...")
        res2 = await b.generate_response("What are the college programs?")
        print(f"AI: {res2}")
        
        print("\nTesting CRM Enforcement...")
        res3 = await b.generate_response("Check status for ticket MOCK-12345")
        print(f"AI: {res3}")
    
    asyncio.run(test())
