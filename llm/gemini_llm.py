"""
LLM Module: Google Gemini (gemini-2.0-flash by default).

Drop-in replacement for llm/groq_llm.py. Same run_llm() signature so the
orchestrator can swap providers via the LLM_PROVIDER config flag with no
code changes upstream.

Contract:
  - Reads (text, detected_lang) tuples from `transcript_queue`
  - Streams response chunks into `text_queue` token-by-token
  - Flushes turns with "\n" so TTS knows when to drain its sentence buffer
  - Supports the same governance interceptor + RAG kb hooks as groq_llm
  - Maintains per-call conversation history in Gemini's native format
  - Stops cleanly when `transcript_queue` yields None

History format note:
  Gemini uses `{"role": "user"|"model", "parts": ["text"]}` (not
  `{"role": "assistant", "content": "text"}` like OpenAI/Groq).
  We store history in Gemini's native format end-to-end; no conversion needed.

System prompt:
  Gemini's GenerativeModel takes a `system_instruction` parameter — we pass
  config.SYSTEM_PROMPT once at model creation, not as a history entry.

Safety filters:
  Relaxed to BLOCK_NONE so the model doesn't refuse to answer benign GD
  College questions that happen to mention strong language. Governance is
  enforced upstream by the LanguageGovernanceInterceptor + restricted-topic
  detector; the LLM-level safety filter is redundant and only causes false
  positives.
"""

import asyncio

import config

_FALLBACK_MESSAGE = "Sorry, I had a brief issue. Could you say that again?"

# google.generativeai imported lazily inside run_llm so the module compiles
# in environments where the SDK isn't installed yet (e.g. when LLM_PROVIDER=groq).
# Install when ready:  uv pip install google-generativeai


def _build_model():
    """Configure the SDK + return a GenerativeModel with system prompt baked in."""
    import google.generativeai as genai

    if not config.GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set in .env — required when LLM_PROVIDER=gemini"
        )

    genai.configure(api_key=config.GEMINI_API_KEY)

    # Relax safety filters: governance happens upstream in the interceptor,
    # not here. The default Gemini safety filter trips on benign cosmetology
    # / beauty / chemical-product questions and returns empty responses,
    # which would leave the caller hanging.
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT",        "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH",       "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    return genai.GenerativeModel(
        model_name=config.GEMINI_MODEL,
        system_instruction=config.SYSTEM_PROMPT,
        safety_settings=safety_settings,
        generation_config={
            "temperature": config.GROQ_TEMPERATURE,  # reuse same temp setting
            "max_output_tokens": 256,                # voice responses are short
        },
    )


async def run_llm(
    transcript_queue: asyncio.Queue,
    text_queue: asyncio.Queue,
    logger=None,
    interceptor=None,
    kb=None,
    barge_in_event: asyncio.Event | None = None,
) -> None:
    """
    Gemini equivalent of groq_llm.run_llm. Same governance + RAG hooks.

    Conversation history is maintained in Gemini's native format:
      [{"role": "user"|"model", "parts": ["text"]}]

    Gemini 2.0-flash requires the history to start with role="user". The
    greeting (spoken before this coroutine starts) is a "model" message
    conceptually, but we don't add it to history — Gemini sees the FIRST
    user utterance as the start of the conversation, which matches what
    the caller actually experiences.
    """
    # Lazy governance imports — match groq_llm pattern.
    detect_restricted_topic = None
    if interceptor is not None:
        from contracts.policy import detect_restricted_topic as _detect
        detect_restricted_topic = _detect

    try:
        model = _build_model()
        print(f"[LLM/Gemini] Model ready: {config.GEMINI_MODEL}")
    except Exception as e:
        print(f"[LLM/Gemini] Model init failed: {type(e).__name__}: {e}")
        # Push a fallback into TTS and exit — the call still completes cleanly.
        await text_queue.put(_FALLBACK_MESSAGE)
        await text_queue.put("\n")
        await text_queue.put(None)
        return

    history: list[dict] = []

    try:
        while True:
            item = await transcript_queue.get()

            if item is None:
                print("[LLM/Gemini] Shutting down")
                return

            # 3-tuple: (text, detected_lang, turn_hint). turn_hint=="final_turn"
            # signals a hangup-phrase farewell — see groq_llm for details.
            if isinstance(item, tuple):
                if len(item) >= 3:
                    user_text, detected_lang, turn_hint = item[0], item[1], item[2]
                else:
                    user_text, detected_lang = item
                    turn_hint = None
            else:
                user_text, detected_lang, turn_hint = item, None, None

            print(f"[LLM/Gemini] User said: '{user_text}'")

            # ── Governance gates (identical to groq_llm) ─────────────────
            if interceptor is not None:
                lang_result = interceptor.check(user_text, detected_lang)
                if not lang_result.proceed_to_llm:
                    print(
                        f"[GOV-LANG] strike {lang_result.strike}/{interceptor.max_strikes}"
                        f"  lang={lang_result.lang_code}"
                        f"  conf={lang_result.confidence:.2f}"
                        f"  terminate={lang_result.terminate_call}"
                    )
                    await text_queue.put(lang_result.refusal_text)
                    await text_queue.put("\n")
                    if logger is not None:
                        logger.log_bot(lang_result.refusal_text)
                        if hasattr(logger, "log_governance_lang_strike"):
                            logger.log_governance_lang_strike(
                                strike=lang_result.strike,
                                lang_code=lang_result.lang_code,
                                confidence=lang_result.confidence,
                                terminated=lang_result.terminate_call,
                            )
                    if lang_result.terminate_call:
                        return
                    continue

                topic_match = detect_restricted_topic(user_text)
                if topic_match is not None:
                    category, response_text = topic_match
                    print(f"[GOV-TOPIC] {category} -> canned refusal")
                    await text_queue.put(response_text)
                    await text_queue.put("\n")
                    history.append({"role": "user",  "parts": [user_text]})
                    history.append({"role": "model", "parts": [response_text]})
                    if logger is not None:
                        logger.log_bot(response_text)
                        if hasattr(logger, "log_governance_topic_refusal"):
                            logger.log_governance_topic_refusal(category)
                    continue

            # ── RAG context retrieval (identical to groq_llm) ────────────
            user_message = user_text
            if kb is not None:
                try:
                    context_str, score, category, *_ = await kb.search(
                        user_text, call_logger=logger
                    )
                    if context_str and context_str not in (
                        "LOW_CONFIDENCE_FALLBACK",
                        "No specific documents found due to a knowledge base error.",
                    ):
                        print(f"[RAG] Injecting context (score={score:.2f}, cat={category})")
                        user_message = (
                            f"[RELEVANT KNOWLEDGE]\n{context_str}\n\n"
                            f"[CALLER QUESTION]\n{user_text}"
                        )
                except Exception as e:
                    print(f"[RAG] Search error (non-fatal): {type(e).__name__}: {e}")

            # For the hangup turn, prepend a brevity instruction directly to
            # the user message — Gemini's system_instruction is set at model
            # init and cannot be changed per-call, so we inject it inline.
            if turn_hint == "final_turn":
                user_message = (
                    "[FINAL TURN — caller is ending the call. Reply with ONE brief warm "
                    "farewell sentence, max 12 words. Do not ask questions or offer more help.]\n\n"
                    + user_message
                )

            history.append({"role": "user", "parts": [user_message]})

            await _stream_turn(model, history, text_queue, logger, barge_in_event=barge_in_event)

    finally:
        await text_queue.put(None)


async def _stream_turn(model, history, text_queue, logger=None, barge_in_event: asyncio.Event | None = None) -> None:
    """
    Stream one Gemini response chunk-by-chunk into text_queue.

    On any error, push a fallback message + history-consistent assistant
    turn so the conversation continues. Always emits a final "\n" flush
    so TTS drains its buffer.

    If ``barge_in_event`` is set mid-stream, abandon the rest of the response
    without flushing — the user has interrupted and TTS has already cleared
    its buffer. The partial response is NOT added to history.
    """
    response_text = ""

    try:
        stream = await model.generate_content_async(history, stream=True)

        first_chunk = True
        async for chunk in stream:
            # ── Mid-stream barge-in (mirrors groq_llm) ───────────────────────
            if barge_in_event is not None and barge_in_event.is_set():
                print("[LLM/Gemini] Barge-in mid-generation -> abandoning stream")
                return

            # Gemini chunks come with candidates -> content.parts[i].text.
            # Defensive: a safety filter or empty candidate can return no text.
            try:
                text_piece = chunk.text or ""
            except Exception:
                text_piece = ""

            if not text_piece:
                continue

            if first_chunk and logger is not None:
                logger.mark_llm_first_token()
                first_chunk = False

            # Strip markdown asterisks so TTS doesn't read them aloud.
            text_piece = text_piece.replace("*", "")

            response_text += text_piece
            await text_queue.put(text_piece)

    except Exception as e:
        # Lazy import keeps top-of-module import safe when SDK is absent.
        try:
            from google.api_core.exceptions import ResourceExhausted
            is_quota = isinstance(e, ResourceExhausted)
        except Exception:
            is_quota = False

        tag = "QUOTA" if is_quota else type(e).__name__
        print(f"[LLM/Gemini] Stream error ({tag}): {e}")

        if response_text.strip():
            history.append({"role": "model", "parts": [response_text]})
            if logger is not None:
                logger.log_bot(response_text)
        else:
            await text_queue.put(_FALLBACK_MESSAGE)
            history.append({"role": "model", "parts": [_FALLBACK_MESSAGE]})
            if logger is not None:
                logger.log_bot(_FALLBACK_MESSAGE)
        await text_queue.put("\n")
        return

    if response_text:
        history.append({"role": "model", "parts": [response_text]})
        if logger is not None:
            logger.log_bot(response_text)
    await text_queue.put("\n")
