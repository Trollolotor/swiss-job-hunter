"""Validated JSON LLM responses with a same-model repair attempt."""
from __future__ import annotations

import json
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from llm.router import call_llm

T = TypeVar("T", bound=BaseModel)


def _json_object(raw: str) -> str:
    """Accept JSON or one fenced JSON block, without reconstructing partial output."""
    cleaned = raw.strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 3:
            cleaned = "\n".join(lines[1:-1]).strip()
    json.loads(cleaned)
    return cleaned


async def call_structured(
    *,
    user: str,
    system: str,
    max_tokens: int,
    operation: str,
    schema: type[T],
    model: str | None = None,
    use_cache: bool = True,
    temperature: float = 0.2,
) -> tuple[T, str]:
    raw, model = await call_llm(
        user=user, system=system, max_tokens=max_tokens, operation=operation,
        model=model, use_cache=use_cache, temperature=temperature,
    )
    try:
        return schema.model_validate_json(_json_object(raw)), model
    except (ValidationError, ValueError, json.JSONDecodeError) as first_error:
        repair = (
            "Repair the response below so it is valid JSON matching this JSON schema. "
            "Preserve the original meaning, do not add facts, and output JSON only.\n\n"
            f"Schema:\n{json.dumps(schema.model_json_schema())}\n\nResponse:\n{raw}"
        )
        fixed, _ = await call_llm(
            user=repair,
            system="You repair JSON syntax and schema errors only.",
            max_tokens=max_tokens,
            operation=operation,
            model=model,
            use_cache=use_cache,
            temperature=temperature,
        )
        try:
            return schema.model_validate_json(_json_object(fixed)), model
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid structured response after repair: {exc}") from first_error
