"""
CV ↔ Job Description match scorer.

Two modes:
  1. Fast (keyword overlap) — no API call, runs on all new jobs.
  2. LLM (semantic)        — routes through llm.router, runs on shortlisted jobs.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, RootModel

from config.settings import settings

# Keywords are derived from the selected CV, never a built-in candidate's skills.
KEYWORD_VERSION = "cv-local-v2"
_STOP = set("a an and are as at be by for from in is it of on or the to with you your we our this that have has will can skills experience expert needed requirements role senior developer engineer years work working und der die das mit für von ein eine im zu des et les des pour dans une un du le la en de au sur nous vous with".split())


def cv_terms(cv_text):
    terms = []
    seen = set()
    for token in re.findall(r"[\w][\w+#./-]*", cv_text.casefold()):
        token = token.strip("./-")
        if len(token) < 2 or token in _STOP or token.isdigit() or token in seen:
            continue
        seen.add(token)
        terms.append({"keyword": token, "weight": 1.0})
    return terms[:300]


@dataclass
class MatchResult:
    score: float
    matched_skills: list[str]
    missing_skills: list[str]
    explanation: str
    provider: str = "keyword"
    requirements: list[dict] = field(default_factory=list)


class RequirementEvidence(BaseModel):
    requirement: str
    importance: Literal["mandatory", "preferred", "unspecified"] = "unspecified"
    state: Literal["supported", "not_evidenced", "unknown"] = "unknown"
    jd_evidence: str = ""
    cv_evidence: str = ""


class MatchPayload(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    explanation: str
    requirements: list[RequirementEvidence] = Field(default_factory=list)


class KeywordPayload(RootModel[list[dict]]):
    pass


def _compile_dynamic(raw: list[dict]) -> list[tuple[re.Pattern, float, str]]:
    """Convert LLM-extracted keyword dicts to compiled pattern tuples."""
    result = []
    for item in raw:
        kw = str(item.get("keyword", "")).strip()
        if not kw:
            continue
        weight = float(item.get("weight", 1.0))
        escaped = re.escape(kw)
        pattern = re.compile(r'(?<!\w)' + escaped + r'(?!\w)', re.IGNORECASE)
        result.append((pattern, weight, kw))
    return result


async def _extract_keywords_llm(cv_text: str) -> list[dict]:
    """Call LLM once to extract skill keywords from CV. Returns list of {keyword, weight}."""
    from llm.prompt_manager import render_prompt
    from llm.structured import call_structured
    system, user, prompt = render_prompt("keyword_extraction", cv_text=cv_text[:5000])
    try:
        payload, _ = await call_structured(
            user=user, system=system, max_tokens=prompt["max_tokens"],
            temperature=prompt["temperature"], operation="keyword_extraction",
            schema=KeywordPayload,
        )
        return payload.root
    except ValueError:
        return []


async def load_cv_keywords(cv_text, direction=None, cv_path=None):
    """Content-addressed local cache: this function never calls an LLM."""
    from db.models import AppSetting
    from db.session import get_session
    from employers.contracts import digest
    key = "cv-keywords:" + digest([KEYWORD_VERSION, cv_text])
    with get_session() as session:
        row = session.get(AppSetting, key)
        if row:
            return _compile_dynamic(json.loads(row.value_json)["keywords"])
        keywords = cv_terms(cv_text)
        session.add(AppSetting(key=key, value_json=json.dumps({"keywords": keywords})))
    return _compile_dynamic(keywords)


def fast_score(cv_text, jd_text, compiled=None):
    """Provisional CV keyword coverage; it cannot establish missing requirements."""
    patterns = compiled if compiled is not None else _compile_dynamic(cv_terms(cv_text))
    if not (jd_text or "").strip():
        return MatchResult(0.0, [], [], "Description unavailable; fit is unknown.")
    evidenced = [(pat, weight, label) for pat, weight, label in patterns if pat.search(cv_text)]
    matched = [(weight, label) for pat, weight, label in evidenced if pat.search(jd_text)]
    total = sum(weight for _, weight, _ in evidenced)
    score = sum(weight for weight, _ in matched) / total if total else 0.0
    return MatchResult(round(min(score, 1), 3), [label for _, label in matched], [],
        f"Provisional CV keyword coverage: {len(matched)}/{len(evidenced)} terms. "
        "Requirements and overall fit need evaluation.")


async def llm_score(cv_text: str, job_title: str, jd_text: str, role: str = "General") -> MatchResult:
    """Deep, schema-validated LLM scoring."""
    from llm.prompt_manager import render_prompt
    from llm.structured import call_structured
    system, user, prompt = render_prompt(
        "job_screening", role=role, cv_text=cv_text[:64000],
        job_title=job_title, jd_text=jd_text[:64000],
    )
    system += ("\nReturn score, matched_skills, missing_skills, explanation, and requirements. "
               "For each requirement give requirement, importance (mandatory/preferred/unspecified), "
               "state (supported/not_evidenced/unknown), jd_evidence and cv_evidence as exact excerpts. "
               "Missing CV evidence is not proof the candidate lacks a skill. "
               "Treat unspecified or omitted requirements as unknown.")
    if len(jd_text) > 64000 or len(cv_text) > 64000:
        # A truncated ad is not eligible for an automatic rejection decision.
        raise ValueError("Description exceeds screening context; full evaluation required")
    payload, provider = await call_structured(
        user=user, system=system, max_tokens=prompt["max_tokens"],
        temperature=prompt["temperature"],
        operation="job_screening",
        schema=MatchPayload,
    )
    norm = lambda value: " ".join(value.casefold().split())
    for requirement in payload.requirements:
        if not requirement.jd_evidence or norm(requirement.jd_evidence) not in norm(jd_text):
            requirement.state, requirement.jd_evidence = "unknown", ""
        if requirement.state == "supported" and (not requirement.cv_evidence or norm(requirement.cv_evidence) not in norm(cv_text)):
            requirement.state, requirement.cv_evidence = "unknown", ""
    return MatchResult(
        score=payload.score,
        matched_skills=payload.matched_skills,
        missing_skills=payload.missing_skills,
        explanation=payload.explanation,
        provider=provider,
        requirements=[r.model_dump() for r in payload.requirements],
    )


def load_cv_text(path: Optional[Path] = None, direction: Optional[str] = None) -> str:
    if direction:
        try:
            from db.models import SearchProfile
            from db.session import get_session, init_db
            init_db()
            with get_session() as session:
                profile = session.query(SearchProfile).filter_by(slug=direction).first()
                if profile and profile.cv_text.strip():
                    return profile.cv_text
        except Exception:
            pass
        candidate = Path(f"./data/cv_{direction}.txt")
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    p = path or settings.cv_text_path
    if not p.exists():
        raise FileNotFoundError(
            f"CV text not found at {p}. "
            "Copy your CV as plain text to data/cv.txt"
        )
    return p.read_text(encoding="utf-8")
