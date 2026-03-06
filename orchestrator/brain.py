import os
import re
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted  # Add this import
import logging
import asyncio
from typing import List, Dict, Any, Optional
from retrieval.vector_store import KnowledgeBase  # New RAG module
from dotenv import load_dotenv

# Configure logging
logger = logging.getLogger("Brain")

load_dotenv()

from contracts.interfaces import LLMEngine
from contracts.config import FeatureConfig
from contracts.policy import PRDScripts

# Pillar 2: Anti-Freeze Timeouts
LLM_TIMEOUT = 12.0
RAG_TIMEOUT = 5.0

class Brain(LLMEngine):
    # 1. DEFINE SOURCE OF TRUTH FOR REFUSAL SCRIPT
    KB_MISS_SCRIPT = PRDScripts.REFUSAL_KB_MISS

    def __init__(self, call_logger=None, crm_client=None):
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.call_logger = call_logger
        self.crm_client = crm_client
        self.config = FeatureConfig()
        
        # 1. Initialize Knowledge Base
        self.kb = KnowledgeBase()

        # --- ATTEMPT CONNECTION ---
        try:
            # 2. Define Instructions (Injecting the Constant)
            self.system_instruction = f"""
            You are the GD College Intelligence Bridge (CILA). Your role is to be the friendly, professional, and deterministic link between prospective students and the college's Knowledge Base.

            MANDATORY RESPONSE PATTERN (Applied to EVERY response):
            1. ACKNOWLEDGE: You MUST start EVERY single response by acknowledging the user's topic. For example: "I understand you are asking about [TOPIC]," or "I see you're interested in [TOPIC]."
            2. RETRIEVE: Use the provided [CONTEXT] to provide deterministic, accurate data.
            3. GUARD:
               - If the Knowledge Base context is missing or irrelevant, acknowledge the topic first, then state: "{self.KB_MISS_SCRIPT}"
               - If the input is empty or noise, stay in "Listening Mode" and do not respond with a refusal.
               - LANGUAGE GUARD: You are English-only. If a clear non-English intent is detected, immediately trigger the Non-English Refusal: "{PRDScripts.REFUSAL_LANGUAGE}"

            CONVERSATIONAL RULES:
            1. TONE: Friendly but professional. No rude phrasing, overly casual slang, or persuasive/sales-like language.
            2. CONCISE: Keep answers to 1 or 2 short sentences.
            3. STRUCTURED: If the user asks for a list or steps, use a numbered list (1., 2., 3.).
            4. RAPPORT: If you don't know the user's name, ask politely: "May I know who I am speaking with?" If you do, use it naturally.
            5. LIMITS: No immigration, medical, or legal advice.
            6. BARGE-IN: If interrupted, classify as NEW_TOPIC or SAME_TOPIC and respond naturally without asking procedural questions.
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

    def _fix_history_roles(self, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Gemini 2.5 Flash requires history to strictly start with 'user'.
        If it starts with 'model' (greeting), prepend a dummy user message.
        """
        if not history:
            return history
            
        if history[0].get("role") != "user":
            logger.debug("[FIX] Prepended dummy user message to conversation history.")
            return [{"role": "user", "parts": ["[Call started]"]}] + history
        return history

    async def generate_with_classification(self, session, caller_input: str, context_text: str = None, trace_id: str = None):
        """
        Special method for barge-in handling (CRITICAL-P2-05). 
        Returns (classification, response, is_multi_step, topic).
        Uses optional RAG context to ground responses.
        """
        import json
        
        # 🟢 PINNED TIMEOUT FOR BARGE-IN (Sub-second target, 1.5s absolute ceiling)
        BARGE_IN_TIMEOUT = 1.5
        
        context_block = f"\n[KNOWLEDGE BASE CONTEXT]\n{context_text}\nUse this context to accurately answer if relevant." if context_text else ""

        # Build a prompt for classification + response
        prompt = f"""
        USER INPUT (Barge-in): {caller_input}
        {context_block}

        RULES:
        - For NEW_TOPIC: Respond directly to the new topic with zero reference to your previous interrupted response.
        - For SAME_TOPIC: Naturally incorporate the clarification into your response as a continuation.
        - For AMBIGUOUS: Respond to the most likely interpretation without asking for clarification.
        - ALWAYS prioritize the [KNOWLEDGE BASE CONTEXT] for your answers if it is provided.

        You must respond in VALID JSON format ONLY:
        {{
          "classification": "NEW_TOPIC" | "SAME_TOPIC" | "AMBIGUOUS",
          "topic": "Brief topic name",
          "response": "Your natural grounded response here",
          "is_multi_step": true | false
        }}
        """
        
        # PRD §5: Zero-Retry Enforcement (max_retries = 1 attempt total)
        try:
            # We reuse the history but append the special prompt
            history = list(session.conversation_history)
            history = self._fix_history_roles(history)
            history.append({"role": "user", "parts": [prompt]})
            
            # 🟢 HARD TIMEOUT: Fast fail to prevent "clobbering"
            response = await asyncio.wait_for(
                self.model.generate_content_async(contents=history), 
                timeout=BARGE_IN_TIMEOUT
            )
            text = response.text.strip()
            
            # Cleanup potential markdown code blocks
            if text.startswith("```json"):
                text = text.replace("```json", "", 1).replace("```", "", 1).strip()
            elif text.startswith("```"):
                 text = text.replace("```", "", 1).replace("```", "", 1).strip()

            data = json.loads(text)
            
            return (
                data.get("classification", "AMBIGUOUS"),
                data.get("response", "I'm listening, please go ahead."),
                data.get("is_multi_step", False),
                data.get("topic", "General")
            )
                
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning(f"Barge-in classification failed or timed out ({BARGE_IN_TIMEOUT}s): {e}")
            # 🟢 GRACEFUL FALLBACK (Safe Default)
            return "AMBIGUOUS", "I'm listening, please go ahead.", False, "Barge-in"

    async def generate_stream(self, text, history, caller_number=None, intent="unknown", trace_id=None, call_context=None, prefetched_context_task=None):
        """
        Yields responses sentence-by-sentence for low-latency audio streaming.
        Accepts a history list (managed externally).
        """
        if history is None:
            yield PRDScripts.APOLOGY_OVERLOADED
            return

        try:
            # 1. RETRIEVE KNOWLEDGE
            if self.config.override_retrieval:
                logger.warning(f"[OVERRIDE] RAG Retrieval Disabled (env={self.config.env})")
                context_text = "RAG Disabled by manual override."
                rag_score = 0.0
            else:
                try:
                    # TELEMETRY: Start RAG Timer
                    rag_start_time = asyncio.get_event_loop().time()

                    if prefetched_context_task:
                        logger.info(f"Using pre-fetched RAG context for '{text}'")
                        context_text, rag_score, rag_topic = await prefetched_context_task
                    else:
                        # Context-Aware Follow-Ups (Story S4-9)
                        # Augment short follow-up questions with known context for better vector matching
                        search_query = text
                        if call_context and len(text.split()) < 8:
                            tags = []
                            if call_context.program_interest: tags.append(call_context.program_interest)
                            if call_context.intake: tags.append(call_context.intake)
                            if tags:
                                search_query = f"{text} (Context: {' '.join(tags)})"
                                logger.info(f"Augmented RAG Query: '{search_query}'")

                        # KB returns (content, top_score, category)
                        context_text, rag_score, rag_topic = await asyncio.wait_for(
                            asyncio.to_thread(self.kb.search, search_query, self.call_logger, 3, trace_id),
                            timeout=RAG_TIMEOUT
                        )
                    
                    # TELEMETRY: Finish RAG Timer
                    rag_latency = asyncio.get_event_loop().time() - rag_start_time
                    if self.call_logger:
                        self.call_logger.log_event("retrieval", "rag_search_latency", latency_ms=int(rag_latency*1000), trace_id=trace_id)
                except asyncio.TimeoutError:
                    logger.error(f"RAG Search timed out after {RAG_TIMEOUT}s")
                    context_text = "No specific documents found due to timeout."
                    rag_score = 0.0
                    rag_topic = "General"
                    rag_latency = RAG_TIMEOUT

                    # CRM Artifact: System Alert for Knowledge Base Timeout
                    if self.crm_client and call_context:
                        try:
                            await self.crm_client.create_ticket(
                                transcript=f"[SYSTEM_EVENT] RAG timeout after {RAG_TIMEOUT}s for query: {text}",
                                summary="System Alert: Knowledge Base Timeout",
                                sentiment="Negative",
                                call_logger=self.call_logger,
                                call_id=call_context.session_id,
                                title="System Alert: Knowledge Base Timeout"
                            )
                        except Exception as e:
                            logger.error(f"Failed to create KB Timeout CRM ticket: {e}")
                except Exception as e:
                    logger.error(f"RAG Search failed with error: {e}")
                    context_text = "No specific documents found due to an internal knowledge base error."
                    rag_score = 0.0
                    rag_topic = "General"
                    rag_latency = RAG_TIMEOUT

                    # CRM Artifact: System Alert for Knowledge Base Failure
                    if self.crm_client and call_context:
                        try:
                            await self.crm_client.create_ticket(
                                transcript=f"[SYSTEM_EVENT] Knowledge Base failure for query: {text}\nError: {e}",
                                summary="System Alert: Knowledge Base Failure",
                                sentiment="Negative",
                                call_logger=self.call_logger,
                                call_id=call_context.session_id,
                                title="System Alert: Knowledge Base Failure"
                            )
                        except Exception as ce:
                            logger.error(f"Failed to create KB Failure CRM ticket: {ce}")
            
            # Grounding is determined by whether KnowledgeBase returned valid chunks.
            # KnowledgeBase now handles its own config-driven confidence gates.
            invalid_contexts = [
                "No specific documents found.",
                "No specific documents found due to timeout.",
                "No specific documents found due to an internal knowledge base error.",
                "LOW_CONFIDENCE_FALLBACK",
                "BLOCKED_BY_SAFETY_GUARDRAIL",
                "RAG Disabled by manual override."
            ]
            has_grounding = bool(context_text and context_text not in invalid_contexts)
            
            # Logging accuracy fix: 'kb_hit' means GOOD hit, not just ANY hit
            
            if not context_text:
                context_text = "No specific documents found."
                has_grounding = False
                rag_score = 0.0
                rag_topic = "General"
            
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

            # --- DECISION LOG & EXPLAINABILITY (Story S3-3) ---
            # Determine Governance Decision
            governance_decision = "Allowed"
            if not has_grounding and not crm_hit:
                governance_decision = "Refusal: Low Confidence / KB Miss"
            
            # Prepare Readable Chunks (Split for JSON list if needed, or keep text)
            chunks_list = context_text.split("\n\n") if context_text else []
            
            if call_context:
                call_context.retrieved_chunks_snapshot = chunks_list
            
            decision_meta = {
                "intent": intent,
                "confidence_score": round(rag_score, 2),
                "chunks_used": chunks_list,
                "crm_hit": crm_hit,
                "governance_decision": governance_decision,
                "refusal_flags": {
                    "kb_miss": not has_grounding,
                    "crm_miss": not crm_hit and is_status_query
                }
            }
            
            # 1. Structural Log (JSON)
            if self.call_logger:
                self.call_logger.log_event("brain", "decision_trace", meta=decision_meta, trace_id=trace_id)
            
            # 2. Human-Readable Log (Console/File)
            log_str = (f"DECISION LOG: [{governance_decision}] | "
                       f"Intent: {intent} | "
                       f"Score: {rag_score:.2f} | "
                       f"Chunks: {len(chunks_list)} | "
                       f"CRM: {crm_hit}")
            logger.info(log_str)
            # --------------------------------------------------

            # ── RAG SCORE FLOOR ───────────────────────────────────────────────────
            # S4 Refinement Fix: Hard gate to prevent hallucination.
            # DO NOT pass to LLM if grounding and CRM are both missed.
            if not has_grounding and not crm_hit:
                # 1. Create the CRM callback ticket
                if self.crm_client and call_context:
                    try:
                        await self.crm_client.create_ticket(
                            transcript=f"User Query: {text}\nStatus: Knowledge Base Miss (Score: {rag_score})",
                            summary="Callback Required: KB Miss",
                            sentiment="Neutral",
                            call_logger=self.call_logger,
                            call_id=call_context.session_id,
                            title="Callback Required: Unanswered User Query"
                        )
                    except Exception as e:
                        logger.error(f"Failed to create callback ticket for KB miss: {e}")

                # 2. Deterministically yield the refusal and bypass the LLM entirely
                yield (self.KB_MISS_SCRIPT, {"error": "kb_miss", "has_grounding": has_grounding, "rag_score": rag_score})
                return
            # ──────────────────────────────────────────────────────────────────────

            # Metadata for Policy Engine
            sent_metadata = {
                "rag_score": rag_score,
                "has_grounding": has_grounding,
                "topic": rag_topic
            }

            # S4-9: Context Injection
            context_block = ""
            if call_context:
                last_intent = call_context.last_intents[-1] if call_context.last_intents else "unknown"
                chunks_info = "Yes" if call_context.retrieved_chunks_snapshot else "None"
                context_block = f"""
                [CURRENT CALL CONTEXT]
                User Name: {call_context.user_name or "Unknown"}
                Program Interest: {call_context.program_interest or "Not specified"}
                Intake: {call_context.intake or "Not specified"}
                Mode: {call_context.study_mode or "Not specified"}
                Campus: {call_context.campus or "Not specified"}
                Last Intent: {last_intent}
                Last Answer: {call_context.last_agent_answer_summary or "None"}
                Last Retrieved Chunks: {chunks_info}
                """

            rag_prompt = f"""
            [KB CONTEXT (General Info)]
            {context_text}
            
            [CRM DATA (Dynamic Info)]
            {crm_context}
            
            {context_block}
            
            [USER QUESTION]
            {text}
            
            Answer the user based on the hierarchy: CRM for status, KB for general info.
            Use [CURRENT CALL CONTEXT] to answer naturally (e.g. "As you are interested in Nursing...").
            """
            
            # 3. APPEND USER MSG TO HISTORY (Internal)
            history = self._fix_history_roles(history)
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
                yield (PRDScripts.APOLOGY_CAPACITY, {"error": "timeout"})
                return
            except ResourceExhausted as quota_error:
                # GRACEFUL HANDLING: Log single line instead of full traceback
                logger.warning("Gemini Quota Exceeded (429). Triggering fallback.")
                
                # Structured log event for error fallback
                if self.call_logger:
                    self.call_logger.log_event("brain", "error_fallback", 
                                               meta={"reason": "quota_exceeded_429"},
                                               trace_id=trace_id)
                
                # Structural change: yield tuple (text, metadata)
                yield (PRDScripts.APOLOGY_CAPACITY, {"error": True})
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
                if call_context:
                    call_context.last_agent_answer_summary = full_ai_text.strip()

        except ResourceExhausted as quota_error:
            # GRACEFUL HANDLING: Catch quota errors at stream iteration level too
            # GRACEFUL HANDLING: Catch quota errors at stream iteration level too
            logger.warning("Gemini Quota Exceeded (429) during streaming. Triggering fallback.")
            yield (PRDScripts.APOLOGY_CAPACITY, {"error": True})
        except ResourceExhausted:
            logger.warning("Gemini Quota Exceeded during streaming.")
            yield PRDScripts.APOLOGY_CAPACITY
        except Exception as e:
            # OTHER ERRORS: Still log full traceback for debugging
            logger.error(f"AI Stream Error: {e}", exc_info=True)

            # Error Recovery: Rollback the 'user' message so we don't break the [User, Model] alternation
            if history and history[-1].get("role") == "user":
                history.pop()

            if "429" in str(e) or "quota" in str(e).lower():
                yield (PRDScripts.APOLOGY_OVERLOADED, {"error": True})
            elif "404" in str(e):
                yield PRDScripts.APOLOGY_STRUCTURAL_UPDATE
            else:
                yield PRDScripts.APOLOGY_INTERNAL_ERROR

    async def generate_response(self, text, history=None, trace_id=None):
        """
        Standard non-streaming response for tests and simple fallbacks.
        """
        if history is None:
            history = self.start_new_session()
            
            
        full_text = ""
        async for chunk, meta in self.generate_stream(text, history, trace_id=trace_id):
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
        
        # S4-9 Test
        print("\nTesting Context Injection...")
        from contracts.schemas import CallContext
        ctx = CallContext(session_id="test", caller_number="999", start_time=0.0)
        ctx.program_interest = "Nursing"
        ctx.user_name = "John"
        
        # We need to call generate_stream directly or update generate_response to pass context
        # For this test, let's just use generate_stream manually
        print(f"Context: {ctx.program_interest}, {ctx.user_name}")
        full_text = ""
        async for chunk, meta in b.generate_stream("How long is the course?", [], call_context=ctx):
            full_text += chunk + " "
        print(f"AI (Context-Aware): {full_text.strip()}")
    
    asyncio.run(test())
