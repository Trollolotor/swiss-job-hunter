"""Profiles and runtime settings API."""
from __future__ import annotations

import json
import re
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from config.runtime import (
    get_llm_config,
    get_priority_config,
    put_setting,
    validate_llm_config,
    validate_priority_config,
)
from config.settings import settings

router = APIRouter()


class ProfileBody(BaseModel):
    slug: str
    name: str
    keywords: list[str] = Field(default_factory=list)
    cv_text: str = ""
    active: bool = True


def _profile_dict(profile) -> dict:
    try:
        keywords = json.loads(profile.keywords_json)
    except json.JSONDecodeError:
        keywords = []
    return {
        "id": profile.id,
        "slug": profile.slug,
        "name": profile.name,
        "keywords": keywords,
        "cv_text": profile.cv_text,
        "active": profile.active,
    }


@router.get("/profiles")
def list_profiles():
    from db.models import SearchProfile
    from db.session import get_session, init_db
    init_db()
    with get_session() as session:
        return [_profile_dict(p) for p in session.query(SearchProfile).order_by(SearchProfile.name)]


@router.post("/profiles")
def create_profile(body: ProfileBody):
    from db.models import SearchProfile
    from db.session import get_session, init_db
    init_db()
    slug = re.sub(r"[^a-z0-9_-]+", "-", body.slug.lower()).strip("-")
    if not slug:
        raise HTTPException(400, "valid slug required")
    with get_session() as session:
        if session.query(SearchProfile).filter_by(slug=slug).first():
            raise HTTPException(409, "profile slug already exists")
        row = SearchProfile(
            slug=slug, name=body.name.strip() or slug,
            keywords_json=json.dumps(body.keywords, ensure_ascii=False),
            cv_text=body.cv_text, active=body.active,
        )
        session.add(row)
        session.flush()
        result = _profile_dict(row)
    return result


@router.put("/profiles/{profile_id}")
def update_profile(profile_id: int, body: ProfileBody):
    from db.models import SearchProfile
    from db.session import get_session, init_db
    init_db()
    slug = re.sub(r"[^a-z0-9_-]+", "-", body.slug.lower()).strip("-")
    with get_session() as session:
        row = session.get(SearchProfile, profile_id)
        if not row:
            raise HTTPException(404, "profile not found")
        duplicate = session.query(SearchProfile).filter(
            SearchProfile.slug == slug, SearchProfile.id != profile_id
        ).first()
        if duplicate:
            raise HTTPException(409, "profile slug already exists")
        row.slug, row.name = slug, body.name.strip() or slug
        row.keywords_json = json.dumps(body.keywords, ensure_ascii=False)
        row.cv_text, row.active = body.cv_text, body.active
        session.flush()
        result = _profile_dict(row)
    return result


@router.delete("/profiles/{profile_id}")
def delete_profile(profile_id: int):
    from db.models import JobProfile, SearchProfile
    from db.session import get_session, init_db
    init_db()
    with get_session() as session:
        row = session.get(SearchProfile, profile_id)
        if not row:
            raise HTTPException(404, "profile not found")
        session.query(JobProfile).filter_by(profile_id=profile_id).delete()
        session.delete(row)
    return {"ok": True}


@router.get("/settings/llm")
def read_llm_settings():
    return {
        **get_llm_config(),
        "openrouter_configured": bool(settings.openrouter_api_key),
    }


@router.put("/settings/llm")
def write_llm_settings(body: dict):
    try:
        config = validate_llm_config(body)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    put_setting("llm", config)
    return {**config, "openrouter_configured": bool(settings.openrouter_api_key)}


@router.post("/settings/llm/test")
async def test_llm_settings(body: dict):
    from llm.task_router import call_task_llm
    operation = str(body.get("operation", "job_screening"))
    model = str(body.get("model", "")).strip() or None
    text, used = await call_task_llm(
        user="Reply with exactly: OK",
        system="You are a connectivity test.",
        max_tokens=8,
        operation=operation,
        model=model,
    )
    return {"ok": text.strip().upper().startswith("OK"), "model": used, "response": text[:100]}


@router.get("/settings/search-priority")
def read_priority_settings():
    return get_priority_config()


@router.put("/settings/search-priority")
def write_priority_settings(body: dict):
    try:
        config = validate_priority_config(body)
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    put_setting("search_priority", config)
    return config

