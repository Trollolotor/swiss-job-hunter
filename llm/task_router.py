"""OpenRouter task routing with per-operation models and ordered fallbacks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Optional

from config.runtime import get_llm_config
from config.settings import settings

_semaphores: dict[tuple[int, str, int], asyncio.Semaphore] = {}
_clients: dict[int, object] = {}


def _semaphore(operation: str, limit: int) -> asyncio.Semaphore:
    key = (id(asyncio.get_running_loop()), operation, limit)
    if key not in _semaphores:
        _semaphores[key] = asyncio.Semaphore(limit)
    return _semaphores[key]


def _client():
    """Reuse OpenRouter connection pools within each event loop."""
    from openai import AsyncOpenAI

    loop_id = id(asyncio.get_running_loop())
    client = _clients.get(loop_id)
    if client is None:
        client = AsyncOpenAI(
            max_retries=0,
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            default_headers={
                "HTTP-Referer": "https://github.com/Donvink/swiss-job-hunter",
                "X-Title": "Swiss Job Hunter",
            },
        )
        _clients[loop_id] = client
    return client


def resolve_models(operation: str, explicit_model: Optional[str] = None) -> list[str]:
    config = get_llm_config()
    task = config.get("operations", {}).get(operation, {})
    primary = explicit_model or str(task.get("model", "")).strip() or config["default_model"]
    fallbacks = task.get("fallbacks") or config.get("global_fallbacks", [])
    return list(dict.fromkeys([primary, *[str(m).strip() for m in fallbacks if str(m).strip()]]))


def reasoning_options(config, operation):
    effort = config.get("operations", {}).get(operation, {}).get("reasoning_effort")
    return {"reasoning": {"effort": effort}} if effort else {}


def _cache_key(
    operation: str, model: str, system: str, user: str, max_tokens: int, temperature: float = 0.2
) -> str:
    raw = "\0".join((operation, model, system, user, str(max_tokens), str(temperature)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> Optional[str]:
    try:
        from db.models import LLMCache
        from db.session import get_session, init_db

        init_db()
        with get_session() as session:
            row = session.get(LLMCache, key)
            return row.response_text if row else None
    except Exception:
        return None


def _cache_put(key: str, operation: str, model: str, response: str) -> None:
    try:
        from db.models import LLMCache
        from db.session import get_session, init_db

        init_db()
        with get_session() as session:
            if not session.get(LLMCache, key):
                session.add(
                    LLMCache(
                        cache_key=key, operation=operation, model=model, response_text=response
                    )
                )
    except Exception:
        pass


def _log(
    operation: str,
    model: str,
    latency_ms: int,
    fallback_index: int,
    success: bool,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    try:
        from db.models import LLMCallLog
        from db.session import get_session, init_db

        init_db()
        with get_session() as session:
            session.add(
                LLMCallLog(
                    operation=operation,
                    model=model,
                    latency_ms=latency_ms,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    fallback_index=fallback_index,
                    success=success,
                    error=(error or "")[:500] or None,
                )
            )
    except Exception:
        pass


async def call_task_llm(
    *,
    user: str,
    system: str,
    max_tokens: int,
    operation: str,
    model: Optional[str] = None,
    use_cache: bool = True,
    temperature: float = 0.2,
) -> tuple[str, str]:
    if operation in {
        "employer_resolution",
        "connector_investigation",
        "connector_generation",
        "connector_repair",
    }:
        raise ValueError("Employer operations must run through the budgeted employer workflow")
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required for task-based LLM routing")

    from llm.spending import OPERATIONS, reserve, settle

    config = get_llm_config()
    concurrency = int(config.get("concurrency", 10))
    slot = operation
    if operation in OPERATIONS:
        from analyzer.config import get_config

        concurrency, slot = get_config().concurrency, "matching"
    models = resolve_models(operation, model)
    retries, timeout = int(config.get("retries", 1)), int(config.get("timeout", 120))
    client, last_error = _client(), None
    for fallback_index, current_model in enumerate(models):
        options = reasoning_options(config, operation)
        key = _cache_key(
            operation,
            current_model,
            system,
            user + json.dumps(options, sort_keys=True) if options else user,
            max_tokens,
            temperature,
        )
        cached = _cache_get(key) if use_cache else None
        if cached is not None:
            settle(None, cache_hit=True, operation=operation, model=current_model)
            _log(operation, current_model, 0, fallback_index, True, 0, 0)
            return cached, current_model
        for attempt in range(retries + 1):
            async with _semaphore(slot, concurrency):
                # No reservation is held by a call still waiting for its slot.
                reservation = reserve(operation, current_model, system + user, max_tokens)
                started = time.monotonic()
                try:
                    response = await asyncio.wait_for(
                        client.chat.completions.create(
                            model=current_model,
                            max_tokens=max_tokens,
                            temperature=temperature,
                            extra_body=options,
                            messages=[
                                {"role": "system", "content": system},
                                {"role": "user", "content": user},
                            ],
                        ),
                        timeout,
                    )
                    usage = response.usage.model_dump() if response.usage else {}
                    latency = int((time.monotonic() - started) * 1000)
                    settle(
                        reservation,
                        {
                            "usage": usage,
                            "request_id": response.id,
                            "fallback_index": fallback_index,
                            "latency_ms": latency,
                        },
                    )
                    reservation = None
                    if getattr(response.choices[0], "finish_reason", None) == "length":
                        raise ValueError("Model response was truncated")
                    text = (response.choices[0].message.content or "").strip()
                    if use_cache:
                        _cache_put(key, operation, current_model, text)
                    _log(
                        operation,
                        current_model,
                        latency,
                        fallback_index,
                        True,
                        usage.get("prompt_tokens"),
                        usage.get("completion_tokens"),
                    )
                    return text, current_model
                except asyncio.CancelledError:
                    settle(reservation, error="Request cancelled; billing outcome unknown")
                    raise
                except Exception as exc:
                    settle(reservation, error=exc)
                    last_error = exc
                    _log(
                        operation,
                        current_model,
                        int((time.monotonic() - started) * 1000),
                        fallback_index,
                        False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            if attempt < retries:
                await asyncio.sleep(min(2**attempt, 4))
    raise RuntimeError(f"All OpenRouter models failed for {operation}: {last_error}")
