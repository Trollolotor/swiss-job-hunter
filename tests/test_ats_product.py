"""Ready-library integration: real broker, queue, validation, replay and ingestion.

Only the host launcher is replaced by an in-process SDK bridge in these tests.
"""

import asyncio
import json

import httpx
import pytest

from tests.test_agent_optimization import database as database  # noqa: PLC0414
from tests.test_ats_libraries import fixture


async def sdk_bridge(kind, payload, context, *args, **kwargs):
    """Emulate the existing SDK v2 IPC, including trusted staging and caching."""
    from employers.cache import cached
    from employers.contracts import CatalogueResult, CollectedPosting
    from employers.validation import posting_errors

    loop = asyncio.get_running_loop()

    class SDK:
        def fetch(self, url, method="GET", body=None):
            obs = asyncio.run_coroutine_threadsafe(context.fetch(url, method, body), loop).result()
            context.fresh_detail_keys.add(obs.key)
            return {
                "url": obs.url,
                "text": obs.text,
                "status": obs.status,
                "headers": obs.headers,
                "key": obs.key,
            }

    scope = {}
    exec(payload["code"], scope)  # noqa: S102 -- reviewed static wrappers inside this test bridge only
    config, sdk = payload["config"], SDK()
    catalogue = await asyncio.to_thread(scope["enumerate"], config, sdk)
    parsed = CatalogueResult.model_validate(catalogue)
    context.catalogue_entries = parsed.entries
    for entry in parsed.entries:
        context.allowed(entry.detail_url)
        posting = cached(context, entry)
        if posting is None:
            raw = await asyncio.to_thread(scope["fetch_detail"], config, entry.model_dump(), sdk)
            posting = CollectedPosting.model_validate(
                scope["normalize"](config, entry.model_dump(), raw)
            )
            errors = posting_errors(posting, context.observations, context.manifest)
            if errors:
                raise ValueError("; ".join(errors))
        context.staged.append(posting)
    return {k: v for k, v in catalogue.items() if k != "entries"} | {"postings": []}


def source_row(db, name, *, shadow=True):
    from ats_vendors.registry import network_rules
    from config.runtime import put_setting
    from db.models import Employer, EmployerSource, SearchProfile
    from employers.config import EmployerSettings

    portal, data = fixture(name)
    put_setting(
        "employer_sources",
        EmployerSettings(enabled=True, shadow=shadow, coverage="worldwide").model_dump(),
    )
    with db.get_session() as session:
        employer = Employer(
            name="Acme", identity_key="ats-product-" + name, classification="direct_employer"
        )
        profile = SearchProfile(slug="python", name="Python", cv_text="Python and SQL")
        session.add_all([employer, profile])
        session.flush()
        row = EmployerSource(
            employer_id=employer.id,
            namespace=f"ats:{portal.vendor}:{portal.tenant}:{portal.board}",
            portal_url=portal.portal_url,
            platform=portal.vendor,
            tenant=portal.tenant,
            board=portal.board,
            config_json=json.dumps(portal.config()),
            policy_json=json.dumps(network_rules(portal)),
        )
        session.add(row)
        session.flush()
        return row.id, portal, data


def contexts(portal, data, calls=None):
    from ats_vendors.registry import vendor
    from employers.network import CollectionContext

    def handler(request):
        if calls is not None:
            calls.append((request.method, str(request.url)))
        body = json.loads(request.content) if request.method == "POST" else None
        expected = vendor(portal.vendor).catalogue_request(portal, (body or {}).get("offset", 0))
        if str(request.url) == expected.url and request.method == expected.method:
            payload = data["catalogue"]
        elif "/wday/cxs/" in request.url.path or request.url.path.endswith(".md"):
            payload = data.get("detail", "# Platform Engineer\n\nPython and SQL")
        else:
            return httpx.Response(200, text="<h1>Platform Engineer</h1><p>Acme careers</p>")
        return (
            httpx.Response(200, text=payload)
            if isinstance(payload, str)
            else httpx.Response(200, json=payload)
        )

    def factory(policy, settings):
        return CollectionContext(
            policy,
            settings.model_copy(update={"domain_delay_seconds": 0}),
            transport=httpx.MockTransport(handler),
        )

    return factory


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "workable",
        "successfactors",
        "workday",
        "greenhouse",
        "lever",
        "ashby",
        "recruitee",
        "teamtailor",
    ],
)
async def test_supported_onboarding_uses_ready_library_without_paid_generation(
    database, monkeypatch, tmp_path, name
):
    from db.models import BudgetLedger, ConnectorVersion, EmployerSource, EmployerTask, Job
    from employers import runtime, service, tasks

    sid, portal, data = source_row(database, name)
    monkeypatch.setenv("EMPLOYER_RUNNER_IMAGE", "sha256:" + "b" * 64)
    monkeypatch.setenv("EMPLOYER_ARTIFACT_DIR", str(tmp_path / "evidence"))
    monkeypatch.setattr(runtime, "run_isolated", sdk_bridge)
    monkeypatch.setattr(service, "CollectionContext", contexts(portal, data))

    async def no_generation(*args, **kwargs):
        raise AssertionError("Known vendors must not spend on code generation")

    monkeypatch.setattr(tasks, "_generate", no_generation)
    tid = tasks.enqueue("onboard", source_id=sid)
    await tasks.execute_task(tasks.claim(tid))
    with database.get_session() as session:
        task = session.get(EmployerTask, tid)
        assert task.status == "completed", task.error
        source = session.get(EmployerSource, sid)
        version = session.get(ConnectorVersion, source.active_version_id)
        assert (
            version.status == "active"
            and version.kind == "generated"
            and version.sdk_version == "2"
        )
        report = json.loads(version.validation_json)
        assert report["repeatable"] and report["first"]["outcome"] == "complete"
        assert json.loads(version.manifest_json)["ats_library"]["vendor"] == name
        assert session.query(BudgetLedger).count() == 0
        assert session.query(Job).count() == 0  # Validation/shadow mode does not ingest.


@pytest.mark.asyncio
async def test_unsupported_variant_uses_existing_custom_connector_flow(database, monkeypatch):
    from db.models import EmployerSource
    from employers import tasks

    sid, _, _ = source_row(database, "ashby")
    with database.get_session() as session:
        row = session.get(EmployerSource, sid)
        row.platform = "custom"
    calls = []

    async def generate(task, source, repair, incident):
        calls.append((source["id"], repair))
        return 123

    async def validate(*args, **kwargs):
        pass

    monkeypatch.setattr(tasks, "_generate", generate)
    monkeypatch.setattr(tasks, "validate_activate", validate)
    tid = tasks.enqueue("onboard", source_id=sid)
    await tasks.execute_task(tasks.claim(tid))
    assert calls == [(sid, False)]


@pytest.mark.asyncio
async def test_ready_ingestion_keeps_ids_workflow_and_profile_evaluations(
    database, monkeypatch, tmp_path
):
    from analyzer.matching import local_evaluate
    from db.models import Application, EmployerSource, Job, JobProfile, JobSource, SearchProfile
    from employers import runtime, service
    from employers.ready import ready_candidate

    sid, portal, data = source_row(database, "workable", shadow=False)
    monkeypatch.setenv("EMPLOYER_RUNNER_IMAGE", "sha256:" + "b" * 64)
    monkeypatch.setenv("EMPLOYER_ARTIFACT_DIR", str(tmp_path / "evidence"))
    monkeypatch.setattr(runtime, "run_isolated", sdk_bridge)
    monkeypatch.setattr(service, "CollectionContext", contexts(portal, data))
    with database.get_session() as session:
        source = service.row_dict(session.get(EmployerSource, sid))
    version_id = ready_candidate(source)
    await service.validate_activate(version_id)
    assert (await service.run_version(version_id))["outcome"] == "complete"
    with database.get_session() as session:
        job = session.query(Job).one()
        jid, primary_url = job.id, job.url
        profile = session.query(SearchProfile).one()
        job.status, job.status_origin = "applied", "user"
        session.add(Application(job_id=jid, cover_letter="Tailored letter", notes="Referral"))
        link = local_evaluate(session, job, profile)
        link.match_score, link.score_method, link.evaluation_state = 0.8, "llm", "scored"
        profile_id = profile.id
    assert (await service.run_version(version_id))["outcome"] == "complete"
    with database.get_session() as session:
        job = session.query(Job).one()
        assert job.id == jid and job.url == primary_url and job.status == "applied"
        assert session.query(Application).one().cover_letter == "Tailored letter"
        assert session.query(JobSource).one().application_url.endswith("/apply/")
        assert session.get(JobProfile, (jid, profile_id)).match_score == 0.8


@pytest.mark.asyncio
async def test_cached_details_rebind_to_fresh_catalogue_without_extending_ttl(
    database, monkeypatch, tmp_path
):
    from db.models import DetailCache, EmployerSource
    from employers import runtime, service
    from employers.ready import ready_candidate

    sid, portal, data = source_row(database, "workday", shadow=False)
    monkeypatch.setenv("EMPLOYER_RUNNER_IMAGE", "sha256:" + "b" * 64)
    monkeypatch.setenv("EMPLOYER_ARTIFACT_DIR", str(tmp_path / "evidence"))
    monkeypatch.setattr(runtime, "run_isolated", sdk_bridge)
    calls = []
    monkeypatch.setattr(service, "CollectionContext", contexts(portal, data, calls))
    with database.get_session() as session:
        source = service.row_dict(session.get(EmployerSource, sid))
    vid = ready_candidate(source)
    await service.validate_activate(vid)
    assert (await service.run_version(vid))["outcome"] == "complete"
    with database.get_session() as session:
        cached_at = session.query(DetailCache).one().fetched_at
    # Only an unrelated top-level listing field changes; this record's token is stable.
    data["catalogue"]["diagnosticTimestamp"] = "2026-10-22"
    calls.clear()
    report = await service.run_version(vid)
    assert report["outcome"] == "complete", report
    assert report["cache_hits"] == 1 and len(calls) == 1
    with database.get_session() as session:
        assert session.query(DetailCache).one().fetched_at == cached_at


@pytest.mark.asyncio
async def test_oversized_feed_cannot_close_existing_vacancies(database, monkeypatch, tmp_path):
    from config.runtime import put_setting
    from db.models import EmployerSource, JobSource, SourceRun
    from employers import runtime, service
    from employers.config import get_config
    from employers.ready import ready_candidate

    sid, portal, data = source_row(database, "successfactors", shadow=False)
    monkeypatch.setenv("EMPLOYER_RUNNER_IMAGE", "sha256:" + "b" * 64)
    monkeypatch.setenv("EMPLOYER_ARTIFACT_DIR", str(tmp_path / "evidence"))
    monkeypatch.setattr(runtime, "run_isolated", sdk_bridge)
    monkeypatch.setattr(service, "CollectionContext", contexts(portal, data))
    with database.get_session() as session:
        source = service.row_dict(session.get(EmployerSource, sid))
    vid = ready_candidate(source)
    await service.validate_activate(vid)
    await service.run_version(vid)
    data["catalogue"] = data["catalogue"].replace("Build Python services.", "Big feed " * 200)
    put_setting(
        "employer_sources", get_config().model_copy(update={"response_bytes": 1024}).model_dump()
    )
    with pytest.raises(Exception, match="byte budget"):
        await service.run_version(vid)
    with database.get_session() as session:
        record = session.query(JobSource).one()
        assert record.missing_checks == 0 and record.availability == "open"
        assert session.query(SourceRun).order_by(SourceRun.id.desc()).first().outcome == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [429, 503, "cap"])
async def test_ready_transient_failures_and_source_caps_do_not_start_paid_repair(
    database, monkeypatch, failure
):
    from db.models import (
        BudgetLedger,
        ConnectorIncident,
        ConnectorVersion,
        EmployerSource,
        EmployerTask,
    )
    from employers import service, tasks
    from employers.ready import ready_candidate

    sid, _, _ = source_row(database, "workday")
    monkeypatch.setenv("EMPLOYER_RUNNER_IMAGE", "sha256:" + "b" * 64)
    with database.get_session() as session:
        vid = ready_candidate(service.row_dict(session.get(EmployerSource, sid)))
    with database.get_session() as session:
        session.get(EmployerSource, sid).active_version_id = vid
        session.get(ConnectorVersion, vid).status = "active"

    async def collect(*args, **kwargs):
        if failure == "cap":
            return {"outcome": "partial", "limitation": "catalogue_cap", "errors": ["Query cap"]}
        response = httpx.Response(failure, request=httpx.Request("GET", "https://example.test"))
        response.raise_for_status()

    monkeypatch.setattr(tasks, "run_version", collect)
    tid = tasks.enqueue("collect", source_id=sid)
    await tasks.execute_task(tasks.claim(tid))
    with database.get_session() as session:
        task = session.get(EmployerTask, tid)
        assert task.status == ("needs_review" if failure == "cap" else "queued")
        assert session.query(EmployerTask).filter_by(kind="repair").count() == 0
        assert session.query(ConnectorIncident).count() == 0
        assert session.query(BudgetLedger).count() == 0


@pytest.mark.asyncio
async def test_ready_schema_failure_retains_existing_bounded_llm_repair(database, monkeypatch):
    from db.models import ConnectorIncident, EmployerTask
    from employers import tasks

    sid, _, _ = source_row(database, "workable")

    async def validate(*args, **kwargs):
        raise ValueError("Observed catalogue schema changed")

    monkeypatch.setenv("EMPLOYER_RUNNER_IMAGE", "sha256:" + "b" * 64)
    monkeypatch.setattr(tasks, "validate_activate", validate)
    tid = tasks.enqueue("onboard", source_id=sid)
    await tasks.execute_task(tasks.claim(tid))
    with database.get_session() as session:
        assert session.get(EmployerTask, tid).status == "failed"
        incident = session.query(ConnectorIncident).one()
        assert incident.status == "open" and incident.failed_candidates == 0
        repair = session.query(EmployerTask).filter_by(kind="repair").one()
        repair_id = repair.id
    calls = []

    async def generate(task, source, repair, incident):
        calls.append((source["id"], repair, incident["id"]))
        return 123

    async def accept(*args, **kwargs):
        pass

    monkeypatch.setattr(tasks, "_generate", generate)
    monkeypatch.setattr(tasks, "validate_activate", accept)
    await tasks.execute_task(tasks.claim(repair_id))
    assert calls == [(sid, True, incident.id)]


@pytest.mark.asyncio
async def test_ready_retry_builds_a_new_version_without_replacing_custom_code(
    database, monkeypatch
):
    from db.models import ConnectorVersion, EmployerSource, EmployerTask
    from employers import service, tasks
    from employers.ready import ready_candidate

    sid, _, _ = source_row(database, "ashby")
    monkeypatch.setenv("EMPLOYER_RUNNER_IMAGE", "sha256:" + "b" * 64)
    with database.get_session() as session:
        source = service.row_dict(session.get(EmployerSource, sid))
    vid = ready_candidate(source)
    with database.get_session() as session:
        session.get(EmployerSource, sid).active_version_id = vid
        # A previous library revision is not used to create the new candidate.
        row = session.get(ConnectorVersion, vid)
        manifest = json.loads(row.manifest_json)
        manifest["ats_library"]["fingerprint"] = "previous-library-revision"
        row.manifest_json = json.dumps(manifest)
    candidates = []

    async def validate(version_id, **kwargs):
        candidates.append(version_id)

    monkeypatch.setattr(tasks, "validate_activate", validate)
    tid = tasks.enqueue("revalidate", source_id=sid)
    await tasks.execute_task(tasks.claim(tid))
    with database.get_session() as session:
        assert session.get(EmployerTask, tid).status == "completed"
        version = session.get(ConnectorVersion, candidates[0])
        assert version.id != vid and version.parent_id == vid
        assert (
            json.loads(version.manifest_json)["ats_library"]["fingerprint"]
            != "previous-library-revision"
        )
        assert session.get(EmployerSource, sid).active_version_id == vid  # Promotion is separate.


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["workable", "workday", "successfactors", "successfactors_legacy"])
async def test_official_link_verification_saves_vendor_identity_and_scoped_policy(
    database, monkeypatch, name
):
    from db.models import Employer, EmployerEvidence, EmployerSource
    from employers import service
    from employers.network import CollectionContext

    portal, data = fixture(name)
    with database.get_session() as session:
        employer = Employer(name="Acme", identity_key="verification-" + name)
        session.add(employer)
        session.flush()
        eid = employer.id

    def handler(request):
        if request.url.host == "official.example":
            return httpx.Response(200, text=f'<h1>Acme</h1><a href="{data["portal"]}">Jobs</a>')
        return httpx.Response(200, text=data.get("html", "<h1>Careers</h1>"))

    def factory(policy, settings):
        return CollectionContext(policy, settings, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(service, "CollectionContext", factory)
    sid = await service.verify_and_create(eid, "https://official.example", data["portal"])
    with database.get_session() as session:
        row = session.get(EmployerSource, sid)
        assert (row.platform, row.tenant, row.board) == (portal.vendor, portal.tenant, portal.board)
        config = json.loads(row.config_json)
        assert config["variant"] == portal.variant and config["company"] == "Acme"
        context = factory(json.loads(row.policy_json), service.get_config())
        from ats_vendors.registry import vendor

        request = vendor(portal.vendor).catalogue_request(portal)
        context.allowed(request.url, request.method, request.body)
        await context.close()
        if name == "successfactors":
            assert json.loads(session.query(EmployerEvidence).one().payload_json)["ats_protocol"][
                "observation_hash"
            ]


@pytest.mark.asyncio
async def test_optional_vendor_probe_keeps_existing_custom_portals_available(database, monkeypatch):
    from db.models import Employer, EmployerSource
    from employers import service
    from employers.network import CollectionContext

    with database.get_session() as session:
        employer = Employer(name="Acme", identity_key="custom-probe")
        session.add(employer)
        session.flush()
        eid = employer.id

    def handler(request):
        if request.url.host == "official.example":
            return httpx.Response(
                200, text='<p>Acme</p><a href="https://careers.example/jobs">Jobs</a>'
            )
        return httpx.Response(503, text="Temporary maintenance")

    def factory(policy, settings):
        return CollectionContext(policy, settings, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(service, "CollectionContext", factory)
    sid = await service.verify_and_create(
        eid, "https://official.example", "https://careers.example/jobs"
    )
    with database.get_session() as session:
        assert session.get(EmployerSource, sid).platform == "custom"


@pytest.mark.asyncio
async def test_legacy_successfactors_verification_binds_the_company_query(database, monkeypatch):
    from db.models import Employer
    from employers import service
    from employers.network import CollectionContext

    with database.get_session() as session:
        employer = Employer(name="Acme", identity_key="sf-company-binding")
        session.add(employer)
        session.flush()
        eid = employer.id
    official_link = "https://career2.successfactors.eu/career?company=acme"

    def factory(policy, settings):
        return CollectionContext(
            policy,
            settings,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, text=f'<p>Acme</p><a href="{official_link}">Jobs</a>'
                )
            ),
        )

    monkeypatch.setattr(service, "CollectionContext", factory)
    with pytest.raises(ValueError, match="does not visibly link"):
        await service.verify_and_create(
            eid, "https://official.example", official_link.replace("=acme", "=other")
        )


def test_ready_candidate_cannot_change_the_verified_employer_tenant(database, monkeypatch):
    from dataclasses import replace

    from ats_vendors.registry import manifest, wrapper
    from employers.service import candidate

    sid, portal, _ = source_row(database, "ashby")
    other = replace(portal, tenant="other", portal_url="https://jobs.ashbyhq.com/other")
    monkeypatch.setenv("EMPLOYER_RUNNER_IMAGE", "sha256:" + "b" * 64)
    with pytest.raises(ValueError, match="does not match its source"):
        candidate(sid, code=wrapper("ashby"), manifest=manifest(other), config=other.config())


@pytest.mark.asyncio
async def test_retry_preserves_custom_code_on_a_known_vendor_source(database, monkeypatch):
    from db.models import ConnectorVersion, EmployerSource, EmployerTask
    from employers import ready, service, tasks

    sid, portal, _ = source_row(database, "ashby")
    code = """def enumerate(config, sdk):
    return {"entries": [], "observed_ids": [], "reported_total": 0}
def fetch_detail(config, entry, sdk):
    return {}
def normalize(config, entry, detail):
    return {}
"""
    spec = {
        "catalogue": {
            "format": "json",
            "path_suffix": "/custom",
            "items": "jobs",
            "id": "id",
            "total": "total",
        }
    }
    monkeypatch.setenv("EMPLOYER_RUNNER_IMAGE", "sha256:" + "b" * 64)
    vid = service.candidate(
        sid, code=code, manifest=spec, config=portal.config(), image_digest="sha256:" + "b" * 64
    )
    with database.get_session() as session:
        session.get(EmployerSource, sid).active_version_id = vid

    def no_ready(*args, **kwargs):
        raise AssertionError("Retry must retain the custom connector")

    validated = []

    async def validate(version_id, **kwargs):
        validated.append(version_id)

    monkeypatch.setattr(ready, "ready_candidate", no_ready)
    monkeypatch.setattr(tasks, "validate_activate", validate)
    tid = tasks.enqueue("revalidate", source_id=sid)
    await tasks.execute_task(tasks.claim(tid))
    with database.get_session() as session:
        assert session.get(EmployerTask, tid).status == "completed"
        row = session.get(ConnectorVersion, validated[0])
        assert row.id != vid and row.parent_id == vid and row.code == code
        assert "ats_library" not in json.loads(row.manifest_json)
