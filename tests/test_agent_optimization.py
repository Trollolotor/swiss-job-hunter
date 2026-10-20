"""Behavior checks for matching, extraction recovery, ingestion and spending."""

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def database(tmp_path, monkeypatch):
    import db.session as db
    from config.runtime import _settings_cache
    from db.models import Base

    engine = create_engine(
        f"sqlite:///{tmp_path / 'optimization.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(
        db, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    )
    monkeypatch.setattr(db, "_initialized", True)
    import employers.budget as employer_budget

    monkeypatch.setattr(employer_budget, "engine", engine)
    _settings_cache.clear()
    yield db
    _settings_cache.clear()
    engine.dispose()


def seed(db, quality="complete"):
    from db.models import Job, SearchProfile

    with db.get_session() as session:
        p = SearchProfile(slug="python", name="Python CV", cv_text="Python PostgreSQL Linux Docker")
        q = SearchProfile(
            slug="accounting", name="Accounting CV", cv_text="Accounting audit payroll IFRS"
        )
        job = Job(
            dedup_hash="one",
            title="Python engineer",
            company="Acme",
            location="Bern",
            description="Develop Python services and PostgreSQL databases.",
            description_quality=quality,
            source="jobs.ch",
            source_job_id="one",
            url="https://www.jobs.ch/en/vacancies/detail/one/",
        )
        session.add_all([p, q, job])
        session.flush()
        return job.id, p.id, q.id


def test_historical_rows_without_source_identity_do_not_block_backfill(database):
    from db.models import Employer, Job, JobSource
    from employers.discovery import backfill

    with database.get_session() as session:
        row = Job(
            dedup_hash="legacy-no-url",
            title="Legacy role",
            company="Historical employer",
            location="Bern",
            description="Preserved",
            source="legacy",
            url="",
        )
        session.add(row)
        session.flush()
        jid = row.id
    assert backfill()["done"]
    assert backfill()["processed"] == 0
    with database.get_session() as session:
        assert session.get(Job, jid).description == "Preserved"
        assert session.query(Employer).filter_by(name="Historical employer").count() == 1
        assert session.query(JobSource).count() == 0


@pytest.mark.asyncio
async def test_root_official_candidate_is_followed_to_verified_career_portal(database, monkeypatch):
    from db.models import Employer, EmployerSource
    from employers import resolution, service
    from employers.config import EmployerSettings
    from employers.network import CollectionContext

    with database.get_session() as session:
        employer = Employer(name="Acme", identity_key="acme")
        session.add(employer)
        session.flush()
        eid = employer.id

    def handler(request):
        if request.url.path == "/":
            return httpx.Response(
                200,
                text='<a href="#top">Top</a><a href="mailto:info@example.com">Email</a><p>Acme</p><a href="/career.html">Careers</a>',
            )
        return httpx.Response(
            200, text='<p>Acme</p><a href="https://careers.smartrecruiters.com/Acme">Jobs</a>'
        )

    def context(policy, settings):
        return CollectionContext(
            policy,
            EmployerSettings(domain_delay_seconds=2).model_copy(update={"domain_delay_seconds": 0}),
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(resolution, "CollectionContext", context)
    monkeypatch.setattr(service, "CollectionContext", context)
    sid = await resolution.from_official_links(
        {"employer_id": eid},
        {"observations": [], "candidate_official_urls": ["https://official.example"]},
        lambda task: None,
    )
    with database.get_session() as session:
        assert session.get(EmployerSource, sid).tenant == "Acme"
        assert session.get(Employer, eid).resolution_status == "resolved"


@pytest.mark.asyncio
async def test_investigation_primes_real_listing_and_detail_through_policy(database):
    from employers.config import EmployerSettings
    from employers.investigation import prime
    from employers.network import CollectionContext

    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(
                200, json={"total": 1, "jobPostings": [{"externalPath": "/job/Bern/Engineer_R-1"}]}
            )
        return httpx.Response(
            200, json={"jobPostingInfo": {"jobDescription": "Maintain services."}}
        )

    context = CollectionContext(
        [
            {"host": "tenant.example", "path": "/", "methods": ["GET"]},
            {
                "host": "tenant.example",
                "path": "/wday/cxs/tenant/Board/jobs",
                "methods": ["POST"],
                "body_keys": ["appliedFacets", "limit", "offset", "searchText"],
            },
        ],
        EmployerSettings().model_copy(update={"domain_delay_seconds": 0}),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await prime(
            {
                "platform": "workday",
                "portal_url": "https://tenant.example/en-US/Board",
                "tenant": "tenant",
                "board": "Board",
            },
            context,
        )
        assert seen == [
            ("POST", "/wday/cxs/tenant/Board/jobs"),
            ("GET", "/wday/cxs/tenant/Board/job/Bern/Engineer_R-1"),
        ]
        assert result[1]["key"] == context.observations[1].key
        assert "jobDescription" in result[1]["text"]
    finally:
        await context.close()


def test_repair_samples_preserve_structured_details_after_public_link_probes():
    from employers.investigation import samples
    from employers.network import Observation

    catalogue = Observation("https://public.example/jobs", "POST", 200, '{"total":1}', {})
    detail = Observation(
        "https://public.example/api/job/one",
        "GET",
        200,
        '{"jobPostingInfo":{"jobDescription":"Complete"}}',
        {},
    )
    probe = Observation(
        "https://public.example/job/one", "GET", 200, "<html>Client app shell</html>", {}
    )
    assert samples([catalogue, detail, probe]) == [catalogue, detail]


@pytest.mark.asyncio
async def test_unknown_agency_resolution_cannot_bypass_exclusion_via_official_link(
    database, monkeypatch
):
    from db.models import Employer, EmployerEvidence, EmployerSource
    from employers import tasks
    from employers.agent import AgentStep

    with database.get_session() as session:
        employer = Employer(name="Staffing firm", identity_key="agency")
        session.add(employer)
        session.flush()
        eid = employer.id
        session.add(
            EmployerEvidence(
                employer_id=eid,
                evidence_key="agency-observation",
                url="https://board.example/job/agency-role",
                kind="board",
                source="board",
                payload_json=json.dumps({"official_url": "https://agency.example"}),
            )
        )
    monkeypatch.setattr(
        tasks,
        "step",
        AsyncMock(
            return_value=AgentStep(
                action="resolved",
                classification="agency",
                official_url="https://agency.example",
                portal_url="https://careers.smartrecruiters.com/Agency",
            )
        ),
    )
    task = tasks.claim(tasks.enqueue("resolve", employer_id=eid))
    await tasks.execute_task(task)
    with database.get_session() as session:
        assert session.get(Employer, eid).classification == "agency"
        assert session.query(EmployerSource).count() == 0


@pytest.mark.asyncio
async def test_local_cv_coverage_is_profile_specific_and_never_paid(database, monkeypatch):
    from analyzer.matching import evaluate
    from db.models import Job, JobProfile

    jid, p, q = seed(database)
    monkeypatch.setattr(
        "analyzer.scorer._extract_keywords_llm", AsyncMock(side_effect=AssertionError("paid"))
    )
    one = await evaluate(jid, p)
    two = await evaluate(jid, q)
    assert 0 < one["keyword_score"] < 1 and two["keyword_score"] == 0
    with database.get_session() as s:
        assert s.get(JobProfile, (jid, p)).screened_at is None
        assert s.get(JobProfile, (jid, p)).score_method == "keyword"
        assert s.get(Job, jid).status.value == "new"


@pytest.mark.asyncio
async def test_pending_budget_state_and_contributing_profile_remain_visible(database):
    from analyzer.matching import aggregate, evaluate
    from db.models import Job, JobProfile
    from llm.spending import SpendingUnavailable

    jid, p, _ = seed(database)
    with pytest.raises(SpendingUnavailable):
        await evaluate(jid, p, llm=True)
    with database.get_session() as session:
        result = aggregate(session.get(Job, jid), [session.get(JobProfile, (jid, p))])
        assert result["match_score"] is None
        assert result["evaluation_state"] == "budget_wait"
        assert result["matching_profile_id"] == p


@pytest.mark.asyncio
async def test_employer_agent_requests_json_and_retains_context_for_paid_and_cached_calls(
    database, monkeypatch
):
    from config.runtime import put_setting
    from db.models import BudgetLedger
    from employers import agent
    from employers.config import EmployerSettings

    put_setting(
        "employer_sources",
        EmployerSettings(
            daily_budget_usd=1,
            prices={"test/agent": {"input_per_million": 1, "output_per_million": 1}},
        ).model_dump(),
    )
    put_setting("llm", {"default_model": "test/agent", "retries": 0})

    async def create(**kwargs):
        assert kwargs["response_format"] == {"type": "json_object"}
        return SimpleNamespace(
            id="request",
            usage=SimpleNamespace(model_dump=lambda: {"cost": 0.001}),
            choices=[
                SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content='{"action":"candidate"}')
                )
            ],
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client.with_options = lambda **kwargs: client
    monkeypatch.setattr(agent, "_client", lambda: client)
    monkeypatch.setattr(agent, "_cache_get", lambda key: None)
    monkeypatch.setattr(agent, "_cache_put", lambda *args: None)
    evidence = {"source": {"id": 77}, "incident": {"id": 88}}
    assert (await agent.step("connector_repair", evidence)).action == "candidate"
    monkeypatch.setattr(agent, "_cache_get", lambda key: '{"action":"candidate"}')
    await agent.step("connector_repair", evidence)
    with database.get_session() as session:
        rows = session.query(BudgetLedger).order_by(BudgetLedger.id).all()
        assert [row.actual_usd for row in rows] == [0.001, 0]
        assert all(json.loads(row.metadata_json)["source_id"] == 77 for row in rows)
        assert all(json.loads(row.metadata_json)["incident_id"] == 88 for row in rows)


@pytest.mark.asyncio
async def test_cv_keyword_cache_changes_with_database_text(database, monkeypatch):
    from analyzer.scorer import load_cv_keywords

    monkeypatch.setattr(
        "analyzer.scorer._extract_keywords_llm", AsyncMock(side_effect=AssertionError("paid"))
    )
    first = await load_cv_keywords("Python Linux", direction="same")
    second = await load_cv_keywords("Payroll Accounting", direction="same")
    assert {p[2] for p in first} != {p[2] for p in second}


@pytest.mark.asyncio
@pytest.mark.parametrize("quality", ["preview", "failed", "insufficient", "unknown"])
async def test_incomplete_descriptions_are_deferred_not_rejected(database, monkeypatch, quality):
    from analyzer.matching import evaluate
    from db.models import Job, JobProfile

    jid, p, _ = seed(database, quality)
    forbidden = AsyncMock(side_effect=AssertionError("Incomplete description must not be screened"))
    monkeypatch.setattr("analyzer.scorer.llm_score", forbidden)
    result = await evaluate(jid, p, llm=True)
    assert result["state"] == "deferred"
    with database.get_session() as s:
        assert s.get(Job, jid).status.value == "new"
        assert s.get(JobProfile, (jid, p)).match_score is None
    forbidden.assert_not_awaited()


def test_short_complete_description_and_long_preview(database):
    from analyzer.quality import detail_is_complete, quality

    text = "Maintain Python services."
    html = (
        '<script type="application/ld+json">'
        + json.dumps({"@type": "JobPosting", "description": text})
        + "</script>"
    )
    assert detail_is_complete(html, text)
    assert quality(text, trusted_detail=True) == "complete"
    assert quality("Long teaser " * 300, declared="preview") == "preview"
    assert not detail_is_complete("<main>Cookie settings " * 500, "Cookie settings")


@pytest.mark.asyncio
async def test_provisional_scores_receive_llm_and_no_hidden_shortlist_cap(database, monkeypatch):
    from analyzer.matching import evaluate
    from analyzer.scorer import MatchResult
    from db.models import Job, JobProfile

    jid, p, _ = seed(database)
    await evaluate(jid, p)
    mock = AsyncMock(return_value=MatchResult(0.25, [], [], "Limited fit", "test"))
    monkeypatch.setattr("analyzer.scorer.llm_score", mock)
    await evaluate(jid, p, llm=True, shortlist=0.3)
    with database.get_session() as s:
        assert s.get(JobProfile, (jid, p)).score_method == "llm"
        assert s.get(Job, jid).status.value == "analyzed"
    mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_other_profile_fit_and_manual_skip_survive_rescoring(database, monkeypatch):
    from analyzer.matching import evaluate
    from analyzer.scorer import MatchResult
    from db.models import Job, JobStatusChange
    from job_lifecycle import mark_unavailable, transition

    jid, p, q = seed(database)
    mock = AsyncMock(
        side_effect=[
            MatchResult(0.9, [], [], "Fit"),
            MatchResult(0.01, [], [], "Poor fit"),
            MatchResult(0.95, [], [], "Fit"),
        ]
    )
    monkeypatch.setattr("analyzer.scorer.llm_score", mock)
    await evaluate(jid, p, llm=True)
    await evaluate(jid, q, llm=True)
    with database.get_session() as s:
        job = s.get(Job, jid)
        assert job.status.value == "shortlisted"
        transition(job, "archived", origin="user", reason="Prefer another industry")
        mark_unavailable(job, "404")
        mark_unavailable(job, "404")
    await evaluate(jid, p, llm=True, force=True)
    with database.get_session() as s:
        assert s.get(Job, jid).status_origin == "user"
        assert s.get(Job, jid).status.value == "archived"
        assert s.query(JobStatusChange).filter_by(reason="Prefer another industry").count() == 1


@pytest.mark.asyncio
async def test_stale_async_result_is_discarded(database, monkeypatch):
    from analyzer.matching import evaluate
    from analyzer.scorer import MatchResult
    from db.models import JobProfile, SearchProfile

    jid, p, _ = seed(database)

    async def slow(*args, **kwargs):
        with database.get_session() as s:
            s.get(SearchProfile, p).cv_text = "A different CV"
        return MatchResult(0.9, [], [], "Old inputs")

    monkeypatch.setattr("analyzer.scorer.llm_score", slow)
    assert (await evaluate(jid, p, llm=True))["state"] == "stale_result"
    with database.get_session() as s:
        assert s.get(JobProfile, (jid, p)).match_score is None


def test_profile_validation_and_all_profile_aggregation(database):
    from analyzer.matching import evaluate
    from db.models import JobProfile

    jid, p, q = seed(database)
    client = TestClient(__import__("server").app)
    assert (
        client.post("/run/analyze", json={"profile_id": p, "direction": "accounting"}).status_code
        == 422
    )
    for unknown in [0, -1, 99999]:
        assert client.post("/run/analyze", json={"profile_id": unknown}).status_code == 422
        assert client.get(f"/jobs?profile_id={unknown}").status_code == 422
        assert client.get(f"/stats?profile_id={unknown}").status_code == 422
    asyncio.run(evaluate(jid, p))
    asyncio.run(evaluate(jid, q))
    with database.get_session() as s:
        for pid, score in ((p, 0.8), (q, 0.1)):
            link = s.get(JobProfile, (jid, pid))
            link.match_score, link.score_method, link.score_stale = score, "llm", False
    response = client.get("/jobs").json()[0]
    assert response["match_score"] == 0.8 and response["matching_profile_id"] == p
    selected = client.get(f"/jobs?profile_id={q}").json()[0]
    assert selected["match_score"] == 0.1
    assert client.get(f"/stats?profile_id={q}&threshold=.3").json()["above_threshold"] == 0


def test_purge_waits_for_every_active_cv_evaluation(database):
    from analyzer.matching import input_hash
    from db.models import Job, JobProfile, SearchProfile
    from employers.contracts import digest

    jid, p, q = seed(database)
    with database.get_session() as session:
        job = session.get(Job, jid)
        job.status, job.status_origin, job.match_score = "archived", "scoring", 0.01
        session.add(
            JobProfile(
                job_id=jid,
                profile_id=p,
                score_method="llm",
                evaluation_state="scored",
                match_score=0.01,
                score_stale=False,
                cv_hash=digest(session.get(SearchProfile, p).cv_text),
                content_hash=input_hash(job),
            )
        )
    client = TestClient(__import__("server").app)
    result = client.post("/run/purge-archived", json={"max_score": 0.1, "dry_run": False})
    assert "0 jobs" in result.text
    with database.get_session() as session:
        job = session.get(Job, jid)
        assert job is not None
        session.add(
            JobProfile(
                job_id=jid,
                profile_id=q,
                score_method="llm",
                evaluation_state="scored",
                match_score=0.01,
                score_stale=False,
                cv_hash=digest(session.get(SearchProfile, q).cv_text),
                content_hash=input_hash(job),
            )
        )
    assert (
        "1 jobs"
        in client.post("/run/purge-archived", json={"max_score": 0.1, "dry_run": True}).text
    )


@pytest.mark.asyncio
async def test_board_capture_before_filter_and_partial_persistence(database):
    from config.runtime import put_setting
    from db.models import BoardSearchRun, Employer, Job
    from employers.config import EmployerSettings
    from scrapers.base import BaseScraper, ScrapedJob
    from scrapers.collection import collect_board

    put_setting("employer_sources", EmployerSettings(enabled=True).model_dump())

    class Board(BaseScraper):
        source_name = "fixture"

        async def scrape(self, keyword, location, max_pages):
            yield ScrapedJob(
                "Old role",
                "Old Employer AG",
                "Bern",
                "",
                "https://example.test/old",
                "fixture",
                source_job_id="old",
                posted_at=datetime(2000, 1, 1),
            )
            yield ScrapedJob(
                "New role",
                "New Employer AG",
                "Bern",
                "",
                "https://example.test/new",
                "fixture",
                source_job_id="new",
            )
            raise ValueError("Second page schema changed")

    scraper = Board()
    found = []
    with pytest.raises(ValueError):
        async for _, job, _ in collect_board(scraper, "role", "Bern", 2, max_age_days=14):
            found.append(job.id)
    with database.get_session() as s:
        assert s.query(Employer).count() == 2
        assert s.query(Job).count() == 1
        assert s.get(Job, found[0]).title == "New role"
        assert s.query(BoardSearchRun).one().outcome == "partial"


@pytest.mark.asyncio
async def test_jobs_search_parameters_and_explicit_empty(database, monkeypatch):
    from db.models import BoardSearchRun
    from scrapers.collection import collect_board
    from scrapers.jobs_ch import JobsChScraper

    scraper = JobsChScraper()
    urls = []

    async def fetch(url, **kwargs):
        urls.append(url)
        return SimpleNamespace(
            text='<script type="application/ld+json">{"@type":"ItemList","itemListElement":[]}</script>'
        )

    monkeypatch.setattr(scraper, "_fetch", fetch)
    assert [j async for j in collect_board(scraper, "nonsense", "Bern", 1)] == []
    assert "term=nonsense" in urls[-1] and "location=Bern" in urls[-1]
    with database.get_session() as s:
        assert s.query(BoardSearchRun).one().outcome == "empty"
    with pytest.raises(ValueError):
        scraper._parse_search_page("<h1>Access challenge</h1>")


@pytest.mark.asyncio
async def test_permanent_http_failure_is_not_retried(database, monkeypatch):
    from scrapers.base import PermanentHTTPError
    from scrapers.jobs_ch import JobsChScraper

    scraper = JobsChScraper()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403)

    scraper._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(scraper, "_polite_delay", AsyncMock())
    with pytest.raises(PermanentHTTPError):
        await scraper._fetch("https://www.jobs.ch/test")
    assert len(calls) == 1
    await scraper.close()


def configure_spending():
    from analyzer.config import MatchingSettings
    from config.runtime import put_setting

    put_setting(
        "matching",
        MatchingSettings(
            daily_budget_usd=0.02,
            max_attempt_usd=1,
            prices={"test/model": {"input_per_million": 1, "output_per_million": 1}},
        ).model_dump(),
    )


def test_matching_budget_is_separate_and_concurrent_reservations_are_atomic(database):
    from concurrent.futures import ThreadPoolExecutor

    from db.models import BudgetLedger
    from llm.spending import SpendingUnavailable, reserve

    with pytest.raises(SpendingUnavailable):
        reserve("job_screening", "test/model", "hello", 100)
    configure_spending()

    def attempt():
        try:
            return reserve("job_screening", "test/model", "hello", 100)
        except SpendingUnavailable:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(r is not None for r in results) == 1
    with database.get_session() as s:
        row = s.query(BudgetLedger).one()
        assert row.category == "matching"


def test_legacy_unknown_archive_is_protected_but_auto_archive_recovers(database):
    from db.models import Job
    from job_lifecycle import transition

    jid, _, _ = seed(database)
    with database.get_session() as s:
        job = s.get(Job, jid)
        job.status, job.status_origin = "archived", "legacy_unknown"
        assert not transition(job, "new", origin="scoring")
        job.status_origin = "scoring"
        assert transition(job, "shortlisted", origin="scoring")


def test_read_filters_do_not_change_processing_settings(database):
    from analyzer.config import get_config

    client = TestClient(__import__("server").app)
    seed(database)
    before = get_config().model_dump()
    assert client.get("/jobs?min_score=.9&direct_only=true&review_queue=true").status_code == 200
    assert get_config().model_dump() == before
    assert (
        client.put(
            "/settings/matching",
            json={**before, "archive_threshold": 0.9, "shortlist_threshold": 0.3},
        ).status_code
        == 422
    )


@pytest.mark.asyncio
async def test_ignored_search_filter_is_reported_before_ingestion(database, monkeypatch):
    from db.models import BoardSearchRun, Job
    from scrapers.collection import collect_board
    from scrapers.jobs_ch import JobsChScraper

    scraper = JobsChScraper()
    bad = (
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@type": "ItemList",
                "itemListElement": [
                    {
                        "item": {
                            "@type": "JobPosting",
                            "title": "Unrelated role",
                            "identifier": {"value": "one"},
                            "url": "https://www.jobs.ch/en/vacancies/detail/one/",
                            "hiringOrganization": {"name": "Unrelated"},
                        }
                    }
                ],
            }
        )
        + "</script>"
    )
    monkeypatch.setattr(scraper, "_fetch", AsyncMock(return_value=SimpleNamespace(text=bad)))
    with pytest.raises(ValueError, match="negative control"):
        async for _ in collect_board(scraper, "intune", "Bern", 1):
            raise AssertionError("Ignored filters must never ingest a vacancy")
    with database.get_session() as session:
        assert session.query(Job).count() == 0
        assert session.query(BoardSearchRun).one().outcome == "failed"


@pytest.mark.asyncio
async def test_sdk_v2_cache_refresh_and_validation_bypass(database, tmp_path, monkeypatch):
    from config.runtime import put_setting
    from db.models import ConnectorVersion, DetailCache
    from employers.config import EmployerSettings
    from employers.service import candidate, run_version, validate_activate
    from tests.test_employer_sources import context_factory, source

    put_setting(
        "employer_sources",
        EmployerSettings(enabled=True, shadow=False, coverage="worldwide").model_dump(),
    )
    monkeypatch.setenv("EMPLOYER_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    sid = source()
    vid = candidate(sid)
    factory = context_factory(["one", "two"])
    await validate_activate(vid, context_factory=factory)
    first = await run_version(vid, context_factory=factory)
    from employers.metrics import source_metrics

    with database.get_session() as session:
        assert source_metrics(session)["sources"][0]["observed_after_baseline"] == 0
    second = await run_version(vid, context_factory=factory)
    assert first["outcome"] == second["outcome"] == "complete"
    assert first["requests"] == 3 and second["requests"] == 1 and second["cache_hits"] == 2
    with database.get_session() as s:
        assert s.get(ConnectorVersion, vid).sdk_version == "2"
        for cache in s.query(DetailCache):
            cache.fetched_at = datetime.utcnow() - timedelta(hours=25)
    expired = await run_version(vid, context_factory=factory)
    assert expired["requests"] == 3 and expired["cache_hits"] == 0
    validation = await run_version(vid, validation=True, context_factory=factory)
    assert validation["cache_hits"] == 0 and validation["requests"] > 3


@pytest.mark.asyncio
async def test_detail_failure_keeps_earlier_sightings_and_never_closes(
    database, tmp_path, monkeypatch
):
    from config.runtime import put_setting
    from db.models import JobSource, SourceRun, SourceRunMember
    from employers.config import EmployerSettings
    from employers.service import candidate, run_version, validate_activate
    from tests.test_employer_sources import context_factory, source

    put_setting(
        "employer_sources",
        EmployerSettings(enabled=True, shadow=False, coverage="worldwide").model_dump(),
    )
    monkeypatch.setenv("EMPLOYER_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    sid = source()
    vid = candidate(sid)
    await validate_activate(vid, context_factory=context_factory(["one", "two"]))
    factory = context_factory(["one", "two"])

    def broken(policy, settings):
        ctx = factory(policy, settings)
        fetch = ctx.fetch

        async def fetch_page(url, *args, **kwargs):
            if url.endswith("/two"):
                raise TimeoutError("Detail endpoint timed out")
            return await fetch(url, *args, **kwargs)

        ctx.fetch = fetch_page
        return ctx

    with pytest.raises(TimeoutError):
        await run_version(vid, context_factory=broken)
    with database.get_session() as s:
        rows = s.query(JobSource).filter_by(source_id=sid).all()
        assert len(rows) == 1 and rows[0].posting_id == "one" and rows[0].availability == "open"
        run = s.query(SourceRun).order_by(SourceRun.id.desc()).first()
        assert run.outcome == "partial" and json.loads(run.stats_json)["observed_count"] == 2
        assert s.query(SourceRunMember).filter_by(run_id=run.id).count() == 2


@pytest.mark.asyncio
async def test_structured_json_repair_is_billed_as_another_matching_attempt(database, monkeypatch):
    from pydantic import BaseModel

    from analyzer.config import MatchingSettings
    from config.runtime import put_setting
    from config.settings import settings
    from db.models import BudgetLedger
    from llm.structured import call_structured

    put_setting(
        "matching",
        MatchingSettings(
            daily_budget_usd=1,
            prices={"test/model": {"input_per_million": 1, "output_per_million": 1}},
        ).model_dump(),
    )
    put_setting(
        "llm",
        {
            "default_model": "test/model",
            "retries": 0,
            "global_fallbacks": [],
            "operations": {"job_screening": {"reasoning_effort": "none"}},
        },
    )
    monkeypatch.setattr(settings, "openrouter_api_key", "test")
    answers = iter(["not json", '{"value":1}'])

    async def create(**kwargs):
        assert kwargs["extra_body"] == {"reasoning": {"effort": "none"}}
        return SimpleNamespace(
            id="request-id",
            usage=SimpleNamespace(
                model_dump=lambda: {"cost": 0.001, "prompt_tokens": 10, "completion_tokens": 10}
            ),
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=next(answers)), finish_reason="stop"
                )
            ],
        )

    monkeypatch.setattr(
        "llm.task_router._client",
        lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    monkeypatch.setattr("llm.task_router._cache_get", lambda key: None)
    monkeypatch.setattr("llm.task_router._cache_put", lambda *args: None)

    class Payload(BaseModel):
        value: int

    result, _ = await call_structured(
        user="Evaluate", system="JSON", max_tokens=100, operation="job_screening", schema=Payload
    )
    assert result.value == 1
    with database.get_session() as s:
        ledger = s.query(BudgetLedger).filter_by(category="matching").all()
        assert len(ledger) == 2 and sum(r.actual_usd for r in ledger) == 0.002


@pytest.mark.asyncio
async def test_fallback_failure_retains_reservation(database, monkeypatch):
    from analyzer.config import MatchingSettings
    from config.runtime import put_setting
    from db.models import BudgetLedger
    from llm.task_router import call_task_llm

    prices = {
        model: {"input_per_million": 1, "output_per_million": 1} for model in ("first", "second")
    }
    put_setting("matching", MatchingSettings(daily_budget_usd=1, prices=prices).model_dump())
    put_setting("llm", {"default_model": "first", "global_fallbacks": ["second"], "retries": 0})
    monkeypatch.setattr("llm.task_router.settings.openrouter_api_key", "test")

    async def create(**kwargs):
        if kwargs["model"] == "first":
            raise TimeoutError("Unknown billing result")
        return SimpleNamespace(
            id="second",
            usage=SimpleNamespace(model_dump=lambda: {"cost": 0.001}),
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
        )

    monkeypatch.setattr(
        "llm.task_router._client",
        lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    monkeypatch.setattr("llm.task_router._cache_get", lambda key: None)
    monkeypatch.setattr("llm.task_router._cache_put", lambda *args: None)
    answer, model = await call_task_llm(
        user="CV", system="Screen", max_tokens=100, operation="job_screening"
    )
    assert (answer, model) == ("ok", "second")
    with database.get_session() as s:
        rows = s.query(BudgetLedger).order_by(BudgetLedger.id).all()
        assert (
            rows[0].actual_usd is None and rows[0].reserved_usd > 0 and rows[0].status == "failed"
        )
        assert rows[1].actual_usd == 0.001
