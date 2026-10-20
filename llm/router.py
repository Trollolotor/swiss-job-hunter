"""
LLM provider router — round-robin between Anthropic, DeepSeek, OpenRouter, and Ollama.

DeepSeek, OpenRouter, and Ollama are OpenAI-compatible, so we use the openai SDK for all three.
Anthropic uses its own SDK.

Usage:
    from llm.router import call_llm

    text = await call_llm(
        system="You are a helpful assistant.",
        user="Write a cover letter for...",
        max_tokens=800,
    )
"""
from __future__ import annotations

import asyncio
import itertools
import time
from typing import Optional

LLM_TIMEOUT = 120  # seconds; hung API calls are cancelled and re-raise TimeoutError

from config.settings import settings

# ── Build the provider cycle ───────────────────────────────────────────────────

def _build_cycle() -> itertools.cycle:
    """
    Build a round-robin cycle from whichever providers are configured.
    If LLM_PROVIDER is pinned to a specific provider, always use that one.
    """
    if settings.llm_provider == "anthropic":
        return itertools.cycle(["anthropic"])
    if settings.llm_provider == "deepseek":
        return itertools.cycle(["deepseek"])
    if settings.llm_provider == "openrouter":
        return itertools.cycle(["openrouter"])
    if settings.llm_provider == "ollama":
        return itertools.cycle(["ollama"])

    # "auto" — include only providers that have a key set
    available: list[str] = []
    if settings.anthropic_api_key:
        available.append("anthropic")
    if settings.deepseek_api_key:
        available.append("deepseek")
    if settings.openrouter_api_key:
        available.append("openrouter")
    if settings.ollama_base_url:
        available.append("ollama")

    if not available:
        raise RuntimeError(
            "No LLM provider configured. Set ANTHROPIC_API_KEY, DEEPSEEK_API_KEY, "
            "OPENROUTER_API_KEY, or OLLAMA_BASE_URL."
        )

    return itertools.cycle(available)


_provider_cycle = _build_cycle()


def _next_provider() -> str:
    return next(_provider_cycle)


# ── Anthropic call ─────────────────────────────────────────────────────────────

async def _call_anthropic(system: str, user: str, max_tokens: int) -> str:
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=0)
    messages = [{"role": "user", "content": user}]
    response = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    from llm.spending import remember
    remember(response)
    return response.content[0].text.strip()


# ── DeepSeek call (OpenAI-compatible) ─────────────────────────────────────────

async def _call_deepseek(system: str, user: str, max_tokens: int) -> str:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        max_retries=0,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
    )
    response = await client.chat.completions.create(
        model=settings.deepseek_model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    from llm.spending import remember
    remember(response)
    return (response.choices[0].message.content or "").strip()


# ── OpenRouter call (OpenAI-compatible) ───────────────────────────────────────

async def _call_openrouter(system: str, user: str, max_tokens: int) -> str:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        max_retries=0,
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        default_headers={
            "HTTP-Referer": "https://github.com/Donvink/swiss-job-hunter",
            "X-Title": "Swiss Job Hunter",
        },
    )
    response = await client.chat.completions.create(
        model=settings.openrouter_model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    from llm.spending import remember
    remember(response)
    return (response.choices[0].message.content or "").strip()


# ── Ollama call (OpenAI-compatible) ───────────────────────────────────────────

async def _call_ollama(system: str, user: str, max_tokens: int) -> str:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        max_retries=0,
        api_key="ollama",  # Ollama ignores the key but the SDK requires a non-empty string
        base_url=settings.ollama_base_url,
    )
    response = await client.chat.completions.create(
        model=settings.ollama_model,
        max_tokens=max_tokens,
        reasoning_effort="high" if settings.ollama_think else "none",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )

    from llm.spending import remember
    remember(response)
    return (response.choices[0].message.content or "").strip()


# ── Public interface ───────────────────────────────────────────────────────────

async def call_llm(
    user: str,
    system: str = "You are a helpful assistant.",
    max_tokens: int = 1000,
    provider: Optional[str] = None,
    operation: str = "job_screening",
    model: Optional[str] = None,
    use_cache: bool = True,
    temperature: float = 0.2,
) -> tuple[str, str]:
    """Route named operations through OpenRouter; keep explicit legacy providers."""
    if settings.openrouter_api_key and (provider is None or provider == "openrouter"):
        from llm.task_router import call_task_llm
        return await call_task_llm(
            user=user,
            system=system,
            max_tokens=max_tokens,
            operation=operation,
            model=model,
            use_cache=use_cache,
            temperature=temperature,
        )

    p = provider or _next_provider()
    if p == "anthropic":
        coro = _call_anthropic(system, user, max_tokens)
    elif p == "deepseek":
        coro = _call_deepseek(system, user, max_tokens)
    elif p == "openrouter":
        coro = _call_openrouter(system, user, max_tokens)
    elif p == "ollama":
        coro = _call_ollama(system, user, max_tokens)
    else:
        raise ValueError(f"Unknown provider: {p}")
    from llm.spending import OPERATIONS, reserve, settle, response_metadata
    from llm.task_router import _semaphore, _log
    from config.runtime import get_llm_config
    from analyzer.config import get_config as matching_config
    concurrency = matching_config().concurrency if operation in OPERATIONS else get_llm_config()["concurrency"]
    semaphore = _semaphore("matching" if operation in OPERATIONS else operation, concurrency)
    try:
        await semaphore.acquire()
    except BaseException:
        coro.close()
        raise
    model_name = model or getattr(settings, p + "_model")
    reservation = None
    started = time.monotonic()
    try:
        try:
            reservation = reserve(operation, f"{p}:{model_name}", system + user, max_tokens, free=p == "ollama")
        except BaseException:
            coro.close()
            raise
        response_metadata.set({})
        answer = await asyncio.wait_for(coro, timeout=LLM_TIMEOUT)
        metadata = {**response_metadata.get(), "latency_ms":int((time.monotonic()-started)*1000)}
        settle(reservation, metadata)
        usage = metadata.get("usage", {})
        _log(operation,f"{p}:{model_name}",metadata["latency_ms"],0,True,
             usage.get("prompt_tokens",usage.get("input_tokens")),
             usage.get("completion_tokens",usage.get("output_tokens")))
        return answer, p
    except BaseException as exc:
        settle(reservation, response_metadata.get(), error=exc)
        _log(operation,f"{p}:{model_name}",int((time.monotonic()-started)*1000),0,False,
             error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        semaphore.release()
