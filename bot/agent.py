"""
The actual "data analyst" brain.

Both Gemini and AIPipe are reached through the OpenAI-compatible chat
completions surface (Gemini exposes one at /v1beta/openai/, AIPipe exposes
one directly), so a single agent loop with OpenAI-style tool calling works
against either provider — we just swap the client. Gemini is tried first;
if it errors (quota, 429, etc.) we fall back to AIPipe, same pattern
already used elsewhere in the course work.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from . import config
from .tools import TOOL_IMPLS, TOOL_SPECS

SYSTEM_PROMPT = """You are a rigorous data analyst agent operating inside a Telegram bot.

You will be given a data-analysis question, optionally with earlier turns of
the same conversation for context. You have three tools:

  web_search(query) - find candidate URLs when the question doesn't hand
                       you one directly (e.g. "based on MOSPI data" with no
                       link) - use this FIRST in that situation, then
                       fetch_url on whatever result looks authoritative
  fetch_url(url)     - pull a public dataset or webpage (e.g. MOSPI
                       pages/CSVs, or a URL you found via web_search)
  run_python(code)   - run code with pd/np/json/io already bound (no import
                       statements - they're blocked); you MUST print() what
                       you want back, there is no implicit return value

Rules you must follow:
1. Never guess a number you have not actually computed. If the question needs
   data, fetch it and compute the answer with run_python before answering.
   If no URL was given for data you need, use web_search once or twice to
   find one before giving up - don't just retry fetch_url blindly on guessed
   URLs, and don't spend more than ~3 tool calls searching before settling
   on a source to fetch.
2. When you are ready to give your final answer, respond with ONLY the
   "answer" payload itself, in exactly the shape the question requests -
   e.g. if the question wants {"answer": {"state": "..."}, "log_url": "..."},
   you output just {"state": "..."} and nothing else. A wrapper with the
   log_url will be added by the bot after you're done - never include a
   log_url field yourself, and never wrap your output in an extra "answer" key.
3. No markdown fences, no explanation text outside the JSON, no prose before
   or after it. If the answer is a single value rather than an object (e.g.
   a bare number or string), output just that value in the format asked for.
4. CRITICAL: if the question literally shows an example like
   'Reply with only {"answer": <number>}', that "answer" key belongs to the
   OUTER envelope the bot adds - not something you should type yourself. The
   correct output in that exact case is just the number, e.g. 42 - NOT
   {"answer": 42}. Do not echo the word "answer" as a key in your own output
   unless the question's answer shape genuinely calls for a field named that
   (rare - check the question's own described shape, not example wrapper text).
5. If a multi-turn conversation is provided, use earlier turns only as context;
   you are answering the LAST user message.
6. Keep run_python snippets small and print only what's needed - your context
   window is limited.
7. You have a limited number of steps. If fetch_url isn't returning usable
   data after 2-3 different attempts (e.g. it keeps returning HTML/portal
   pages instead of raw data), stop retrying the same approach and instead
   answer using your best domain knowledge, clearly better than burning your
   remaining steps on repeated failed fetches. A well-reasoned best-effort
   answer beats no answer.
"""


def _to_openai_tools() -> list[dict]:
    return [
        {"type": "function", "function": {"name": s["name"], "description": s["description"], "parameters": s["parameters"]}}
        for s in TOOL_SPECS
    ]


def _client_for(provider: str) -> tuple[OpenAI, str]:
    # Explicit timeout matters a lot here: without one, the SDK defaults to
    # 600s. If a provider is slow/unreachable, that would block this whole
    # synchronous polling loop - and every other chat's replies with it -
    # for up to 10 minutes on a single stuck call, with nothing in the logs
    # to explain why. Failing fast lets the Gemini -> AIPipe fallback (and
    # the grader's own timeout) actually do their job.
    if provider == "gemini":
        return (
            OpenAI(
                api_key=config.GEMINI_API_KEY,
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                timeout=45.0,
                max_retries=1,
            ),
            config.GEMINI_MODEL,
        )
    return (
        OpenAI(
            api_key=config.AIPIPE_API_KEY,
            base_url=config.AIPIPE_BASE_URL,
            timeout=45.0,
            max_retries=1,
        ),
        config.AIPIPE_MODEL,
    )


def _providers_in_order() -> list[str]:
    order = []
    if config.GEMINI_API_KEY:
        order.append("gemini")
    if config.AIPIPE_API_KEY:
        order.append("aipipe")
    return order


@dataclass
class AgentRun:
    """Everything about one agent run, for both the reply and the log upload."""
    log: list[dict] = field(default_factory=list)

    def record(self, event_type: str, **payload: Any) -> None:
        self.log.append({"ts": time.time(), "type": event_type, **payload})


def _chat_completion_with_fallback(messages: list[dict], run: AgentRun):
    last_error = None
    for provider in _providers_in_order():
        client, model = _client_for(provider)
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=_to_openai_tools(),
                tool_choice="auto",
                temperature=0,
            )
            run.record("llm_call", provider=provider, model=model, ok=True)
            return resp
        except Exception as exc:  # noqa: BLE001
            run.record("llm_call", provider=provider, model=model, ok=False, error=str(exc))
            last_error = exc
            continue
    raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")


def answer_question(conversation: list[dict]) -> tuple[str, AgentRun]:
    """
    conversation: list of {"role": "user"|"assistant", "content": str} for the
    chat so far (oldest first), ending with the user message to answer.

    Returns (final_json_text, run_log).
    """
    run = AgentRun()
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + conversation
    run.record("conversation_in", messages=conversation)

    final_text = None
    for step in range(config.MAX_AGENT_STEPS):
        resp = _chat_completion_with_fallback(messages, run)
        choice = resp.choices[0]
        msg = choice.message

        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            reconstructed_calls = []
            for tc in tool_calls:
                call = {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                # Gemini 3 attaches a thought_signature to (at least) the first
                # function call in each step, under extra_content.google. It
                # MUST be echoed back exactly as received or the next call
                # fails validation - see
                # https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures
                extra_content = getattr(tc, "extra_content", None)
                if extra_content is None:
                    model_extra = getattr(tc, "model_extra", None) or {}
                    extra_content = model_extra.get("extra_content")
                if extra_content:
                    call["extra_content"] = extra_content
                else:
                    # No real signature (e.g. this call came from the AIPipe
                    # fallback, or a non-thinking model). If a later step in
                    # this same run falls back to Gemini 3, it would otherwise
                    # reject this history as missing a signature. Google
                    # documents this exact dummy value for injecting
                    # foreign/signature-less function calls:
                    # https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures (FAQ 1)
                    call["extra_content"] = {
                        "google": {"thought_signature": "skip_thought_signature_validator"}
                    }
                reconstructed_calls.append(call)

            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": reconstructed_calls,
            })
            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                run.record("tool_call", step=step, name=name, args=args)
                try:
                    result = TOOL_IMPLS[name](args)
                except Exception as exc:  # noqa: BLE001
                    result = f"TOOL_ERROR: {exc}"
                # keep tool results bounded so the context doesn't blow up
                if isinstance(result, str) and len(result) > 20_000:
                    result = result[:20_000] + "\n...[truncated]"
                run.record("tool_result", step=step, name=name, result=result)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
            continue  # let the model see tool results and continue reasoning

        # No tool call -> model believes it's done.
        final_text = (msg.content or "").strip()
        run.record("final_candidate", step=step, text=final_text)
        break

    if final_text is None:
        # Ran out of steps without the model volunteering a final answer -
        # rather than giving up with null (which can never be right), force
        # one last answer-only call (no more tool use allowed) asking it to
        # commit to its best answer given whatever it already gathered. A
        # plausible best-effort guess has a real chance of being correct;
        # null never does.
        run.record("step_budget_exhausted", steps_used=config.MAX_AGENT_STEPS)
        messages.append({
            "role": "user",
            "content": (
                "You're out of steps to investigate further. Do not call any "
                "more tools. Based on everything you've gathered so far (or "
                "your best domain knowledge if data retrieval didn't fully "
                "succeed), give your single best answer NOW, in exactly the "
                "shape the original question asked for. Respond with only "
                "that answer - no tool calls, no explanation."
            ),
        })
        try:
            client, model = _client_for(_providers_in_order()[0])
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                tool_choice="none",  # force a plain text answer, no more tool use
                temperature=0,
            )
            run.record("llm_call", provider="forced_final", model=model, ok=True)
            final_text = (resp.choices[0].message.content or "").strip()
            run.record("final_candidate", step="forced", text=final_text)
        except Exception as exc:  # noqa: BLE001
            run.record("llm_call", provider="forced_final", ok=False, error=str(exc))
            final_text = json.dumps({"answer": None, "error": "agent did not converge in time"})
            run.record("final_fallback", text=final_text)

    return final_text, run
