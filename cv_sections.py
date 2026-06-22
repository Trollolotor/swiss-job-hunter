"""Canonical CV sections shared by profiles, imports, and LLM workflows."""
from __future__ import annotations

import re
from typing import Iterable

from pydantic import BaseModel, ConfigDict

CV_SECTION_ORDER = (
    "about",
    "experience",
    "education",
    "projects",
    "skills",
    "certifications",
    "languages",
    "publications",
    "volunteering",
    "awards",
    "additional",
)

CV_SECTION_LABELS = {
    "about": "About",
    "experience": "Experience",
    "education": "Education",
    "projects": "Projects",
    "skills": "Skills",
    "certifications": "Certifications",
    "languages": "Languages",
    "publications": "Publications",
    "volunteering": "Volunteering",
    "awards": "Awards",
    "additional": "Additional",
}


class CVSections(BaseModel):
    model_config = ConfigDict(extra="ignore")

    about: str = ""
    experience: str = ""
    education: str = ""
    projects: str = ""
    skills: str = ""
    certifications: str = ""
    languages: str = ""
    publications: str = ""
    volunteering: str = ""
    awards: str = ""
    additional: str = ""


_HEADING_ALIASES = {
    "about": "about", "summary": "about", "profile": "about", "objective": "about",
    "experience": "experience", "work experience": "experience", "employment": "experience",
    "education": "education", "academic background": "education",
    "projects": "projects", "selected projects": "projects",
    "skills": "skills", "technical skills": "skills", "competencies": "skills",
    "certifications": "certifications", "certificates": "certifications", "licenses": "certifications",
    "languages": "languages", "language": "languages",
    "publications": "publications", "volunteering": "volunteering",
    "volunteer experience": "volunteering", "awards": "awards", "honors": "awards",
    "additional": "additional", "additional information": "additional",
}


def empty_sections() -> dict[str, str]:
    return {key: "" for key in CV_SECTION_ORDER}


def normalize_sections(value: dict | CVSections | None) -> dict[str, str]:
    raw = value.model_dump() if isinstance(value, CVSections) else (value or {})
    return {key: str(raw.get(key, "") or "").strip() for key in CV_SECTION_ORDER}


def compile_cv_text(sections: dict | CVSections) -> str:
    normalized = normalize_sections(sections)
    return "\n\n".join(
        f"## {CV_SECTION_LABELS[key]}\n{normalized[key]}"
        for key in CV_SECTION_ORDER if normalized[key]
    )


def parse_legacy_cv(text: str) -> dict[str, str]:
    """Best-effort heading split; all unclassified text is preserved in Additional."""
    result = empty_sections()
    current: str | None = None
    unclassified: list[str] = []
    buffers: dict[str, list[str]] = {key: [] for key in CV_SECTION_ORDER}
    for line in (text or "").splitlines():
        heading = re.sub(r"^[#*\s]+|[:\s]+$", "", line).strip().lower()
        key = _HEADING_ALIASES.get(heading)
        if key:
            current = key
            continue
        if current:
            buffers[current].append(line)
        else:
            unclassified.append(line)
    for key, lines in buffers.items():
        result[key] = "\n".join(lines).strip()
    leftover = "\n".join(unclassified).strip()
    if leftover:
        result["additional"] = "\n\n".join(filter(None, [result["additional"], leftover]))
    if not any(result.values()) and text.strip():
        result["additional"] = text.strip()
    return result


def normalize_keywords(values: Iterable[str] | str | None) -> list[str]:
    items = [values] if isinstance(values, str) else list(values or [])
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        for part in re.split(r"[,;\n]+", str(item)):
            tag = re.sub(r"\s+", " ", part).strip()
            key = tag.casefold()
            if tag and key not in seen:
                seen.add(key)
                normalized.append(tag[:200])
    return normalized[:100]
