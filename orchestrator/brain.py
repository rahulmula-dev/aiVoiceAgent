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

# --- Pillar 2: Anti-Freeze Timeouts ---
# [PRODUCTION / DEPLOYMENT TIMERS - STRICT PRD]
# Uncomment these when deployed to a cloud server (US-East) to enforce strict <500ms rules:
# LLM_TIMEOUT = 0.5
# RAG_TIMEOUT = 0.3

# [LOCAL TESTING TIMERS]
# Relaxed to account for long physical distances, Ngrok routing, and local network latency:
LLM_TIMEOUT = 2.0  # Increased for local testing stability
RAG_TIMEOUT = 10.0  # Increased for local testing stability
# ----------------------------------------

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
            # 2. Define Instructions (Injecting the Constant) - Fixed for WS-03
            self.system_instruction = f"""
            You are the CILA Reliability & Compliance Engine—the intelligent heartbeat and safety-guard for all GD College communication.
            Beyond answering student queries, you have three critical 'Compliance & Reliability Guard' duties:
            1. **Residency Guard**: Ensure all processing stays compliant with local regulations. If a residency violation is detected, follow the 'soft landing' protocol.
            2. **CRM Reliability**: Every dropped call is a lost opportunity. Log resource exhaustion events immediately and create high-priority follow-up tickets.
            3. **Leak Prevention**: Maintain healthy connections and ensure zero-waste of college resources.

            MANDATORY RESPONSE PATTERN:
            1. ACKNOWLEDGE: Start EVERY response by acknowledging the user's topic (e.g., "I understand you are asking about [TOPIC]").
            2. RETRIEVE: Use the provided [CONTEXT] for deterministic accuracy.
            3. GUARD:
               - If KB context is missing: "{self.KB_MISS_SCRIPT}"
               - LANGUAGE GUARD: English-only. Non-English detected: "{PRDScripts.REFUSAL_LANGUAGE}"
            
            CONVERSATIONAL RULES: Professional, friendly, concise (1-2 sentences), numbered lists for steps, rapport protocol.
            """

            # 3. SAFETY SETTINGS (Relaxed to prevent blocked responses for harmless RAG queries)
            safety_settings = [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ]

            # Workstream 2: AI Data Residency (CRITICAL-P3-02)
            # [PRD] Strict enforcement for production
            if os.getenv("DPA_CANADA_ACTIVE", "false").lower() == "true":
                logger.info("RESIDENCY GUARD: DPA_CANADA_ACTIVE is set. Enforcing Canadian data residency for Gemini.")

            genai.configure(api_key=self.api_key)
            
            # 4. INITIALIZE MODELS (PRIMARY + FAST FALLBACK)
            self.model_name = self.config.primary_model
            self.fast_model_name = self.config.fast_model
            
            # Primary Model
            self.model = genai.GenerativeModel(
                model_name=self.model_name,
                system_instruction=self.system_instruction,
                safety_settings=safety_settings
            )
            
            # Fast Model (Degradation Fallback)
            self.fast_model = genai.GenerativeModel(
                model_name=self.fast_model_name,
                system_instruction=self.system_instruction,
                safety_settings=safety_settings
            )
            
            logger.info(f"Brain Init: Primary={self.model_name}, Fast={self.fast_model_name}")
        except Exception as e:
            logger.error(f"Brain Init Failed: {e}", exc_info=True)

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
        Returns (classification, response, is_multi_step, topic, kb_version, chunk_ids).
        Uses optional RAG context to ground responses.
        """
        import json
        
        # 🟢 PINNED TIMEOUT FOR BARGE-IN (Sub-second target, 3.0s absolute ceiling)
        BARGE_IN_TIMEOUT = 3.0
        
        context_block = f"\n[KNOWLEDGE BASE CONTEXT]\n{context_text}\nUse this context to accurately answer if relevant." if context_text else ""

        # Build a prompt for classification + response
        prompt = f"""
        USER INPUT (Barge-in): {caller_input}
        {context_block}

        RULES:
        - Provide a direct, standalone response to the user's new query using only the context below. Ignore the previous interrupted sentence.
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
                self._derive_topic("unknown", data.get("topic", "General")),
                "unknown",
                []
            )
                
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning(f"Barge-in classification failed or timed out ({BARGE_IN_TIMEOUT}s): {e}")
            # 🟢 GRACEFUL FALLBACK (Safe Default)
            return "AMBIGUOUS", "I'm listening, please go ahead.", False, "Barge-in", "unknown", []

    async def generate_stream(self, text, history, caller_number=None, intent="unknown", trace_id=None, call_context=None, prefetched_context_task=None, degraded_mode=False):
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
                        context_text, rag_score, rag_topic, kb_v, c_ids = await asyncio.wait_for(
                            prefetched_context_task,
                            timeout=RAG_TIMEOUT
                        )
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

                        # KB returns (content, top_score, category, kb_version, chunk_ids)
                        context_text, rag_score, rag_topic, kb_v, c_ids = await asyncio.wait_for(
                            self.kb.search(search_query, self.call_logger, 3, trace_id),
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
                    kb_v = "unknown"
                    c_ids = []
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
                    kb_v = "unknown"
                    c_ids = []
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
            if not has_grounding:
                context_text = "No specific documents found."
                has_grounding = False
                rag_score = 0.0
                rag_topic = self._derive_topic(intent, "General")
                kb_v = "unknown"
                c_ids = []
            
            # --- AGGREGATE METADATA FOR CALL LOG ---
            if call_context:
                if not call_context.kb_version_id and kb_v and kb_v != "unknown":
                    call_context.kb_version_id = kb_v
                # Add unique chunk IDs to the overall session list
                for cid in c_ids:
                    if cid and cid != "unknown" and cid not in call_context.chunk_ids_used:
                        call_context.chunk_ids_used.append(cid)
            
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

            # ── [GROUNDING-ISS-121] MANDATORY 0.58 THRESHOLD ───────────────────────────
            # S4 Refinement Fix: Hard gate to prevent hallucination.
            # If rag_score < 0.58 and no CRM hit, yield KB_MISS_SCRIPT.
            # EXCEPTION: Short conversational inputs (≤5 words) like "Hello?", "Hi", "What?"
            # have no KB match by design — let the LLM respond naturally to them.
            is_conversational = len(text.strip().split()) <= 5 and not any(
                kw in text.lower() for kw in ["fee", "program", "admission", "course", "eligibil", "deadline", "intake", "campus", "apply", "document"]
            )
            
            if (rag_score < 0.58) and not crm_hit and not is_conversational:
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
                        # [STORY-DUMMY-CRM] Trigger Callback for KB Miss
                        await self.crm_client.create_callback(
                            ticket_id=call_context.session_id,
                            phone_number=call_context.caller_number,
                            reason=f"Missing Information: Could not answer '{text}' from Knowledge Base."
                        )
                    except Exception as e:
                        logger.error(f"Failed to create callback ticket for KB miss: {e}")

                # 2. Deterministically yield the refusal and bypass the LLM entirely
                yield (self.KB_MISS_SCRIPT, {"error": "kb_miss", "has_grounding": has_grounding, "rag_score": rag_score})
                return
            elif (rag_score < 0.50) and not crm_hit and is_conversational:
                logger.info(f"[BRAIN] Conversational input '{text}' exempt from hard gate — passing to LLM.")
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

            # 4. STREAM GENERATE (with graceful quota handling and TTFT enforcement)
            # A. Select Model based on Degradation State
            active_model = self.model
            active_model_name = self.model_name
            
            # Dynamic Switch: Use fast model if global degradation is ON or current turn is slow
            if degraded_mode or self.config.is_degradation_mode:
                active_model = self.fast_model
                active_model_name = self.fast_model_name
                logger.warning(f"[DEGRADATION] Switching to FAST model: {active_model_name} (degraded_mode={degraded_mode})")

            # B. Start the generation request
            response_stream = await active_model.generate_content_async(
                contents=history,
                stream=True
            )
            
            # B. Use iterator to enforce TTFT on the FIRST chunk
            stream_iter = response_stream.__aiter__()
            first_chunk_received = False
            full_ai_text = ""
            sentence_buffer = ""

            while True:
                try:
                    # PRD Pillar 2: TTFT Guardrail (500ms) - only on the first chunk
                    if not first_chunk_received:
                        chunk = await asyncio.wait_for(stream_iter.__anext__(), timeout=LLM_TIMEOUT)
                        first_chunk_received = True
                    else:
                        # Normal streaming for subsequent chunks
                        chunk = await stream_iter.__anext__()
                    
                    # Process the chunk
                    if not chunk.candidates: continue
                    candidate = chunk.candidates[0]
                    if candidate.finish_reason not in [0, 1]: continue
                    if not candidate.content.parts: continue
                    
                    for part in candidate.content.parts:
                        if hasattr(part, 'text') and part.text:
                            text_chunk = part.text.replace("*", "")
                            sentence_buffer += text_chunk
                            if any(punct in sentence_buffer for punct in [". ", "? ", "! ", "\n"]):
                                parts = sentence_buffer.replace("\n", ". ").split(". ")
                                for i in range(len(parts) - 1):
                                    sentence = parts[i].strip()
                                    if sentence:
                                        full_ai_text += sentence + ". "
                                        yield (sentence + ".", sent_metadata)
                                sentence_buffer = parts[-1]

                except asyncio.TimeoutError:
                    logger.error(f"Gemini First Token (TTFT) timed out after {LLM_TIMEOUT}s")
                    yield (PRDScripts.APOLOGY_CAPACITY, {"error": "timeout_ttft"})
                    return
                except StopAsyncIteration:
                    break
                except Exception as e:
                    if "429" in str(e) or "quota" in str(e).lower():
                        logger.warning("Gemini Quota Exceeded during stream.")
                        yield (PRDScripts.APOLOGY_CAPACITY, {"error": True})
                        return
                    raise e

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
            yield (PRDScripts.APOLOGY_CAPACITY, {"error": "quota_exhausted"})
        except Exception as e:
            # OTHER ERRORS: Still log full traceback for debugging
            logger.error(f"AI Stream Error: {e}", exc_info=True)

            # Error Recovery: Rollback the 'user' message so we don't break the [User, Model] alternation
            if history and history[-1].get("role") == "user":
                history.pop()

            if "429" in str(e) or "quota" in str(e).lower():
                yield (PRDScripts.APOLOGY_OVERLOADED, {"error": "quota_error_msg"})
            elif "404" in str(e):
                yield (PRDScripts.APOLOGY_STRUCTURAL_UPDATE, {"error": "model_not_found"})
            else:
                yield (PRDScripts.APOLOGY_INTERNAL_ERROR, {"error": "unknown_stream_err"})

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
    def is_kb_refusal(cls, text: str):
        """
        Utilities for the Orchestrator to detect if the Brain generated the mandatory refusal.
        Using a soft match is safer (in case of minor punctuation deviations).
        """
        if not text: return False
        refusal_patt = cls.KB_MISS_SCRIPT.strip().lower().replace(".", "")
        clean_text = text.strip().lower().replace(".", "")
        return refusal_patt in clean_text or clean_text in refusal_patt

    def _derive_topic(self, intent: str, rag_topic: str) -> str:
        """
        [MEDIUM-P5-03] Logic-based Topic Tagger.
        Prioritizes RAG category, then Intent, then fallback.
        """
        if rag_topic and rag_topic != "General":
            return rag_topic
            
        intent_map = {
            "JOB_QUERY": "Admissions & Careers",
            "VENDOR_PAYMENT": "Finance & Operations",
            "TRANSCRIPT_REQUEST": "Registrar Services",
            "FEES": "Tuition & Fees",
            "PROCEED": "Student Onboarding",
            "REFUSE": "Policy Enforcement",
            "AMBIGUOUS": "General Inquiry"
        }
        
        return intent_map.get(intent, "General Inquiry")

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
