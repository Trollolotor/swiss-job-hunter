"""Employer-source acceptance tests; no production data, live HTTP, or LLM calls."""

import asyncio
import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

DESCRIPTION = "Build reliable financial software with Python and maintain production services. " * 3


@pytest.fixture
def feature(monkeypatch, tmp_path):
    from config.runtime import put_setting
    from db.session import init_db
    from employers.config import EmployerSettings

    init_db()
    monkeypatch.setenv("EMPLOYER_ARTIFACT_DIR", str(tmp_path / "evidence"))
    cfg = EmployerSettings(enabled=True, shadow=False, coverage="worldwide")
    put_setting("employer_sources", cfg.model_dump())
    yield cfg
    put_setting("employer_sources", EmployerSettings().model_dump())


def scraped(pid="one", **kwargs):
    from scrapers.base import ScrapedJob

    data = dict(
        title="Software Engineer",
        company="Evidence Bank AG",
        location="Zurich",
        description=DESCRIPTION,
        url=f"https://board.example/job/{pid}",
        source="test-board",
        source_job_id=pid,
    )
    return ScrapedJob(**{**data, **kwargs})


def source():
    from db.models import Employer, EmployerSource
    from db.session import get_session

    with get_session() as s:
        e = Employer(
            name="Evidence Bank AG",
            identity_key=uuid4().hex,
            classification="direct_employer",
            resolution_status="resolved",
        )
        s.add(e)
        s.flush()
        row = EmployerSource(
            employer_id=e.id,
            namespace="ats:test:" + uuid4().hex,
            portal_url="https://careers.smartrecruiters.com/EvidenceBank/all-jobs",
            platform="smartrecruiters",
            tenant="EvidenceBank",
            config_json=json.dumps({"tenant": "EvidenceBank"}),
            policy_json=json.dumps(
                [
                    {"host": "api.smartrecruiters.com", "path": "/"},
                    {"host": "jobs.smartrecruiters.com", "path": "/"},
                ]
            ),
        )
        s.add(row)
        s.flush()
        return row.id


def context_factory(ids):
    from employers.network import CollectionContext

    def handler(request):
        if request.url.host == "jobs.smartrecruiters.com":
            return httpx.Response(200, text=f"<h1>Software Engineer</h1><p>{DESCRIPTION}</p>")
        if request.url.path.endswith("/postings"):
            offset = int(request.url.params.get("offset", 0))
            content = [{"id": p, "name": "Software Engineer"} for p in ids[offset : offset + 100]]
            return httpx.Response(200, json={"totalFound": len(ids), "content": content})
        pid = request.url.path.split("/")[-1]
        return httpx.Response(
            200,
            json={
                "id": pid,
                "name": "Software Engineer",
                "company": {"name": "Evidence Bank AG"},
                "language": {"code": "en"},
                "location": {"country": "ch", "city": "Zurich", "fullLocation": "Zurich"},
                "jobAd": {"sections": {"jobDescription": {"text": DESCRIPTION}}},
                "postingUrl": f"https://jobs.smartrecruiters.com/EvidenceBank/{pid}",
                "applyUrl": f"https://jobs.smartrecruiters.com/EvidenceBank/{pid}",
            },
        )

    def factory(policy, settings):
        settings = settings.model_copy(update={"domain_delay_seconds": 0})
        return CollectionContext(policy, settings, transport=httpx.MockTransport(handler))

    return factory


def test_discovery_before_rejection_and_names_stay_separate(feature):
    from db.models import Employer
    from db.session import get_session
    from employers.discovery import observe_board_result

    label = "Name test " + uuid4().hex
    observe_board_result(scraped(company=label + " AG", posted_at=datetime(2000, 1, 1)))
    observe_board_result(scraped(company=label + " Holding"))
    with get_session() as s:
        assert s.query(Employer).filter(Employer.name.like(label + "%")).count() == 2


def test_source_identity_preserves_distinct_requisitions(feature):
    from dedup.exact import get_or_create_job

    first, _ = get_or_create_job(scraped(uuid4().hex))
    second, _ = get_or_create_job(scraped(uuid4().hex))
    again, created = get_or_create_job(scraped(first.source_job_id))
    assert first.id != second.id
    assert again.id == first.id and not created


def test_disabling_mode_preserves_new_source_job_identity(feature):
    from config.runtime import put_setting
    from dedup.exact import get_or_create_job
    from employers.config import EmployerSettings

    item = scraped(uuid4().hex)
    first, _ = get_or_create_job(item)
    put_setting("employer_sources", EmployerSettings().model_dump())
    again, created = get_or_create_job(item)
    assert again.id == first.id and not created


def test_legacy_source_identity_preserves_job_id(feature):
    from db.models import Job
    from db.session import get_session
    from dedup.exact import get_or_create_job

    pid = uuid4().hex
    item = scraped(pid)
    with get_session() as s:
        row = Job(
            dedup_hash=uuid4().hex,
            title=item.title,
            company=item.company,
            location=item.location,
            description=item.description,
            url=item.url,
            source=item.source,
            source_job_id=pid,
        )
        s.add(row)
        s.flush()
        old_id = row.id
    new, created = get_or_create_job(item)
    assert new.id == old_id and not created


def test_strong_cross_source_link_and_user_history_preserved(feature):
    from db.models import (
        Application,
        ApplicationEvent,
        EmployerSource,
        JobEvent,
        JobSourceRevision,
        JobStatus,
    )
    from db.session import get_session
    from employers.contracts import CollectedPosting
    from employers.ingestion import ingest

    sid = source()
    with get_session() as s:
        direct = s.get(EmployerSource, sid)
        board = scraped(uuid4().hex, extra={"requisition_id": "REQ1"})
        job, _, _ = ingest(s, board, employer_id=direct.employer_id)
        job.status = JobStatus.APPLIED
        job.user_stars = 5
        s.add(Application(job_id=job.id, notes="Preserve me"))
        s.add(JobEvent(job_id=job.id, event_type=ApplicationEvent.NOTE, note="My decision"))
        direct_job = scraped(
            "direct-one",
            description=DESCRIPTION + " Richer requirements.",
            source="employer:bank",
            url="https://employer.example/jobs/1",
        )
        p = CollectedPosting(job=direct_job, posting_id="direct-one", requisition_id="REQ1")
        linked, created, record = ingest(s, direct_job, source=direct, posting=p)
        assert linked.id == job.id and not created
        assert linked.status == JobStatus.APPLIED and linked.user_stars == 5
        assert linked.source == board.source and linked.url == board.url
        assert linked.application.notes == "Preserve me"
        p.job.description += " Another important change."
        ingest(s, p.job, source=direct, posting=p)
        assert record.score_stale
        assert s.query(JobSourceRevision).filter_by(job_source_id=record.id).count() == 2


@pytest.mark.asyncio
async def test_validation_activation_replay_collection_and_closure(feature, monkeypatch):
    from db.models import EmployerSource, Job, JobSource
    from db.session import get_session
    from employers.service import candidate, run_version, validate_activate
    from llm import router

    monkeypatch.setattr(
        router, "call_llm", AsyncMock(side_effect=AssertionError("Collection used LLM"))
    )
    sid = source()
    vid = candidate(sid)
    await validate_activate(vid, context_factory=context_factory(["id-1", "id-2"]))
    with get_session() as s:
        assert s.get(EmployerSource, sid).active_version_id == vid
    result = await run_version(vid, context_factory=context_factory(["id-1", "id-2"]))
    assert result["outcome"] == "complete"
    with get_session() as s:
        rows = s.query(JobSource).filter_by(source_id=sid).all()
        assert len(rows) == 2
        assert all(s.get(Job, r.job_id).posted_at is None for r in rows)
    assert (await run_version(vid, context_factory=context_factory([])))["outcome"] == "empty"
    with get_session() as s:
        assert all(r.availability == "open" for r in s.query(JobSource).filter_by(source_id=sid))
    await run_version(vid, context_factory=context_factory([]))
    with get_session() as s:
        assert all(r.availability == "open" for r in s.query(JobSource).filter_by(source_id=sid))
        for row in s.query(JobSource).filter_by(source_id=sid):
            row.checked_at = datetime.utcnow() - timedelta(hours=3)
    await run_version(vid, context_factory=context_factory([]))
    with get_session() as s:
        assert all(r.availability == "closed" for r in s.query(JobSource).filter_by(source_id=sid))
        assert all(
            s.get(Job, r.job_id).status.value == "new"
            for r in s.query(JobSource).filter_by(source_id=sid)
        )


@pytest.mark.asyncio
async def test_failed_validation_stays_inactive(feature):
    from db.models import ConnectorVersion, EmployerSource
    from db.session import get_session
    from employers.service import candidate, validate_activate

    sid = source()
    vid = candidate(sid)
    factory = context_factory(["one"])

    def broken(policy, settings):
        ctx = factory(policy, settings)
        original = ctx.fetch

        async def fetch(*args, **kwargs):
            obs = await original(*args, **kwargs)
            if obs.url.endswith("/one"):
                obs.text = '{"name":"Software Engineer","jobAd":{}}'
            return obs

        ctx.fetch = fetch
        return ctx

    with pytest.raises(ValueError):
        await validate_activate(vid, context_factory=broken)
    with get_session() as s:
        assert s.get(EmployerSource, sid).active_version_id is None
        assert s.get(ConnectorVersion, vid).status == "rejected"


@pytest.mark.asyncio
async def test_explicit_version_validation_preserves_repair_audit_and_cancels_queued_generation(
    feature, monkeypatch
):
    from fastapi.testclient import TestClient
    from db.models import ConnectorIncident, EmployerSource, EmployerTask
    from db.session import get_session
    from employers import service, tasks
    from employers.api import source_action
    import server

    sid = source()
    vid = service.candidate(sid)
    with get_session() as session:
        incident = ConnectorIncident(source_id=sid, category="structural", failed_candidates=1)
        session.add(incident)
        session.flush()
        iid = incident.id
    previous = tasks.enqueue("repair", source_id=sid)
    response = TestClient(server.app).post(f"/employer-sources/{sid}/versions/{vid}/validate")
    assert response.status_code == 200
    with get_session() as session:
        assert session.get(EmployerTask, previous).status == "cancelled"
        assert session.get(ConnectorIncident, iid).failed_candidates == 1
    monkeypatch.setattr(service, "CollectionContext", context_factory(["one"]))
    await tasks.execute_task(tasks.claim(response.json()["task_id"]))
    with get_session() as session:
        assert session.get(EmployerSource, sid).active_version_id == vid
        assert session.get(ConnectorIncident, iid).status == "resolved"
        assert session.get(ConnectorIncident, iid).failed_candidates == 1
    source_action(sid, "pause")


@pytest.mark.asyncio
async def test_partial_does_not_advance_absence(feature):
    from db.models import JobSource
    from db.session import get_session
    from employers.service import candidate, run_version, validate_activate

    sid = source()
    vid = candidate(sid)
    await validate_activate(vid, context_factory=context_factory(["one"]))
    await run_version(vid, context_factory=context_factory(["one"]))
    import employers.service as svc

    original = svc.execute

    async def truncated(*args):
        result = await original(*args)
        result.termination = "incomplete"
        return result

    old = svc.execute
    svc.execute = truncated
    try:
        result = await run_version(vid, context_factory=context_factory([]))
        assert result["outcome"] == "failed"
    finally:
        svc.execute = old
    with get_session() as s:
        row = s.query(JobSource).filter_by(source_id=sid).one()
        assert row.availability == "open" and row.missing_checks == 0


@pytest.mark.asyncio
async def test_absence_membership_distinguishes_variants_of_same_posting(feature):
    from db.models import Job, JobSource
    from db.session import get_session
    from employers.service import candidate, run_version, validate_activate

    sid = source()
    vid = candidate(sid)
    factory = context_factory(["one"])
    await validate_activate(vid, context_factory=factory)
    await run_version(vid, context_factory=factory)
    with get_session() as session:
        observed = session.query(JobSource).filter_by(source_id=sid).one()
        # A previously collected language variant shares its requisition, but
        # its continued existence must be independently observed.
        missing = JobSource(
            source_id=sid,
            namespace=observed.namespace,
            posting_id="one",
            variant="de",
            job_id=observed.job_id,
            url=observed.url,
            availability="open",
        )
        session.add(missing)
        session.flush()
        missing_id, observed_id = missing.id, observed.id
    await run_version(vid, context_factory=factory)
    with get_session() as session:
        assert session.get(JobSource, missing_id).missing_checks == 1
        session.get(JobSource, missing_id).checked_at = datetime.utcnow() - timedelta(hours=3)
    await run_version(vid, context_factory=factory)
    with get_session() as session:
        assert session.get(JobSource, missing_id).availability == "closed"
        present = session.get(JobSource, observed_id)
        assert present.availability == "open" and present.missing_checks == 0
        assert session.get(Job, present.job_id).expired_at is None


def test_location_modes_and_unknown_dates(feature):
    from employers.contracts import CollectedPosting, Location, in_coverage

    p = CollectedPosting(
        job=scraped(), posting_id="one", locations=[Location(country="US"), Location(country="CH")]
    )
    assert in_coverage(p, "swiss")
    p.locations = [Location(country="US")]
    p.remote_countries = ["CH"]
    assert not in_coverage(p, "swiss") and in_coverage(p, "swiss_remote")
    p.remote_countries = []
    assert not in_coverage(p, "swiss_remote") and in_coverage(p, "worldwide")
    assert p.job.posted_at is None


@pytest.mark.asyncio
async def test_redirect_to_internal_and_method_denied(feature):
    from employers.network import CollectionContext, PolicyError

    cfg = feature.model_copy(update={"domain_delay_seconds": 0})

    def handler(r):
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

    ctx = CollectionContext(
        [{"host": "public.example", "path": "/"}], cfg, transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(PolicyError):
            await ctx.fetch("https://public.example/jobs")
        with pytest.raises(PolicyError):
            await ctx.fetch("https://public.example/apply", "POST", {"email": "x"})
        assert ctx.requests == 1
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_dns_rebinding_pinned_backend_rejects_private(feature, monkeypatch):
    from employers.network import PinnedBackend, PolicyError

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "getaddrinfo", AsyncMock(return_value=[(2, 1, 6, "", ("127.0.0.1", 443))])
    )
    with pytest.raises(PolicyError):
        await PinnedBackend().connect_tcp("public.example", 443)


@pytest.mark.asyncio
async def test_response_size_limit(feature):
    from employers.network import CollectionContext, CollectionLimit

    cfg = feature.model_copy(update={"domain_delay_seconds": 0, "response_bytes": 10})
    ctx = CollectionContext(
        [{"host": "public.example", "path": "/"}],
        cfg,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="x" * 100)),
    )
    try:
        with pytest.raises(CollectionLimit):
            await ctx.fetch("https://public.example/jobs")
    finally:
        await ctx.close()


def test_budget_unset_and_exhaustion_persist(feature):
    from config.runtime import put_setting
    from employers.budget import BudgetUnavailable, reserve
    from employers.config import get_config

    with pytest.raises(BudgetUnavailable):
        reserve("connector_generation", "test", "hello", 100)
    cfg = get_config().model_dump()
    cfg.update(
        daily_budget_usd=0.01,
        max_task_usd=0.01,
        prices={"budget-test": {"input_per_million": 0.1, "output_per_million": 0.1}},
    )
    put_setting("employer_sources", cfg)
    reserve("connector_generation", "budget-test", "x" * 10000, 10000)
    with pytest.raises(BudgetUnavailable):
        reserve("connector_generation", "budget-test", "x" * 10000, 10000)


def test_fencing_prevents_reclaimed_worker_from_finalizing(feature):
    from db.models import EmployerTask
    from db.session import get_session
    from employers.tasks import StaleTaskError, _finish, claim, enqueue, fenced

    sid = source()
    tid = enqueue("collect", source_id=sid)
    old = claim(tid)
    with get_session() as s:
        s.get(EmployerTask, tid).lease_until = datetime.utcnow() - timedelta(seconds=1)
    new = claim(tid)
    with pytest.raises(StaleTaskError):
        fenced(old)
    _finish(old, "completed")
    fenced(new)


@pytest.mark.asyncio
async def test_repair_limit_survives_scheduler_and_restart(feature):
    from db.models import ConnectorIncident, EmployerTask
    from db.session import get_session
    from employers.tasks import claim, enqueue, execute_task

    sid = source()
    with get_session() as s:
        row = ConnectorIncident(source_id=sid, category="schema", failed_candidates=2)
        s.add(row)
        s.flush()
        iid = row.id
    tid = enqueue("repair", source_id=sid)
    await execute_task(claim(tid))
    with get_session() as s:
        assert s.get(ConnectorIncident, iid).failed_candidates == 2
        assert s.get(ConnectorIncident, iid).status == "needs_review"
        assert s.get(EmployerTask, tid).status == "needs_review"
    from employers.api import source_action

    # Pause/resume must not act as an implicit manual retry.
    source_action(sid, "pause")
    resumed = source_action(sid, "resume")
    await execute_task(claim(resumed["task_id"]))
    with get_session() as s:
        assert s.get(ConnectorIncident, iid).failed_candidates == 2
        assert s.get(ConnectorIncident, iid).status == "needs_review"
    source_action(sid, "retry")
    with get_session() as s:
        assert s.get(ConnectorIncident, iid).failed_candidates == 2
        assert s.get(ConnectorIncident, iid).status == "manually_retried"
    source_action(sid, "pause")


@pytest.mark.asyncio
async def test_fenced_task_cannot_make_another_network_request(feature):
    from employers.network import CollectionContext
    from employers.tasks import StaleTaskError
    from unittest.mock import Mock

    network = Mock(return_value=httpx.Response(200, text="public"))
    context = CollectionContext(
        [{"host": "public.example", "path": "/"}], feature, transport=httpx.MockTransport(network)
    )
    context.fence = Mock(side_effect=StaleTaskError("reclaimed"))
    try:
        with pytest.raises(StaleTaskError):
            await context.fetch("https://public.example/jobs")
        network.assert_not_called()
        assert context.requests == 0
    finally:
        await context.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/%61pply", "/jobs%2faccount", "/%2561pplication", "/%73ubmit"])
async def test_encoded_account_actions_are_denied(feature, path):
    from employers.network import CollectionContext, PolicyError
    from unittest.mock import Mock

    network = Mock(return_value=httpx.Response(200, text="not reached"))
    context = CollectionContext(
        [{"host": "public.example", "path": "/"}], feature, transport=httpx.MockTransport(network)
    )
    try:
        with pytest.raises(PolicyError):
            await context.fetch("https://public.example" + path)
        network.assert_not_called()
    finally:
        await context.close()


def test_launcher_uses_fixed_policy_without_host_mounts():
    spec = importlib.util.spec_from_file_location("launcher", "deploy/connector-launcher.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    args = mod.command("python", "sha256:" + "a" * 64, "test")
    assert "none" in args and "--read-only" in args
    assert "--privileged" not in args and "--volume" not in args and "-v" not in args
    assert "--network" in args and "--cap-drop" in args and "--pids-limit" in args
    with pytest.raises(ValueError):
        mod.command("shell", "latest", "test")
    with pytest.raises(ValueError):
        mod.command("python", "latest", "test")


@pytest.mark.parametrize("versioned", [False, True])
def test_migration_existing_and_empty_database(tmp_path, monkeypatch, versioned):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    from config.settings import settings

    p = tmp_path / "migration.db"
    db = create_engine(f"sqlite:///{p}")
    if versioned:
        # Simulate unversioned startup-created tables, preserving a legacy job ID.
        from db.models import Base, Job

        Base.metadata.create_all(db, tables=[Job.__table__])
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{p}")
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    assert "employer_tasks" in inspect(db).get_table_names()
    with db.connect() as c:
        assert (
            c.execute(text("SELECT version_num FROM alembic_version")).scalar()
            == "20261021_agent_optimization"
        )


@pytest.mark.parametrize("previous_head", [False, True])
def test_migration_preserves_legacy_history_and_adds_stale_flag(
    tmp_path, monkeypatch, previous_head
):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    from config.settings import settings
    from db.models import (
        Application,
        ApplicationEvent,
        Base,
        Job,
        JobEvent,
        JobProfile,
        JobStatus,
        SearchProfile,
    )

    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    db = create_engine(url)
    names = {
        "jobs",
        "raw_jobs",
        "search_profiles",
        "job_profiles",
        "app_settings",
        "llm_call_logs",
        "llm_cache",
        "prompt_revisions",
        "automation_runs",
        "automation_lease",
        "applications",
        "company_info",
        "job_events",
    }
    Base.metadata.create_all(db, tables=[t for t in Base.metadata.sorted_tables if t.name in names])
    with db.begin() as conn:
        conn.execute(
            Job.__table__.insert().values(
                id=17,
                dedup_hash="legacy-history",
                title="Engineer",
                company="Legacy Bank",
                location="Bern",
                description=DESCRIPTION,
                url="https://legacy.example/job",
                source="legacy-board",
                status=JobStatus.APPLIED,
                user_stars=5,
            )
        )
        conn.execute(
            SearchProfile.__table__.insert().values(id=19, slug="legacy-history", name="Legacy")
        )
        conn.execute(
            JobProfile.__table__.insert().values(job_id=17, profile_id=19, match_score=0.9)
        )
        conn.execute(
            Application.__table__.insert().values(job_id=17, notes="Keep this application")
        )
        conn.execute(
            JobEvent.__table__.insert().values(
                job_id=17, event_type=ApplicationEvent.NOTE, note="Keep this event"
            )
        )
        for column in (
            "description_quality",
            "extraction_json",
            "enrichment_attempts",
            "enrichment_due_at",
            "status_origin",
        ):
            conn.exec_driver_sql(f"ALTER TABLE jobs DROP COLUMN {column}")
        for column in (
            "keyword_score",
            "score_method",
            "evaluation_state",
            "cv_hash",
            "content_hash",
            "prompt_revision",
            "requirements_json",
        ):
            conn.exec_driver_sql(f"ALTER TABLE job_profiles DROP COLUMN {column}")
        conn.exec_driver_sql("ALTER TABLE job_profiles DROP COLUMN score_stale")
    monkeypatch.setattr(settings, "database_url", url)
    cfg = Config("alembic.ini")
    if previous_head:
        command.upgrade(cfg, "20260622_intelligence")
    command.upgrade(cfg, "head")
    assert "score_stale" in {c["name"] for c in inspect(db).get_columns("job_profiles")}
    with db.connect() as conn:
        assert conn.execute(text("SELECT id,user_stars,status FROM jobs")).one() == (
            17,
            5,
            "APPLIED",
        )
        assert (
            conn.execute(text("SELECT notes FROM applications")).scalar() == "Keep this application"
        )
        assert conn.execute(text("SELECT note FROM job_events")).scalar() == "Keep this event"
        assert conn.execute(text("SELECT match_score,score_stale FROM job_profiles")).one() == (
            0.9,
            0,
        )


def test_settings_api_exposes_all_coverage_modes_and_unset_budget(feature):
    from fastapi.testclient import TestClient

    import server

    client = TestClient(server.app)
    body = client.get("/settings/employer-sources").json()
    assert body["daily_budget_usd"] is None
    for mode in ["swiss", "swiss_remote", "worldwide"]:
        clean = {
            k: v
            for k, v in body.items()
            if k not in {"schema_ready", "runner_configured", "launcher_available"}
        }
        clean.update(enabled=False, coverage=mode)
        assert client.put("/settings/employer-sources", json=clean).status_code == 200


@pytest.mark.asyncio
async def test_generated_onboarding_collection_and_validated_repair(feature, monkeypatch):
    """Mock paid generation/runner only; exercise real queue, validation, replay and promotion."""
    from db.models import ConnectorIncident, ConnectorVersion, EmployerSource, EmployerTask
    from db.session import get_session
    from employers import service, tasks
    from employers.adapters import SMARTRECRUITERS_MANIFEST, smartrecruiters
    from employers.agent import AgentStep

    sid = source()
    with get_session() as s:
        row = s.get(EmployerSource, sid)
        row.platform = "custom"
        policy = json.loads(row.policy_json)
        policy.append({"host": "careers.smartrecruiters.com", "path": "/"})
        row.policy_json = json.dumps(policy)
    initial = "def collect(config, sdk):\n    return {}\n"
    repair = initial + "# repaired candidate\n"
    state = {"broken": False, "calls": []}

    async def generate(operation, evidence, *args, **kwargs):
        state["calls"].append(operation)
        if operation == "connector_investigation":
            if len(evidence["observations"]) == 1:
                return AgentStep(
                    action="fetch",
                    url="https://api.smartrecruiters.com/v1/companies/EvidenceBank/postings/one",
                )
            return AgentStep(action="generate")
        if operation == "connector_repair":
            assert evidence["previous"]["code"] == initial
            assert evidence["last_success"] and evidence["current_failure"]
        return AgentStep(
            action="candidate",
            code=repair if operation == "connector_repair" else initial,
            manifest=SMARTRECRUITERS_MANIFEST,
            config={"tenant": "EvidenceBank"},
        )

    async def runner(version, source, context):
        result = await smartrecruiters({"tenant": "EvidenceBank"}, context)
        if state["broken"] and version.code == initial:
            result.termination = "incomplete"
        return result

    monkeypatch.setattr(tasks, "step", generate)
    monkeypatch.setattr(tasks, "CollectionContext", context_factory(["one"]))
    monkeypatch.setattr(service, "CollectionContext", context_factory(["one"]))
    monkeypatch.setattr(service, "execute", runner)
    monkeypatch.setenv("EMPLOYER_RUNNER_IMAGE", "sha256:" + "b" * 64)
    tid = tasks.enqueue("onboard", source_id=sid)
    await tasks.execute_task(tasks.claim(tid))
    with get_session() as s:
        assert s.get(EmployerTask, tid).status == "completed"
        first = s.get(EmployerSource, sid).active_version_id
        assert first
    calls = len(state["calls"])
    tid = tasks.enqueue("collect", source_id=sid)
    await tasks.execute_task(tasks.claim(tid))
    assert len(state["calls"]) == calls
    state["broken"] = True
    tid = tasks.enqueue("collect", source_id=sid)
    await tasks.execute_task(tasks.claim(tid))
    with get_session() as s:
        repair_task = (
            s.query(EmployerTask).filter_by(source_id=sid, kind="repair", status="queued").one()
        )
        tid = repair_task.id
    await tasks.execute_task(tasks.claim(tid))
    with get_session() as s:
        assert s.get(EmployerTask, tid).status == "completed"
        current = s.get(EmployerSource, sid).active_version_id
        assert current != first
        assert s.get(ConnectorVersion, current).status == "active"
        assert s.get(ConnectorVersion, current).parent_id == first
        incident = s.query(ConnectorIncident).filter_by(source_id=sid).one()
        assert incident.status == "resolved" and incident.failed_candidates == 1


def test_fabricated_description_and_duplicate_pages_fail_validation(feature):
    from employers.contracts import CollectedPosting, CollectionResult
    from employers.network import Observation
    from employers.validation import assess

    obs = Observation(
        "https://public.example/jobs?offset=0",
        "GET",
        200,
        json.dumps(
            {
                "total": 2,
                "jobs": [{"id": "one", "title": "Software Engineer", "description": DESCRIPTION}],
            }
        ),
        {},
    )
    p = CollectedPosting(
        job=scraped(
            description="Completely fabricated requirements that are not in the source. " * 3
        ),
        posting_id="one",
        evidence=[obs.key],
    )
    result = CollectionResult(
        postings=[p], observed_ids=["one"], reported_total=2, termination="total"
    )
    report = assess(
        result,
        [obs, obs],
        {
            "catalogue": {
                "format": "json",
                "path_suffix": "/jobs",
                "items": "jobs",
                "id": "id",
                "total": "total",
                "pagination": "offset",
            }
        },
    )
    assert report["outcome"] == "partial"
    assert any("Repeated" in e for e in report["errors"])
    assert any("description" in e for e in report["errors"])


@pytest.mark.parametrize(
    "host,totals,expected",
    [
        ("tenant.wd103.myworkdayjobs.com", [3, 0, 0], "complete"),
        ("public.example", [3, 0, 0], "partial"),
        ("tenant.wd103.myworkdayjobs.com", [3, 2, 0], "partial"),
        ("tenant.wd103.myworkdayjobs.com", [0, 0, 0], "partial"),
        ("tenant.wd103.myworkdayjobs.com", [3, False, 0], "partial"),
    ],
)
def test_workday_initial_total_still_reconciles_all_pages(feature, host, totals, expected):
    from employers.contracts import CollectedPosting, CollectionResult
    from employers.network import Observation
    from employers.validation import assess

    observations, postings = [], []
    for offset, total in enumerate(totals):
        pid = f"id-{offset}"
        url = f"https://{host}/job/{pid}"
        observation = Observation(
            f"https://{host}/wday/cxs/tenant/Board/jobs",
            "POST",
            200,
            json.dumps(
                {
                    "total": total,
                    "jobPostings": [
                        {
                            "id": pid,
                            "url": url,
                            "title": "Software Engineer",
                            "description": DESCRIPTION,
                        }
                    ],
                }
            ),
            {},
            {"offset": offset, "limit": 1},
        )
        observations.append(observation)
        postings.append(
            CollectedPosting(job=scraped(pid, url=url), posting_id=pid, evidence=[observation.key])
        )
    report = assess(
        CollectionResult(
            postings=postings,
            observed_ids=[p.posting_id for p in postings],
            reported_total=3,
            termination="total",
        ),
        observations,
        {
            "catalogue": {
                "format": "json",
                "path_suffix": "/jobs",
                "items": "jobPostings",
                "id": "id",
                "total": "total",
                "pagination": "offset",
            }
        },
    )
    assert report["outcome"] == expected


def test_publication_dates_require_source_evidence(feature):
    from employers.contracts import CollectedPosting
    from employers.network import Observation
    from employers.validation import posting_errors

    observation = Observation(
        "https://board.example/job/one",
        "GET",
        200,
        json.dumps(
            {
                "title": "Software Engineer",
                "description": DESCRIPTION,
                "updatedAt": "2026-10-21T00:00:00Z",
            }
        ),
        {},
    )
    posting = CollectedPosting(
        job=scraped(posted_at=datetime(2026, 10, 21)), posting_id="one", evidence=[observation.key]
    )
    assert any("Publication date" in error for error in posting_errors(posting, [observation]))
    observation.text = observation.text.replace("updatedAt", "datePosted")
    posting.evidence = [observation.key]
    assert not posting_errors(posting, [observation])
    posting.posting_id = "fabricated-id-112358"
    assert any("Posting identity" in error for error in posting_errors(posting, [observation]))


@pytest.mark.asyncio
async def test_initial_onboarding_failure_uses_persisted_repair_limit(feature, monkeypatch):
    from db.models import ConnectorIncident, EmployerSource, EmployerTask
    from db.session import get_session
    from employers import service, tasks
    from employers.adapters import SMARTRECRUITERS_MANIFEST, smartrecruiters
    from employers.agent import AgentStep

    sid = source()
    with get_session() as session:
        row = session.get(EmployerSource, sid)
        row.platform = "custom"
        row.policy_json = json.dumps(
            json.loads(row.policy_json) + [{"host": "careers.smartrecruiters.com", "path": "/"}]
        )
    calls = []

    async def generate(operation, evidence, *args, **kwargs):
        if operation == "connector_investigation":
            if len(evidence["observations"]) == 1:
                return AgentStep(
                    action="fetch",
                    url="https://api.smartrecruiters.com/v1/companies/EvidenceBank/postings/one",
                )
            return AgentStep(action="generate")
        calls.append(operation)
        return AgentStep(
            action="candidate",
            code="def collect(config, sdk):\n    return {}\n",
            manifest=SMARTRECRUITERS_MANIFEST,
            config={"tenant": "EvidenceBank"},
        )

    async def broken(version, source, context):
        result = await smartrecruiters({"tenant": "EvidenceBank"}, context)
        result.termination = "incomplete"
        return result

    monkeypatch.setattr(tasks, "step", generate)
    monkeypatch.setattr(tasks, "CollectionContext", context_factory(["one"]))
    monkeypatch.setattr(service, "CollectionContext", context_factory(["one"]))
    monkeypatch.setattr(service, "execute", broken)
    monkeypatch.setenv("EMPLOYER_RUNNER_IMAGE", "sha256:" + "b" * 64)
    tid = tasks.enqueue("onboard", source_id=sid)
    await tasks.execute_task(tasks.claim(tid))
    for _ in range(2):
        with get_session() as session:
            tid = (
                session.query(EmployerTask)
                .filter_by(source_id=sid, kind="repair", status="queued")
                .one()
                .id
            )
        await tasks.execute_task(tasks.claim(tid))
    # A later onboarding request must not reset the incident's durable counter.
    tid = tasks.enqueue("onboard", source_id=sid)
    await tasks.execute_task(tasks.claim(tid))
    with get_session() as session:
        incident = session.query(ConnectorIncident).filter_by(source_id=sid).one()
        assert incident.failed_candidates == 2 and incident.status == "needs_review"
        assert session.get(EmployerSource, sid).health == "blocked"
        assert session.get(EmployerTask, tid).status == "needs_review"
    assert calls == ["connector_generation", "connector_repair", "connector_repair"]


def test_automated_scoring_preserves_manual_statuses(feature):
    from db.models import JobStatus
    from job_lifecycle import set_scored_status

    for status in (
        JobStatus.APPLIED,
        JobStatus.VIEWED,
        JobStatus.CONSIDERING,
        JobStatus.REJECTED,
        JobStatus.ARCHIVED,
    ):
        job = type("Job", (), {"status": status})()
        set_scored_status(job, JobStatus.SHORTLISTED)
        assert job.status == status


def test_html_empty_requires_observed_visible_empty_state(feature):
    from employers.contracts import CollectionResult
    from employers.network import Observation
    from employers.validation import assess

    manifest = {
        "catalogue": {
            "format": "html",
            "path_suffix": "/jobs",
            "items": ".job",
            "next": "a.next",
            "empty": ".empty",
        }
    }
    result = CollectionResult(termination="explicit_empty", reported_total=0)
    good = Observation(
        "https://public.example/jobs", "GET", 200, '<p class="empty">No open positions</p>', {}
    )
    hidden = Observation(
        "https://public.example/jobs",
        "GET",
        200,
        '<p class="empty" hidden>No open positions</p>',
        {},
    )
    assert assess(result, [good], manifest)["outcome"] == "empty"
    assert assess(result, [hidden], manifest)["outcome"] == "failed"


def test_changed_requirements_invalidate_and_refresh_profile_scores(feature):
    from db.models import EmployerSource, JobProfile, SearchProfile
    from db.session import get_session
    from employers.contracts import CollectedPosting
    from employers.ingestion import ingest, mark_scores_fresh

    sid = source()
    with get_session() as s:
        profile = SearchProfile(name="Revision test", slug="revision-" + uuid4().hex)
        s.add(profile)
        s.flush()
        src = s.get(EmployerSource, sid)
        item = scraped(uuid4().hex)
        posting = CollectedPosting(job=item, posting_id=item.source_job_id)
        job, _, record = ingest(s, item, source=src, posting=posting, profile_ids=[profile.id])
        link = s.get(JobProfile, (job.id, profile.id))
        link.match_score = 0.95
        link.score_stale = False
        posting.job.description += " Substantially different requirements."
        ingest(s, posting.job, source=src, posting=posting)
        s.refresh(link)
        assert link.score_stale and link.match_score == 0.95
        mark_scores_fresh(s, job.id, profile.id)
        s.refresh(record)
        assert not link.score_stale and not record.score_stale
