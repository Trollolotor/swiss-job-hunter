"""Bounded structured investigation using existing OpenRouter credentials/routing."""

import json
import time

from pydantic import BaseModel, Field

from config.runtime import get_llm_config
from employers.budget import reserve, settle
from employers.contracts import digest
from llm.task_router import _cache_get, _cache_put, _client, _log, resolve_models


class AgentStep(BaseModel):
    action: str
    url: str | None = None
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
        key = digest(["employer-agent-v1", model, system, user, search])
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
                        metadata_json=json.dumps({"cache_hit": True, "cache_key": key}),
                    )
                )
            _log(operation, model, 0, fallback, True, 0, 0)
            return AgentStep.model_validate_json(cached)
        for _ in range(int(config.get("retries", 1)) + 1):
            reservation = reserve(
                operation, model, system + user, prompt["max_tokens"], task_id, search
            )
            started = time.monotonic()
            try:
                extra = {}
                if search:
                    extra = {
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
                    "prompt_revision":prompt["revision"],
                    "annotations": [a.model_dump(mode="json") if hasattr(a,"model_dump") else a
                                    for a in (getattr(message,"annotations",None) or [])],
                }
                settle(reservation, usage, metadata)
                _log(
                    operation,
                    model,
                    int((time.monotonic() - started) * 1000),
                    fallback,
                    True,
                    usage.get("prompt_tokens",usage.get("input_tokens")),
                    usage.get("completion_tokens",usage.get("output_tokens")),
                )
                if raw.startswith("```"):
                    raw = "\n".join(raw.splitlines()[1:-1])
                parsed = AgentStep.model_validate_json(raw)
                _cache_put(key, operation, model, raw)
                return parsed
            except Exception as exc:
                last = exc
                _log(
                    operation,
                    model,
                    int((time.monotonic() - started) * 1000),
                    fallback,
                    False,
                    error=f"{type(exc).__name__}: {exc}"[:500],
                )
    raise RuntimeError(f"Investigation failed: {type(last).__name__}")
