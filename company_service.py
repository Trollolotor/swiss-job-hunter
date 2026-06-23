"""Structured, reusable company intelligence."""
from __future__ import annotations

import json
import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class CompanyPayload(BaseModel):
    industry: str | None = None
    scope: str | None = None
    summary: str
    aliases: list[str] = Field(default_factory=list)

    @field_validator("scope")
    @classmethod
    def normalize_scope(cls, value):
        if not value:
            return None
        mapping = {"global": "global", "swiss": "Swiss", "regional": "regional", "local": "local"}
        return mapping.get(str(value).strip().lower())


def normalize_company_name(name: str) -> str:
    value = re.sub(r"\s+", " ", name).strip()
    value = re.sub(
        r"\s+(ag|sa|gmbh|sàrl|sarl|ltd\.?|limited|inc\.?|llc|plc|group|holding)$",
        "", value, flags=re.IGNORECASE,
    )
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def company_dict(row) -> dict:
    try:
        aliases = json.loads(row.aliases_json or "[]")
    except json.JSONDecodeError:
        aliases = []
    return {"id": row.id, "name": row.name, "normalized_name": row.normalized_name,
            "aliases": aliases, "industry": row.industry, "scope": row.scope,
            "summary": row.summary, "model": row.model,
            "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None}


def find_company(session, name: str):
    from db.models import CompanyInfo
    normalized = normalize_company_name(name)
    row = session.query(CompanyInfo).filter(CompanyInfo.normalized_name == normalized).first()
    if row:
        return row
    return session.query(CompanyInfo).filter(CompanyInfo.name == name.strip()).first()


async def enrich_company(name: str, force: bool = False) -> tuple[dict, bool]:
    from db.models import CompanyInfo
    from db.session import get_session, init_db
    from llm.prompt_manager import render_prompt
    from llm.structured import call_structured
    init_db()
    clean_name = re.sub(r"\s+", " ", name).strip()
    if not clean_name:
        raise ValueError("company name required")
    with get_session() as session:
        row = find_company(session, clean_name)
        if row and row.summary and row.industry and not force:
            return company_dict(row), True
    system, user, prompt = render_prompt("company_summary", company=clean_name)
    payload, model = await call_structured(
        system=system, user=user, operation="company_summary",
        max_tokens=prompt["max_tokens"], temperature=prompt["temperature"],
        schema=CompanyPayload, use_cache=not force,
    )
    scope = payload.scope if payload.scope in {"global", "Swiss", "regional", "local"} else None
    normalized = normalize_company_name(clean_name)
    with get_session() as session:
        row = find_company(session, clean_name)
        if not row:
            row = CompanyInfo(name=clean_name, normalized_name=normalized)
            session.add(row)
        row.normalized_name = normalized
        row.aliases_json = json.dumps(list(dict.fromkeys(payload.aliases)), ensure_ascii=False)
        row.industry, row.scope, row.summary = payload.industry, scope, payload.summary
        row.model, row.prompt_revision, row.fetched_at = model, prompt["revision"], datetime.utcnow()
        session.flush()
        result = company_dict(row)
    return result, False
