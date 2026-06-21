"""Validated JSON LLM responses with a same-model repair attempt."""
from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from llm.router import call_llm

T = TypeVar("T", bound=BaseModel)


def _json_object(raw: str) -> str:
    cleaned = re.sub(r"^\`\`\`[a-z]*\n?", "", raw.strip())
    cleaned = re.sub(r"\n?\`\`\`$", "", cleaned)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        raise ValueError("response contains no JSON object")
    return match.group(0)


async def call_structured(
    *,
    user: str,
    system: str,
    max_tokens: int,
    operation: str,
    schema: type[T],
) -> tuple[T, str]:
    raw, model = await call_llm(
        user=user, system=system, max_tokens=max_tokens, operation=operation
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
        )
        try:
            return schema.model_validate_json(_json_object(fixed)), model
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid structured response after repair: {exc}") from first_error

