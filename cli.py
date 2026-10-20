"""
Swiss Job Hunter — CLI
Usage: sjh <command> [options]
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

app = typer.Typer(
    name="sjh",
    help="🇨🇭 Swiss Job Hunter — search, deduplicate, analyze, apply.",
    rich_markup_mode="rich",
)
console = Console()


# ── search ────────────────────────────────────────────────────────────────────
@app.command()
def search(
    keyword: str = typer.Argument(..., help="Job title / keywords to search"),
    location: str = typer.Option("Zürich", "--location", "-l", help="City or region"),
    sources: Optional[List[str]] = typer.Option(
        None, "--source", "-s", help="Sources to scrape (default: all)"
    ),
    max_pages: int = typer.Option(5, "--pages", "-p", help="Max pages per source"),
    no_semantic: bool = typer.Option(False, "--no-semantic", help="Skip semantic dedup"),
) -> None:
    """Scrape job listings and store them (with dedup)."""
    asyncio.run(_search(keyword, location, sources, max_pages, not no_semantic))


async def _search(
    keyword: str,
    location: str,
    sources: Optional[List[str]],
    max_pages: int,
    semantic: bool,
) -> None:
    from scrapers import ALL_SOURCES, SCRAPER_REGISTRY
    from scrapers.collection import collect_board
    from db.session import init_db
    init_db()
    for source in sources or ALL_SOURCES:
        cls = SCRAPER_REGISTRY.get(source)
        if not cls:
            console.print(f"Unknown source: {source}")
            continue
        async with cls() as scraper:
            try:
                async for scraped, job, created in collect_board(scraper, keyword, location, max_pages):
                    if created:
                        console.print(f"+ #{job.id}: {scraped.title}")
            except Exception as exc:
                console.print(f"{source} failed: {exc}")
            console.print(scraper.report)


# ── enrich ───────────────────────────────────────────────────────────────────
@app.command()
def enrich(
    limit: int = typer.Option(50, "--limit", "-n", help="Max jobs to enrich"),
    source: str = typer.Option("jobs.ch", "--source", "-s", help="Source to enrich"),
) -> None:
    """Fetch full descriptions for jobs that only have preview text."""
    asyncio.run(_enrich(limit, source))


async def _enrich(limit: int, source: str) -> None:
    from db.models import Job
    from db.session import get_session
    from analyzer.enrichment import enrich
    from analyzer.quality import needs_enrichment
    with get_session() as session:
        ids = [j.id for j in session.query(Job).filter_by(source=source).order_by(Job.scraped_at.desc())
               if needs_enrichment(j)][:limit]
    for jid in ids:
        complete = await enrich(jid)
        console.print(f"#{jid}: {'complete' if complete else 'incomplete/closed'}")


# ── analyze ───────────────────────────────────────────────────────────────────
@app.command()
def analyze(
    limit: int = typer.Option(100, "--limit", "-n", help="Max jobs to analyze"),
    llm: bool = typer.Option(False, "--llm", help="Use LLM scoring (slower, more accurate)"),
    min_score: float = typer.Option(0.3, "--min-score", help="Min score to shortlist"),
    rescore: bool = typer.Option(False, "--rescore", help="Re-score all jobs regardless of status"),
    concurrency: int = typer.Option(5, "--concurrency", "-c", help="Parallel LLM workers"),
    profile_id: Optional[int] = typer.Option(None, "--profile-id", help="CV profile; default: all active"),
) -> None:
    """Score jobs against your CV and shortlist top matches."""
    asyncio.run(_analyze(limit, llm, min_score, rescore, concurrency, profile_id))


async def _analyze(limit: int, use_llm: bool, min_score: float, rescore: bool,
                   concurrency: int, profile_id=None) -> None:
    from analyzer.matching import resolve_profiles, evaluate
    from db.models import Job
    from db.session import get_session
    with get_session() as session:
        pids = [p.id for p in resolve_profiles(session, profile_id)]
        ids = [r[0] for r in session.query(Job.id).order_by(Job.scraped_at.desc()).limit(limit)]
    sem = asyncio.Semaphore(concurrency)
    async def one(jid, pid):
        async with sem:
            try:
                result = await evaluate(jid, pid, llm=use_llm, force=rescore, shortlist=min_score)
                console.print(f"#{jid} profile {pid}: {result}")
            except Exception as exc:
                console.print(f"#{jid} profile {pid}: {exc}")
    await asyncio.gather(*[one(jid, pid) for jid in ids for pid in pids])


# ── top ───────────────────────────────────────────────────────────────────────
@app.command()
def top(
    limit: int = typer.Option(20, "--limit", "-n"),
    min_score: float = typer.Option(0.0, "--min-score"),
) -> None:
    """Show top matching jobs."""
    from analyzer.report import print_top_matches
    print_top_matches(limit=limit, min_score=min_score)


# ── digest ────────────────────────────────────────────────────────────────────
@app.command()
def digest() -> None:
    """Show daily job search summary."""
    from analyzer.report import daily_digest, pipeline_summary
    daily_digest()
    pipeline_summary()


# ── cover ─────────────────────────────────────────────────────────────────────
@app.command()
def cover(
    job_id: int = typer.Argument(..., help="Job ID to generate cover letter for"),
    language: str = typer.Option("en", "--lang", "-l", help="Language: en | de"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path"),
) -> None:
    """Generate a cover letter for a specific job."""
    asyncio.run(_cover(job_id, language, output))  # type: ignore[arg-type]


async def _cover(job_id: int, language: str, output: Optional[str]) -> None:
    from analyzer.scorer import load_cv_text
    from llm.cover_letter import generate_cover_letter, save_cover_letter
    from db.models import Job
    from db.session import get_session

    with get_session() as session:
        job = session.get(Job, job_id)
        if not job:
            console.print(f"[red]Job {job_id} not found[/red]")
            raise typer.Exit(1)

    cv_text = load_cv_text()
    console.print(f"Generating cover letter for: [bold]{job.title}[/bold] @ {job.company}...")

    letter = await generate_cover_letter(job, cv_text, language=language)  # type: ignore[arg-type]

    if output:
        from pathlib import Path
        Path(output).write_text(letter, encoding="utf-8")
        console.print(f"[green]Saved to {output}[/green]")
    else:
        console.print("\n" + "=" * 60)
        console.print(letter)
        console.print("=" * 60)


# ── status ────────────────────────────────────────────────────────────────────
@app.command()
def status(
    job_id: int = typer.Argument(..., help="Job ID"),
    new_status: str = typer.Argument(..., help="New status: applied | rejected | interviewing | offer"),
) -> None:
    """Update the status of a job in the pipeline."""
    from db.models import Job, JobStatus
    from db.session import get_session

    try:
        s = JobStatus(new_status)
    except ValueError:
        valid = [x.value for x in JobStatus]
        console.print(f"[red]Invalid status. Valid: {valid}[/red]")
        raise typer.Exit(1)

    with get_session() as session:
        job = session.get(Job, job_id)
        if not job:
            console.print(f"[red]Job {job_id} not found[/red]")
            raise typer.Exit(1)
        from job_lifecycle import transition
        transition(job, s, origin="user", reason="CLI status change")
    console.print(f"[green]Job {job_id} → {new_status}[/green]")


if __name__ == "__main__":
    app()
