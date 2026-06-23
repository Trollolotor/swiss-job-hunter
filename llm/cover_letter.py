"""
Cover letter generation — routes through llm.router (Anthropic / DeepSeek).
Supports English and German output.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from db.models import Job
from llm.router import call_llm

Language = Literal["en", "de"]


def _build_salutation(company: str, language: Language) -> str:
    if language == "de":
        return f"Sehr geehrte Damen und Herren bei {company},"
    return f"Dear Hiring Team at {company},"


async def generate_cover_letter(
    job: Job,
    cv_text: str,
    language: Language = "en",
    max_tokens: int = 800,
    role: str | None = None,
) -> str:
    """
    Generate a personalized cover letter for `job` using the candidate's CV.
    Automatically rotates between Anthropic and DeepSeek per LLM_PROVIDER setting.

    Returns the full letter (salutation + body + sign-off).
    """
    from llm.prompt_manager import render_prompt
    system, user_prompt, prompt = render_prompt(
        f"cover_letter_{language}", role=role or getattr(job, "direction", None) or "General",
        cv_text=cv_text[:4000],
        job_title=job.title,
        company=job.company,
        location=job.location,
        jd_text=job.description[:3000],
    )

    body, provider = await call_llm(
        user=user_prompt,
        system=system,
        max_tokens=min(max_tokens, prompt["max_tokens"]),
        temperature=prompt["temperature"],
        operation="cover_letter",
    )
    print(f"[cover letter] generated via {provider}")

    salutation = _build_salutation(job.company, language)
    from config.settings import settings
    name = settings.apply_from_name or "Your Name"
    sign_off = (
        f"\n\nMit freundlichen Grüßen,\n{name}"
        if language == "de"
        else f"\n\nBest regards,\n{name}"
    )
    return f"{salutation}\n\n{body}{sign_off}"


def save_cover_letter(
    job: Job, letter: str, output_dir: Path = Path("./data/letters")
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_company = job.company.replace(" ", "_").replace("/", "-")[:30]
    filename = f"cover_letter_{job.id}_{safe_company}.txt"
    path = output_dir / filename
    path.write_text(letter, encoding="utf-8")
    return path
