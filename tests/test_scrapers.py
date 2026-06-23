"""Tests for scrapers — uses mocked HTTP responses."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_jobs_ch_scraper_parse():
    from scrapers.jobs_ch import JobsChScraper

    # Fields match actual jobs.ch API: flat strings, UUID slug, employment_grades
    doc = {
        "job_id": "12345",
        "title": "Senior ML Engineer",
        "company_name": "Acme AG",
        "place": "Zürich",
        "regions": [],
        "preview": "Join our AI team in Zürich.",
        "slug": "550e8400-e29b-41d4-a716-446655440000-senior-ml-engineer-acme",
        "publication_date": "2025-01-15T08:00:00Z",
        "employment_grades": [80, 100],
    }

    scraper = JobsChScraper()
    job = scraper._parse_document(doc)

    assert job is not None
    assert job.title == "Senior ML Engineer"
    assert job.company == "Acme AG"
    assert "Zürich" in job.location
    assert job.employment_type == "80–100%"
    assert job.source_job_id == "12345"


@pytest.mark.asyncio
async def test_jobup_ch_scraper_parse():
    from scrapers.jobup_ch import JobupChScraper

    doc = {
        "id": "99",
        "title": "Data Scientist",
        "company": {"name": "Swiss Bank"},
        "place": {"name": "Genève"},
        "teaser": "Exciting data science role",
        "slug": "data-scientist-swiss-bank",
        "publication_date": "2025-02-01T09:00:00Z",
    }

    scraper = JobupChScraper()
    job = scraper._parse(doc)

    assert job is not None
    assert job.title == "Data Scientist"
    assert job.company == "Swiss Bank"
    assert job.source == "jobup.ch"


@pytest.mark.asyncio
async def test_jobscout_reuses_known_source_id_without_detail_fetch(monkeypatch):
    from types import SimpleNamespace
    from scrapers.jobscout24 import JobScout24Scraper

    scraper = JobScout24Scraper(known_jobs={"known-id": {
        "title": "Known Engineer", "company": "Known AG", "location": "Bern",
        "description": "already enriched", "url": "https://example.test/known",
    }})
    async def fake_fetch(_url):
        return SimpleNamespace(text='<a href="/en/job/known-id">Known</a>')
    async def forbidden_detail(*_args):
        raise AssertionError("known source IDs must not fetch detail pages")
    monkeypatch.setattr(scraper, "_fetch", fake_fetch)
    monkeypatch.setattr(scraper, "_fetch_detail", forbidden_detail)

    jobs = [job async for job in scraper.scrape("engineer", "Bern", 1)]
    assert len(jobs) == 1
    assert jobs[0].source_job_id == "known-id"
