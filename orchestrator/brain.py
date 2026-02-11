import os
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
    def __init__(self, call_logger=None):
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.call_logger = call_logger
        
        # 1. Initialize Knowledge Base
        self.kb = KnowledgeBase()

        # --- ATTEMPT CONNECTION ---
        try:
            # 2. Define Instructions
            self.system_instruction = """
            You are CILA (often mis-transcribed as "Tina", "Sheila", or "Peter"), the friendly and professional AI Voice Agent for GD College in Calgary, Alberta.
            
            CORE INSTRUCTIONS:
            1. GREETING RULES:
               - I have ALREADY introduced myself at the start of this call. NEVER repeat "I am CILA" or "I am from GD College" again.
               - If the caller says "Hello?", "Hi", "Hey", or just greets you, respond conversationally: "Yes, how can I help you?" or "What can I do for you today?"
               - If asked "How are you?", respond briefly: "I'm doing well, thank you. How can I assist you?"
               - DO NOT re-introduce yourself unless the caller explicitly asks "Who am I speaking with?" or "What is your name?"
            
            2. COLLEGE KNOWLEDGE: You MUST ONLY answer questions about GD College using the provided [CONTEXT]. 
            
            3. STRICT REFUSAL: If the [CONTEXT] says "No specific documents found" or does NOT contain the specific answer to a college query, you MUST say exactly: "I don't have that information right now, but I can arrange for a team member to follow up."
            
            4. ACCURACY: Never invent facts. If you aren't 100% sure based on the [CONTEXT], use the refusal phrase.
            
            5. CONCISE: Keep all answers to 1 or 2 short sentences. Be direct and efficient.
            
            6. TOPICS: Do not discuss immigration, medical advice, or personal opinions.
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

    async def generate_stream(self, text, history):
        """
        Yields responses sentence-by-sentence for low-latency audio streaming.
        Accepts a history list (managed externally).
        """
        if history is None:
            yield "I am currently experiencing high traffic. Please try calling back later."
            return

        try:
            # 1. RETRIEVE KNOWLEDGE
            # Currently kb.search returns just text. 
            # Ideally it should return metadata too, but for S1-4 we will trust the presence of text.
            context_text = await asyncio.to_thread(self.kb.search, text, self.call_logger)
            
            has_grounding = True
            rag_score = 0.8 # Dummy score if KB doesn't return one yet. In real app, KB should return score.
            
            if not context_text or context_text == "No specific documents found.": 
            try:
                context_text = await asyncio.wait_for(
                    asyncio.to_thread(self.kb.search, text, self.call_logger),
                    timeout=RAG_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.error(f"RAG Search timed out after {RAG_TIMEOUT}s")
                context_text = "No specific documents found due to timeout."
            
            if not context_text: 
                logger.warning("RAG Decision: No relevant documents found. Falling back to LLM knowledge.")
                context_text = "No specific documents found."
                has_grounding = False
                rag_score = 0.0
            
            logger.info(f"RAG Context for '{text}': {context_text[:200]}...")

            # Metadata for Policy Engine
            sent_metadata = {
                "rag_score": rag_score,
                "has_grounding": has_grounding
            }

            # 2. AUGMENT PROMPT
            rag_prompt = f"""[CONTEXT FROM DATABASE]\n{context_text}\n\n[USER QUESTION]\n{text}\n\nAnswer the user based on the context above."""
            
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
            else:
                yield ("I'm having trouble connecting to my knowledge base right now.", {"error": True})
                yield "My brain is currently resting due to high traffic (Quota reached). Please try again soon."
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

if __name__ == "__main__":
    # Simple standalone test
    import asyncio
    async def test():
        b = Brain()
        print("\nTesting greeting...")
        res1 = await b.generate_response("Hello!")
        print(f"AI: {res1}")
        
        print("\nTesting retrieval...")
        res2 = await b.generate_response("What are the college programs?")
        print(f"AI: {res2}")
        
        print("\nTesting refusal policy...")
        res3 = await b.generate_response("What is my current GPA?")
        print(f"AI: {res3}")
    
    asyncio.run(test())
