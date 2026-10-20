"""Bounded structured investigation using existing OpenRouter credentials/routing."""

import json
import time
from contextvars import ContextVar
from typing import Literal

from pydantic import BaseModel, Field

from config.runtime import get_llm_config
from employers.budget import reserve, settle
from employers.contracts import digest
from llm.task_router import _cache_get, _cache_put, _client, _log, reasoning_options, resolve_models

fence_context = ContextVar("employer_task_fence", default=None)


class AgentStep(BaseModel):
    action: str
    url: str | None = None
    method: Literal["GET", "POST"] = "GET"
    body: dict | None = None
    official_url: str | None = None
    portal_url: str | None = None
    classification: str = "unresolved"
    code: str = ""
    manifest: dict = Field(default_factory=dict)
    config: dict = Field(default_factory=dict)
    reason: str = ""


async def step(operation, evidence, task_id=None, search=False):
    from llm.prompt_manager import render_prompt

    system, user, prompt = render_prompt(operation, evidence=json.dumps(evidence, default=str))
    schema = AgentStep.model_json_schema()
    system += "\nReturn JSON matching this schema: " + json.dumps(schema)
    config = get_llm_config()
    last = None
    for fallback, model in enumerate(resolve_models(operation)):
        if fence_context.get():
            fence_context.get()()
        options = reasoning_options(config, operation)
        key = digest(["employer-agent-v2", model, system, user, search, options])
        cached = _cache_get(key)
        if cached:
            from db.models import BudgetLedger
            from db.session import get_session

            with get_session() as session:
                session.add(
                    BudgetLedger(
                        task_id=task_id,
                        operation=operation,
                        model=model,
                        reserved_usd=0,
                        actual_usd=0,
                        status="cached",
                        metadata_json=json.dumps(
                            {
                                "cache_hit": True,
                                "cache_key": key,
                                "source_id": evidence.get("source", {}).get("id"),
                                "version_id": (evidence.get("previous") or {}).get("id"),
                                "incident_id": (evidence.get("incident") or {}).get("id"),
                                "prompt_revision": prompt["revision"],
                            }
                        ),
                    )
                )
            _log(operation, model, 0, fallback, True, 0, 0)
            return AgentStep.model_validate_json(cached)
        for _ in range(int(config.get("retries", 1)) + 1):
            if fence_context.get():
                fence_context.get()()
            reservation = reserve(
                operation, model, system + user, prompt["max_tokens"], task_id, search
            )
            started = time.monotonic()
            try:
                extra = dict(options)
                if search:
                    extra.update(
                        {
                            "tools": [
                                {
                                    "type": "openrouter:web_search",
                                    "parameters": {
                                        "engine": "exa",
                                        "max_uses": 2,
                                        "max_results": 5,
                                        "max_total_results": 10,
                                        "max_characters": 2000,
                                    },
                                }
                            ],
                            "max_tool_calls": 2,
                        }
                    )
                response = (
                    await _client()
                    .with_options(max_retries=0)
                    .chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        max_tokens=prompt["max_tokens"],
                        temperature=0.1,
                        response_format={"type": "json_object"},
                        timeout=config.get("timeout", 120),
                        extra_body=extra,
                    )
                )
                usage = response.usage.model_dump() if response.usage else {}
                message = response.choices[0].message
                raw = (message.content or "").strip()
                metadata = {
                    "provider_request_id": response.id,
                    "task_id": task_id,
                    "prompt_revision": prompt["revision"],
                    "annotations": [
                        a.model_dump(mode="json") if hasattr(a, "model_dump") else a
                        for a in (getattr(message, "annotations", None) or [])
                    ],
                    "source_id": evidence.get("source", {}).get("id"),
                    "version_id": (evidence.get("previous") or {}).get("id"),
                    "incident_id": (evidence.get("incident") or {}).get("id"),
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "fallback_index": fallback,
                    "search_enabled": search,
                    "cache_hit": False,
                    "reasoning": options.get("reasoning"),
                }
                settle(reservation, usage, metadata)
                if getattr(response.choices[0], "finish_reason", None) == "length":
                    raise ValueError("Structured agent response exhausted its output allowance")
                _log(
                    operation,
                    model,
                    int((time.monotonic() - started) * 1000),
                    fallback,
                    True,
                    usage.get("prompt_tokens", usage.get("input_tokens")),
                    usage.get("completion_tokens", usage.get("output_tokens")),
                )
                if raw.startswith("```"):
                    raw = "\n".join(raw.splitlines()[1:-1])
                parsed = AgentStep.model_validate_json(raw)
                _cache_put(key, operation, model, raw)
                return parsed
            except Exception as exc:
                last = exc
                from employers.budget import fail

                fail(reservation, exc)
                _log(
                    operation,
                    model,
                    int((time.monotonic() - started) * 1000),
                    fallback,
                    False,
                    error=f"{type(exc).__name__}: {exc}"[:500],
                )
    raise RuntimeError(f"Investigation failed: {type(last).__name__}")
