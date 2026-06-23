"""
CV ↔ Job Description match scorer.

Two modes:
  1. Fast (keyword overlap) — no API call, runs on all new jobs.
  2. LLM (semantic)        — routes through llm.router, runs on shortlisted jobs.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, RootModel

from config.settings import settings

# ── Skill keywords grouped by category ────────────────────────────────────────
# Each tuple: (pattern, weight)
# Weight > 1 = core skill (from Leo's CV), counted more heavily

_WEIGHTED_SKILLS: list[tuple[str, float]] = [
    # ── Core perception / AD (Leo's primary expertise) ────────────────────────
    (r"perception",                 2.0),
    (r"autonomous driving",         2.0),
    (r"self.?driving",              2.0),
    (r"adas",                       2.0),
    (r"bev",                        2.0),
    (r"bird.?s.?eye.?view",         2.0),
    (r"sparse4d",                   2.0),
    (r"sensor fusion",              2.0),
    (r"radar.?camera",              2.0),
    (r"camera.?radar",              2.0),
    (r"mmwave",                     2.0),
    (r"lidar",                      1.5),
    (r"3d detection",               2.0),
    (r"object detection",           1.5),
    (r"multi.?object tracking",     2.0),
    (r"\bmot\b",                    1.5),
    (r"trajectory prediction",      2.0),
    (r"wayformer",                  2.0),
    (r"vectornet",                  2.0),
    (r"detr",                       1.5),
    (r"deformable",                 1.5),
    (r"yolo",                       1.5),
    (r"c\-ncap",                    1.5),
    (r"fisheye",                    1.5),
    (r"surround.?view",             1.5),

    # ── Model optimization ─────────────────────────────────────────────────────
    (r"quantization",               2.0),
    (r"\bptq\b",                    2.0),
    (r"\bqat\b",                    2.0),
    (r"mixed.?precision",           2.0),
    (r"int8",                       1.5),
    (r"int16",                      1.5),
    (r"edge deployment",            1.5),
    (r"real.?time inference",       1.5),
    (r"latency",                    1.0),
    (r"model optimization",         1.5),
    (r"pruning",                    1.0),

    # ── Deep learning / ML ─────────────────────────────────────────────────────
    (r"deep learning",              1.5),
    (r"machine learning",           1.0),
    (r"computer vision",            1.5),
    (r"transformer",                1.5),
    (r"attention",                  1.0),
    (r"cross.?attention",           1.5),
    (r"multimodal",                 1.5),
    (r"multi.?modal",               1.5),
    (r"generative",                 1.0),
    (r"diffusion",                  1.0),
    (r"llm",                        1.0),
    (r"fine.?tun",                  1.0),
    (r"rlhf",                       1.0),
    (r"\blora\b",                   1.0),

    # ── Frameworks & tools ─────────────────────────────────────────────────────
    (r"pytorch",                    2.0),
    (r"python",                     1.0),
    (r"c\+\+",                      1.5),
    (r"opencv",                     1.5),
    (r"mmdetection",                1.5),
    (r"mmcv",                       1.5),
    (r"cuda",                       1.5),
    (r"tensorrt",                   1.5),
    (r"onnx",                       1.0),
    (r"docker",                     1.0),
    (r"kubernetes",                 1.0),
    (r"ros\b",                      1.0),
    (r"apollo",                     1.0),

    # ── Generic engineering ────────────────────────────────────────────────────
    (r"algorithm engineer",         1.0),
    (r"research engineer",          1.0),
    (r"ml engineer",                1.0),
    (r"ai engineer",                1.0),
    (r"software engineer",          0.8),
    (r"data scientist",             0.5),
]

_COMPILED: list[tuple[re.Pattern, float, str]] = [
    (re.compile(pat, re.IGNORECASE), weight, pat)
    for pat, weight in _WEIGHTED_SKILLS
]


@dataclass
class MatchResult:
    score: float
    matched_skills: list[str]
    missing_skills: list[str]
    explanation: str
    provider: str = "keyword"


class MatchPayload(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    explanation: str


class KeywordPayload(RootModel[list[dict]]):
    pass


def _extract_weighted(text: str) -> dict[str, float]:
    """Return {skill_label: weight} for all patterns found in text."""
    found = {}
    for pat, weight, label in _COMPILED:
        if pat.search(text):
            # Use a clean label
            clean = label.replace("\\b", "").replace("\\.", ".").replace("?", "").strip()
            found[clean] = weight
    return found


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


async def load_cv_keywords(
    cv_text: str,
    direction: Optional[str] = None,
    cv_path: Optional[Path] = None,
) -> list[tuple[re.Pattern, float, str]]:
    """
    Load compiled keyword patterns for fast_score.
    Cache stored at data/cv_keywords_{direction}.json, invalidated by CV mtime.
    Falls back to hardcoded _COMPILED if extraction fails.
    """
    cache_key = direction or "default"
    cache_file = Path(f"./data/cv_keywords_{cache_key}.json")

    if cv_path is None:
        try:
            from config.settings import Settings
            cv_path = Settings().cv_text_path
        except Exception:
            cv_path = None

    cv_mtime = cv_path.stat().st_mtime if cv_path and cv_path.exists() else 0.0

    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if abs(cached.get("cv_mtime", 0) - cv_mtime) < 1.0:
                compiled = _compile_dynamic(cached["keywords"])
                if compiled:
                    return compiled
        except Exception:
            pass

    keywords = await _extract_keywords_llm(cv_text)
    if keywords:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({"cv_mtime": cv_mtime, "keywords": keywords}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return _compile_dynamic(keywords)

    return _COMPILED  # fallback to hardcoded list


def fast_score(
    cv_text: str,
    jd_text: str,
    compiled: Optional[list[tuple[re.Pattern, float, str]]] = None,
) -> MatchResult:
    """
    Weighted keyword-overlap score.
    Score = sum(weights of matched skills) / sum(weights of all JD skills)
    Capped at 1.0.
    Pass `compiled` to use dynamic CV-extracted keywords instead of hardcoded list.
    """
    _patterns = compiled if compiled is not None else _COMPILED

    if not jd_text or len(jd_text.strip()) < 50:
        return MatchResult(
            score=0.0, matched_skills=[], missing_skills=[],
            explanation="JD too short — run Enrich first for better scoring.",
        )

    def _extract(text: str) -> dict[str, float]:
        found = {}
        for pat, weight, label in _patterns:
            if pat.search(text):
                found[label] = weight
        return found

    cv_skills  = _extract(cv_text)
    jd_skills  = _extract(jd_text)

    if not jd_skills:
        return MatchResult(
            score=0.2, matched_skills=[], missing_skills=[],
            explanation="No recognizable technical keywords found in JD.",
        )

    matched  = {k: w for k, w in jd_skills.items() if k in cv_skills}
    missing  = {k: w for k, w in jd_skills.items() if k not in cv_skills}

    total_jd_weight  = sum(jd_skills.values())
    matched_weight   = sum(matched.values())
    score = min(matched_weight / total_jd_weight, 1.0)

    matched_list = sorted(matched, key=lambda k: -matched[k])
    missing_list = sorted(missing, key=lambda k: -missing[k])

    explanation = (
        f"Matched {len(matched)}/{len(jd_skills)} keywords "
        f"(weighted score {score:.0%}). "
        + (f"Key matches: {', '.join(matched_list[:5])}. " if matched_list else "")
        + (f"Missing: {', '.join(missing_list[:4])}." if missing_list else "Perfect match!")
    )

    return MatchResult(
        score=round(score, 3),
        matched_skills=matched_list,
        missing_skills=missing_list,
        explanation=explanation,
    )


async def llm_score(cv_text: str, job_title: str, jd_text: str, role: str = "General") -> MatchResult:
    """Deep, schema-validated LLM scoring."""
    from llm.structured import call_structured

    from llm.prompt_manager import render_prompt
    system, user, prompt = render_prompt(
        "job_screening", role=role, cv_text=cv_text[:12000],
        job_title=job_title, jd_text=jd_text[:8000],
    )
    payload, provider = await call_structured(
        user=user, system=system, max_tokens=prompt["max_tokens"],
        temperature=prompt["temperature"],
        operation="job_screening",
        schema=MatchPayload,
    )
    return MatchResult(
        score=payload.score,
        matched_skills=payload.matched_skills,
        missing_skills=payload.missing_skills,
        explanation=payload.explanation,
        provider=provider,
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
