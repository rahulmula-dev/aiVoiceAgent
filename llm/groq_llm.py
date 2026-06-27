"""
LLM Module: Groq llama-3.1-8b-instant (free tier)

Groq exposes an OpenAI-compatible REST API, so we reuse the openai SDK
but point it at https://api.groq.com/openai/v1.

To swap provider:
  1. Create llm/<new_provider>_llm.py
  2. Implement: async def run_llm(transcript_queue, text_queue)
  3. Update the import in main.py — nothing else changes.

Contract:
  - Reads transcript strings from `transcript_queue`
  - Pushes response text CHUNKS (token-by-token) into `text_queue`
  - Executes pharmacy tool calls internally using FUNCTION_MAP
  - Maintains per-call conversation history
  - Stops cleanly when `transcript_queue` yields None (sentinel)
  - Forwards None sentinel into `text_queue` to shut down TTS
"""

import asyncio
import json

from openai import AsyncOpenAI, APIError

import config

# No tools registered yet — the GD College corpus is inlined directly in
# config.SYSTEM_PROMPT, so the LLM has nothing to call. The tool-call branch
# below is left in place to make adding tools later (e.g. book_campus_tour)
# a one-line change: just register them in this dict and in config.TOOLS.
FUNCTION_MAP: dict = {}

_FALLBACK_MESSAGE = "Sorry, I had a brief issue. Could you say that again?"

_client = AsyncOpenAI(
    api_key=config.GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1",
)


async def run_llm(
    transcript_queue: asyncio.Queue,
    text_queue: asyncio.Queue,
    logger=None,
    interceptor=None,   # Optional LanguageGovernanceInterceptor (Step 4)
    kb=None,            # Optional KnowledgeBase for RAG context injection (Step 7)
    barge_in_event: asyncio.Event | None = None,  # cancel mid-stream on barge-in
) -> None:
    """
    Main LLM coroutine. Persists conversation history for the entire call.
    Wraps the whole loop in try/finally so the TTS sentinel always fires —
    even if a turn explodes in a way _chat_turn's own handler missed.

    If `logger` is provided, each completed assistant response is appended to
    the per-call transcript JSON.

    If `interceptor` is provided (governance), two gates run before each LLM
    call: language strike gate and restricted-topic gate.

    If `kb` (KnowledgeBase) is provided, a pgvector search is performed for
    each user utterance after governance passes. When a relevant chunk is found
    (score >= threshold), it is injected into the user message as
    [RELEVANT KNOWLEDGE] so the LLM can cite it directly. When the search
    returns LOW_CONFIDENCE_FALLBACK (no good match), the user message is sent
    as-is and the LLM falls back to its inline SYSTEM_PROMPT corpus.
    """
    # Governance helpers imported lazily so the LLM module has no hard
    # dependency on contracts.policy when governance is disabled.
    detect_restricted_topic = None
    if interceptor is not None:
        from contracts.policy import detect_restricted_topic as _detect
        detect_restricted_topic = _detect

    conversation_history = [
        {"role": "system", "content": config.SYSTEM_PROMPT}
    ]

    try:
        while True:
            item = await transcript_queue.get()

            if item is None:                # sentinel → end of call
                print("[LLM] Shutting down")
                return

            # transcript_queue carries (text, detected_lang, turn_hint) tuples
            # from STT. turn_hint == "final_turn" signals the caller said a
            # hangup phrase and the next response should be a one-sentence
            # farewell. Tolerate older 2-tuples and bare strings too.
            if isinstance(item, tuple):
                if len(item) >= 3:
                    user_text, detected_lang, turn_hint = item[0], item[1], item[2]
                else:
                    user_text, detected_lang = item
                    turn_hint = None
            else:
                user_text, detected_lang, turn_hint = item, None, None

            print(f"[LLM] User said: '{user_text}'")

            # ── Governance gates (only if interceptor was injected) ──────
            if interceptor is not None:
                # Language gate: non-English → strike refusal.
                # detected_lang is Deepgram's acoustic/word-level signal — the
                # most reliable indicator of the spoken language.
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
                        # Record the strike in the call's events.jsonl + summary
                        # when the logger is the new CallLogger (it's a no-op
                        # for plain TranscriptLogger).
                        if hasattr(logger, "log_governance_lang_strike"):
                            logger.log_governance_lang_strike(
                                strike=lang_result.strike,
                                lang_code=lang_result.lang_code,
                                confidence=lang_result.confidence,
                                terminated=lang_result.terminate_call,
                            )
                    if lang_result.terminate_call:
                        # Final strike → exit loop. The finally block pushes
                        # None into text_queue so TTS drains the refusal then
                        # exits, which lets the orchestrator close the WS and
                        # Twilio terminate the call via <Hangup/>.
                        return
                    continue

                # Restricted-topic gate: immigration / legal / competitor / etc.
                topic_match = detect_restricted_topic(user_text)
                if topic_match is not None:
                    category, response_text = topic_match
                    print(f"[GOV-TOPIC] {category} -> canned refusal")
                    await text_queue.put(response_text)
                    await text_queue.put("\n")
                    conversation_history.append({"role": "user", "content": user_text})
                    conversation_history.append({"role": "assistant", "content": response_text})
                    if logger is not None:
                        logger.log_bot(response_text)
                        if hasattr(logger, "log_governance_topic_refusal"):
                            logger.log_governance_topic_refusal(category)
                    continue

            # ── RAG context retrieval (Step 7) ──────────────────────────
            # When a KnowledgeBase is wired in, search for relevant chunks
            # before calling the LLM. Inject the best matching context into
            # the user message so the LLM can answer from retrieved facts
            # rather than only the inlined SYSTEM_PROMPT corpus.
            #
            # Falls through silently on any error so the call keeps working
            # even if Postgres is down or the pool is not yet ready.
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
                    print(f"[RAG] Search error (non-fatal, using inline corpus): {type(e).__name__}: {e}")

            # ── Normal LLM path ──────────────────────────────────────────
            conversation_history.append({"role": "user", "content": user_message})

            # One turn = possibly multiple Groq calls if tool use triggers a follow-up.
            # _chat_turn handles its own errors and always emits a "\n" flush.
            await _chat_turn(conversation_history, text_queue, logger,
                             barge_in_event=barge_in_event, turn_hint=turn_hint)
    finally:
        # Defense in depth — TTS must always see a None sentinel so it can exit.
        await text_queue.put(None)


_FINAL_TURN_REMINDER = {
    "role": "system",
    "content": (
        "This is the caller's FINAL turn — they are ending the call. "
        "Reply with ONE brief, warm farewell sentence (max 12 words). "
        "Do not ask questions. Do not offer further help. Do not list anything."
    ),
}


async def _chat_turn(
    history: list,
    text_queue: asyncio.Queue,
    logger=None,
    barge_in_event: asyncio.Event | None = None,
    turn_hint: str | None = None,
) -> None:
    """
    Call Groq with streaming.  If the model requests tool calls, execute them
    and recursively call Groq again with the results.

    Crash-tolerant: any error (malformed tool call, network blip, etc.)
    is caught, a fallback message is queued, history is left consistent,
    and the turn ends with a "\n" flush so TTS does not stall.

    If `barge_in_event` is provided and fires mid-generation, the stream is
    abandoned immediately: no further tokens are pushed to text_queue, no
    partial response is added to history, no "\n" flush is sent. The new
    user turn that fired the barge-in will be processed on the next loop
    iteration in run_llm.
    """
    response_text      = ""
    tool_calls_buffer: dict[int, dict] = {}   # index → {id, name, arguments}

    # ── Per-turn message + token-cap overrides ──────────────────────────
    # For the hangup turn we append a transient brevity reminder (not added
    # to history) and cap max_tokens so the model physically cannot run long.
    if turn_hint == "final_turn":
        messages = history + [_FINAL_TURN_REMINDER]
        max_tokens_override: int | None = 40
    else:
        messages = history
        max_tokens_override = None

    try:
        create_kwargs = dict(
            model=config.GROQ_MODEL,
            temperature=config.GROQ_TEMPERATURE,
            messages=messages,
            tools=config.TOOLS,
            tool_choice="auto",
            stream=True,
        )
        if max_tokens_override is not None:
            create_kwargs["max_tokens"] = max_tokens_override
        stream = await _client.chat.completions.create(**create_kwargs)

        async for chunk in stream:
            # ── Mid-stream barge-in: caller started a new utterance ──────────
            # TTS has already discarded its buffer; we must NOT push any more
            # tokens, NOT add this aborted response to history, and NOT emit
            # the "\n" flush. The just-added user message in history without
            # a paired assistant turn is acceptable — the next user turn
            # follows and the LLM handles back-to-back user messages fine.
            if barge_in_event is not None and barge_in_event.is_set():
                print("[LLM] Barge-in mid-generation -> abandoning stream")
                try:
                    await stream.close()
                except Exception:
                    pass
                return

            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # ── Stream text tokens straight to TTS ───────────────────────────
            if delta.content:
                if not response_text and logger is not None:
                    # First content token of this turn — stamp LLM TTFT.
                    logger.mark_llm_first_token()
                response_text += delta.content
                await text_queue.put(delta.content)

            # ── Accumulate tool call fragments ───────────────────────────────
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_buffer:
                        tool_calls_buffer[idx] = {"id": "", "name": "", "arguments": ""}
                    buf = tool_calls_buffer[idx]
                    if tc.id:
                        buf["id"] += tc.id
                    if tc.function and tc.function.name:
                        buf["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        buf["arguments"] += tc.function.arguments

    except APIError as e:
        # Most common cause: Groq's `tool_use_failed` (model emitted malformed
        # tool-call syntax). Keep the call alive — speak a fallback and bail.
        failed = getattr(e, "body", None)
        print(f"[LLM] Groq APIError: {e}")
        if failed:
            print(f"[LLM] failed_generation body: {failed}")
        # If we got some text before the crash, keep it as the assistant turn.
        if response_text.strip():
            history.append({"role": "assistant", "content": response_text})
            if logger is not None:
                logger.log_bot(response_text)
        else:
            # Nothing to say — push a fallback so the caller hears something.
            await text_queue.put(_FALLBACK_MESSAGE)
            history.append({"role": "assistant", "content": _FALLBACK_MESSAGE})
            if logger is not None:
                logger.log_bot(_FALLBACK_MESSAGE)
        await text_queue.put("\n")
        return
    except Exception as e:
        print(f"[LLM] Unexpected stream error: {type(e).__name__}: {e}")
        if response_text.strip():
            history.append({"role": "assistant", "content": response_text})
            if logger is not None:
                logger.log_bot(response_text)
        else:
            await text_queue.put(_FALLBACK_MESSAGE)
            history.append({"role": "assistant", "content": _FALLBACK_MESSAGE})
            if logger is not None:
                logger.log_bot(_FALLBACK_MESSAGE)
        await text_queue.put("\n")
        return

    # ── No tool calls → add response to history and signal TTS flush ─────────
    if not tool_calls_buffer:
        if response_text:
            history.append({"role": "assistant", "content": response_text})
            if logger is not None:
                logger.log_bot(response_text)
        await text_queue.put("\n")
        return

    # ── Tool call path ────────────────────────────────────────────────────────
    # 1. Record assistant message with tool call requests
    tool_calls_list = [
        {
            "id": buf["id"],
            "type": "function",
            "function": {"name": buf["name"], "arguments": buf["arguments"]},
        }
        for buf in (tool_calls_buffer[i] for i in sorted(tool_calls_buffer))
    ]
    history.append({
        "role": "assistant",
        "content": response_text or None,
        "tool_calls": tool_calls_list,
    })
    # Log any preamble the model spoke before the tool call (it WAS synthesized).
    if response_text.strip() and logger is not None:
        logger.log_bot(response_text)

    # 2. Execute each tool and record results
    for tc_info in tool_calls_list:
        func_name = tc_info["function"]["name"]
        try:
            arguments = json.loads(tc_info["function"]["arguments"])
        except (json.JSONDecodeError, ValueError):
            arguments = {}

        print(f"[LLM] Tool call → {func_name}({arguments})")

        try:
            if func_name in FUNCTION_MAP:
                result = FUNCTION_MAP[func_name](**arguments)
            else:
                result = {"error": f"Function '{func_name}' not found"}
        except Exception as e:
            print(f"[LLM] Tool execution error in {func_name}: {type(e).__name__}: {e}")
            result = {"error": f"{func_name} failed: {e}"}

        print(f"[LLM] Tool result ← {result}")

        history.append({
            "role": "tool",
            "tool_call_id": tc_info["id"],
            "content": json.dumps(result),
        })

    # 3. Call Groq again with tool results to get the final spoken response
    await _chat_turn(history, text_queue, logger, barge_in_event=barge_in_event)
