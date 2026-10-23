"""Tests for scrapers — uses mocked HTTP responses."""
import json
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest


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

    scraper = JobScout24Scraper(known_jobs={"550e8400-e29b-41d4-a716-446655440000": {
        "title": "Known Engineer", "company": "Known AG", "location": "Bern",
        "description": "already enriched", "url": "https://example.test/known",
    }})
    async def fake_fetch(_url, **kwargs):
        if "/Cities/" in _url:
            return SimpleNamespace(json=lambda: [{"Text": "Bern", "Value": "3000"}])
        return SimpleNamespace(status_code=200, text='<li class="job-list-item" data-job-detail-url="/en/job/550e8400-e29b-41d4-a716-446655440000"></li>')
    async def forbidden_detail(*_args):
        raise AssertionError("known source IDs must not fetch detail pages")
    monkeypatch.setattr(scraper, "_fetch", fake_fetch)
    monkeypatch.setattr(scraper, "_fetch_detail", forbidden_detail)

    jobs = [job async for job in scraper.scrape("engineer", "Bern", 1)]
    assert len(jobs) == 1
    assert jobs[0].source_job_id == "550e8400-e29b-41d4-a716-446655440000"


@pytest.mark.asyncio
async def test_jobscout_uses_verified_postcode_and_real_pagination(monkeypatch):
    from types import SimpleNamespace

    from scrapers.jobscout24 import JobScout24Scraper

    scraper = JobScout24Scraper()
    queries = []
    uuid = "550e8400-e29b-41d4-a716-446655440000"

    async def fetch(url, **kwargs):
        query = parse_qs(urlsplit(url).query)
        if "/Cities/" in url:
            return SimpleNamespace(json=lambda: [
                {"Text": "Egg b. Zürich", "Value": "8132"}, {"Text": "Zurich", "Value": "8000"}])
        queries.append(query)
        return SimpleNamespace(status_code=404 if query.get("p") else 200,
            text=f'<li class="job-list-item" data-job-detail-url="/en/job/{uuid}"></li>')

    monkeypatch.setattr(scraper, "_fetch", fetch)
    detail = AsyncMock(return_value=object())
    monkeypatch.setattr(scraper, "_fetch_detail", detail)
    assert len([j async for j in scraper.scrape("Python engineer", "Zürich", 2)]) == 1
    assert queries == [{"ft": ["Python engineer"], "psz": ["8000"]},
                       {"ft": ["Python engineer"], "psz": ["8000"], "p": ["2"]}]
    assert scraper.report["termination"] == "search_404"


@pytest.mark.asyncio
async def test_jobscout_unknown_city_never_broadens_the_search(monkeypatch):
    from types import SimpleNamespace

    from scrapers.jobscout24 import JobScout24Scraper

    scraper = JobScout24Scraper()
    fetch = AsyncMock(return_value=SimpleNamespace(json=lambda: [{"Text": "Another city", "Value": "1234"}]))
    monkeypatch.setattr(scraper, "_fetch", fetch)
    with pytest.raises(ValueError, match="Cannot resolve"):
        _ = [j async for j in scraper.scrape("Python", "Unknown town", 1)]
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_michael_page_sends_search_and_zero_based_pages(monkeypatch):
    from types import SimpleNamespace

    from scrapers.michael_page import MichaelPageScraper

    scraper = MichaelPageScraper()
    urls = []

    async def fetch(url, **kwargs):
        urls.append(url)
        return SimpleNamespace(status_code=404 if len(urls) > 1 else 200,
            text='<div class="view-content">'+''.join(
                f'<div class="views-row"><h3><a href="/job/{i}">Engineer {i}</a></h3></div>'
                for i in range(10))+'</div>')

    monkeypatch.setattr(scraper, "_fetch", fetch)
    assert len([j async for j in scraper.scrape("Python", "Bern", 2)]) == 10
    assert parse_qs(urlsplit(urls[0]).query) == {"search": ["Python"], "location": ["Bern"]}
    assert parse_qs(urlsplit(urls[1]).query)["page"] == ["1"]


@pytest.mark.parametrize("board", ["jobs", "jobup"])
def test_jobcloud_search_joins_structured_metadata_and_visible_cards(board):
    from scrapers.jobs_ch import JobsChScraper
    from scrapers.jobup_ch import JobupChScraper

    scraper = JobsChScraper() if board == "jobs" else JobupChScraper()
    uuid = "550e8400-e29b-41d4-a716-446655440000"
    item = {"@type": "JobPosting", "title": "Python engineer", "identifier": {"value": uuid},
            "hiringOrganization": {"name": "Acme"}, "datePosted": "2026-10-20T10:00:00+02:00",
            "description": "We are looking for Python engineer to join our team."}
    html = '<script type="application/ld+json">'+json.dumps(
        {"@type": "ItemList", "itemListElement": [{"item": item}]})+'</script>'
    html += f'<div data-cy="serp-item"><a href="/detail/{uuid}/">Role</a>'
    html += '<span>Place of work:</span><p>Bern</p><span>Workload:</span><p>80 - 100%</p></div>'
    job = scraper._parse_search_page(html)[0]
    assert (job.location, job.employment_type, job.description) == ("Bern", "80–100%", "")
    assert job.url.startswith(scraper.detail_url)
    assert job.posted_at_source == "json_ld" and job.posted_at_confidence == .95
    assert job.extra["description_quality"] == "preview"
    with pytest.raises(ValueError):
        scraper._parse_search_page("<h1>Changed markup</h1>")


@pytest.mark.asyncio
async def test_transient_http_failures_retry_but_expired_details_are_inspectable(monkeypatch):
    from scrapers.jobs_ch import JobsChScraper

    statuses = iter([429, 503, 200, 410])
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(next(statuses))

    async with JobsChScraper() as scraper:
        scraper._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(scraper, "_polite_delay", AsyncMock())
        monkeypatch.setattr("scrapers.base.asyncio.sleep", AsyncMock())
        assert (await scraper._fetch("https://www.jobs.ch/test")).status_code == 200
        assert await scraper.fetch_full_description("one") == ()
    assert len(calls) == 4
