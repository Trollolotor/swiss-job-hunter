"""Employer verification, version validation, trusted ingestion, and lifecycle."""

import ast
import json
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from sqlalchemy import select, update

from db.models import (
    ConnectorIncident,
    ConnectorVersion,
    Employer,
    EmployerEvidence,
    EmployerSource,
    Job,
    JobProfile,
    JobSource,
    SearchProfile,
    SourceRun,
    SourceRunMember,
)
from db.session import get_session
from employers import artifacts
from employers.adapters import ADAPTERS, SMARTRECRUITERS_MANIFEST, adapter_fingerprint
from employers.config import get_config
from employers.contracts import SDK_VERSION, digest, in_coverage
from employers.ingestion import ingest
from employers.network import CollectionContext, public_url
from employers.runtime import collect_generated
from employers.validation import assess, posting_errors, repeatable


def row_dict(row):
    result = {}
    for c in row.__table__.columns:
        v = getattr(row, c.name)
        if isinstance(v, datetime):
            v = v.isoformat()
        if c.name.endswith("_json"):
            try:
                v = json.loads(v or "{}")
            except ValueError:
                v = {}
            result[c.name[:-5]] = v
        else:
            result[c.name] = v
    return result


def detect_platform(url):
    p = public_url(url)
    parts = [v for v in p.path.split("/") if v]
    if p.hostname == "careers.smartrecruiters.com" and parts:
        return "smartrecruiters", parts[0], "default"
    if re.fullmatch(r"[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com", p.hostname):
        board = parts[-1] if parts else ""
        if not board:
            raise ValueError("Workday board is missing")
        return "workday", p.hostname.split(".")[0], board
    return "custom", p.hostname, "default"


async def verify_and_create(
    employer_id, official_url, portal_url, classification="direct_employer"
):
    official, portal = public_url(official_url), public_url(portal_url)
    config = get_config()
    if classification != "direct_employer":
        raise ValueError("Agency or unresolved employers require review")
    context = CollectionContext([{"host": official.hostname, "path": "/"}], config)
    try:
        observation = await context.fetch(official_url)
    finally:
        await context.close()
    soup = BeautifulSoup(observation.text, "html.parser")
    links = [
        urljoin(observation.url, a["href"])
        for a in soup.select("a[href],iframe[src]")
        if a.has_attr("href")
    ]

    def canonical(url):
        p = urlsplit(url)
        return p.hostname, p.path.rstrip("/")

    if canonical(portal_url) not in {canonical(u) for u in links}:
        raise ValueError("Official page does not visibly link to the proposed career portal")
    with get_session() as session:
        employer = session.get(Employer, employer_id)
        if not employer:
            raise ValueError("Employer not found")
        from company_service import normalize_company_name

        name = normalize_company_name(employer.name)
        if not name or name not in normalize_company_name(soup.get_text(" ", strip=True)):
            raise ValueError("Employer identity is not evidenced on the official page")
        if session.query(EmployerSource).filter_by(enabled=True).count() >= config.max_connected:
            raise ValueError("Connected-source limit reached")
        platform, tenant, board = detect_platform(portal_url)
        namespace = f"ats:{platform}:{tenant}:{board}"
        existing = session.scalar(
            select(EmployerSource).where(EmployerSource.namespace == namespace)
        )
        if existing:
            if existing.employer_id != employer_id:
                raise ValueError("Portal already belongs to a different employer; review identity")
            return existing.id
        rules = [{"host": portal.hostname, "path": "/", "methods": ["GET"]}]
        if platform == "smartrecruiters":
            rules += [
                {
                    "host": "api.smartrecruiters.com",
                    "path": f"/v1/companies/{tenant}/postings",
                    "methods": ["GET"],
                },
                {"host": "jobs.smartrecruiters.com", "path": f"/{tenant}/", "methods": ["GET"]},
            ]
        if platform == "workday":
            rules += [
                {
                    "host": portal.hostname,
                    "path": f"/wday/cxs/{tenant}/{board}/jobs",
                    "methods": ["POST"],
                    "body_keys": ["appliedFacets", "limit", "offset", "searchText"],
                }
            ]
        source = EmployerSource(
            employer_id=employer_id,
            namespace=namespace,
            platform=platform,
            tenant=tenant,
            board=board,
            portal_url=portal_url,
            config_json=json.dumps({"tenant": tenant, "board": board, "portal_url": portal_url}),
            policy_json=json.dumps(rules),
        )
        session.add(source)
        employer.official_url, employer.resolution_status = official_url, "resolved"
        employer.classification, employer.confidence = "direct_employer", 1.0
        session.add(
            EmployerEvidence(
                employer_id=employer_id,
                kind="verified_portal",
                evidence_key=digest([employer_id, observation.key, portal_url]),
                url=official_url,
                source="public_observation",
                payload_json=json.dumps(
                    {"portal_url": portal_url, "observation_hash": observation.key}
                ),
            )
        )
        session.flush()
        return source.id


def candidate(
    source_id, *, code="", manifest=None, config=None, parent_id=None, image_digest="", policy=None
):
    with get_session() as session:
        source = session.get(EmployerSource, source_id)
        if not source:
            raise ValueError("Source not found")
        kind = "builtin" if source.platform in ADAPTERS and not code else "generated"
        if kind == "generated":
            if len(code.encode()) > 100_000:
                raise ValueError("Connector code exceeds limit")
            tree = ast.parse(code)
            if not any(isinstance(n, ast.FunctionDef) and n.name == "collect" for n in tree.body):
                raise ValueError("Connector must define collect(config, sdk)")
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_digest):
                raise ValueError(
                    "Generated connector requires a pinned approved runtime image digest"
                )
        manifest = dict(manifest or SMARTRECRUITERS_MANIFEST)
        manifest["network_policy"] = (
            policy if policy is not None else json.loads(source.policy_json)
        )
        if kind=="builtin":
            manifest["adapter_fingerprint"]=adapter_fingerprint()
        if kind == "generated":
            import os

            manifest = dict(manifest)
            manifest["browser_image_digest"] = os.environ.get("EMPLOYER_BROWSER_IMAGE", "")
        config = config or json.loads(source.config_json)
        bundle = {
            "code": code,
            "manifest": manifest,
            "config": config,
            "sdk": SDK_VERSION,
            "image": image_digest,
            "kind": kind,
        }
        version = ConnectorVersion(
            source_id=source_id,
            code=code,
            kind=kind,
            config_json=json.dumps(config),
            manifest_json=json.dumps(manifest),
            parent_id=parent_id,
            image_digest=image_digest,
            digest=digest(bundle),
        )
        session.add(version)
        session.flush()
        return version.id


async def execute(version, source, context):
    if version.kind == "builtin":
        return await ADAPTERS[source.platform](json.loads(version.config_json), context)
    return await collect_generated(version, context)


async def run_version(version_id, *, validation=False, context_factory=None, fence=None):
    context_factory = context_factory or CollectionContext
    config = get_config()
    with get_session() as session:
        version = session.get(ConnectorVersion, version_id)
        source = session.get(EmployerSource, version.source_id)
        if not source.enabled:
            raise ValueError("Source is paused")
        if (version.kind=="builtin" and json.loads(version.manifest_json).get("adapter_fingerprint")
                !=adapter_fingerprint()):
            from employers.runtime import RuntimeUnavailable
            raise RuntimeUnavailable("Maintained adapter or dependencies changed; retry the source to validate a new version")
        bundle = {
            "code": version.code,
            "manifest": json.loads(version.manifest_json),
            "config": json.loads(version.config_json),
            "sdk": version.sdk_version,
            "image": version.image_digest,
            "kind": version.kind,
        }
        if digest(bundle) != version.digest:
            raise ValueError("Connector bundle changed after validation")
        version_obj = SimpleNamespace(
            **{c.name: getattr(version, c.name) for c in version.__table__.columns}
        )
        source_obj = SimpleNamespace(
            **{c.name: getattr(source, c.name) for c in source.__table__.columns}
        )
        scope = digest([version.digest, config.coverage])
        run = SourceRun(
            source_id=source.id, version_id=version.id, scope=scope, validation=validation
        )
        session.add(run)
        session.flush()
        run_id = run.id
    context = context_factory(
        json.loads(version_obj.manifest_json).get(
            "network_policy", json.loads(source_obj.policy_json)
        ),
        config,
    )
    result = None
    try:
        import asyncio

        async with asyncio.timeout(config.runtime_seconds):
            result = await execute(version_obj, source_obj, context)
        assessment = assess(result, context.observations, json.loads(version_obj.manifest_json))
        # Independently check sampled detail/application URLs for every candidate.
        if validation and assessment["outcome"] in {"complete", "empty"}:
            for posting in result.postings[:3]:
                # Verify the public vacancy page. Application URLs are checked
                # against authoritative response evidence, without entering forms
                # or authentication/challenge flows.
                url = posting.job.url
                context.allowed(url)
                live = await context.fetch(url)
                if not live.text.strip():
                    raise ValueError("Vacancy link returned no content")
        ref = artifacts.save(context.observations, assessment["outcome"] in {"failed", "partial"})
        assessment.update(fixture=ref, requests=context.requests, bytes=context.bytes)
        with get_session() as session:
            run = session.get(SourceRun, run_id)
            source = session.get(EmployerSource, source_obj.id)
            run.outcome, run.finished_at = assessment["outcome"], datetime.utcnow()
            run.stats_json = json.dumps(assessment)
            for identity in set(result.observed_ids):
                session.add(SourceRunMember(run_id=run_id, identity=identity))
            if fence:
                fence()
            if not validation:
                _finalize_collection(
                    session,
                    source,
                    version_id,
                    scope,
                    result,
                    assessment,
                    config,
                    run_id,
                    context.observations,
                )
                run.outcome = assessment["outcome"]
                run.stats_json = json.dumps(assessment)
        return assessment
    except Exception as exc:
        try:
            ref = artifacts.save(context.observations, True)
        except (OSError, ValueError):
            ref = None
        with get_session() as session:
            run = session.get(SourceRun, run_id)
            run.outcome, run.finished_at, run.error = "failed", datetime.utcnow(), str(exc)[:2000]
            run.stats_json = json.dumps(
                {"fixture": ref, "requests": context.requests, "bytes": context.bytes}
            )
        raise
    finally:
        await context.close()


def _finalize_collection(
    session, source, version_id, scope, result, assessment, config, run_id, observations
):
    if not source.enabled or source.active_version_id != version_id:
        return
    now = datetime.utcnow()
    complete = assessment["outcome"] in {"complete", "empty"}
    previous = session.scalar(
        select(SourceRun)
        .where(
            SourceRun.source_id == source.id,
            SourceRun.validation.is_(False),
            SourceRun.outcome.in_(["complete", "empty"]),
            SourceRun.scope == scope,
            SourceRun.id != run_id,
        )
        .order_by(SourceRun.id.desc())
    )
    if complete and previous:
        count = json.loads(previous.stats_json).get("observed_count", 0)
        if count >= 10 and assessment["observed_count"] < count / 2:
            # A second successful independent run is needed before applying a collapse.
            prior = session.scalar(
                select(SourceRun)
                .where(
                    SourceRun.source_id == source.id,
                    SourceRun.validation.is_(False),
                    SourceRun.id != run_id,
                    SourceRun.scope == scope,
                )
                .order_by(SourceRun.id.desc())
            )
            prior_stats = json.loads(prior.stats_json) if prior else {}
            if not (prior_stats.get("collapse_confirmation") == assessment["observed_count"]):
                assessment["outcome"] = "partial"
                assessment["collapse_confirmation"] = assessment["observed_count"]
                assessment["errors"].append(
                    "Suspicious catalogue count collapse; requires confirmation"
                )
                complete = False
    if not config.shadow:
        active_profiles = session.scalars(
            select(SearchProfile).where(SearchProfile.active.is_(True))
        ).all()
        for posting in result.postings:
            if not in_coverage(posting, config.coverage):
                continue
            # Partial extraction can contribute only individually evidenced valid rows.
            if posting_errors(posting, observations):
                continue
            job, _, record = ingest(session, posting.job, source=source, posting=posting)
            from analyzer.scorer import fast_score

            for profile in active_profiles:
                if not session.get(JobProfile, (job.id, profile.id)):
                    score = fast_score(profile.cv_text, job.description)
                    session.add(
                        JobProfile(
                            job_id=job.id,
                            profile_id=profile.id,
                            match_score=score.score,
                            match_explanation=score.explanation,
                            screened_at=now,
                        )
                    )
        if complete:
            observed = set(result.observed_ids)
            for row in session.scalars(select(JobSource).where(JobSource.source_id == source.id)):
                if row.posting_id in observed:
                    row.missing_checks, row.availability, row.checked_at = 0, "open", now
                    continue
                if row.missing_scope != scope or row.missing_checks == 0:
                    row.missing_checks = 1
                elif row.checked_at and now - row.checked_at < timedelta(hours=2):
                    continue
                else:
                    row.missing_checks += 1
                row.missing_scope, row.checked_at = scope, now
                if row.missing_checks >= 2:
                    row.availability = "closed"
                    siblings = session.scalars(
                        select(JobSource).where(JobSource.job_id == row.job_id)
                    ).all()
                    if row.job_id and all(s.availability == "closed" for s in siblings):
                        job = session.get(Job, row.job_id)
                        job.expired_at, job.expired_reason = (
                            now,
                            "All monitored sources confirmed closure",
                        )
    if complete:
        source.last_success_at, source.health, source.last_error = now, "active", None
    else:
        source.health, source.last_error = "degraded", "; ".join(assessment["errors"])[:2000]
    source.next_due_at = now + timedelta(hours=config.poll_hours)


async def validate_activate(version_id, *, context_factory=None, fence=None):
    with get_session() as session:
        version = session.get(ConnectorVersion, version_id)
        version.status = "validating"
        source_id = version.source_id
        expected_active = session.get(EmployerSource, source_id).active_version_id
    try:
        first = await run_version(version_id, validation=True, context_factory=context_factory)
        second = await run_version(version_id, validation=True, context_factory=context_factory)
        with get_session() as session:
            version = session.get(ConnectorVersion, version_id)
            report = {"first": first, "second": second, "repeatable": repeatable(first, second)}
            version.validation_json = json.dumps(report)
        if not report["repeatable"]:
            raise ValueError("Candidate failed completeness or stable identity validation")
        # Replay saved observations through the same code and trusted validator.
        from employers.replay import replay_context

        for item in (first, second):
            replayed = await run_version(
                version_id,
                validation=True,
                context_factory=replay_context(artifacts.load(item["fixture"])),
            )
            if not repeatable(item, replayed):
                raise ValueError("Candidate failed saved-fixture replay")
        with get_session() as session:
            version = session.get(ConnectorVersion, version_id)
            source = session.get(EmployerSource, source_id)
            bundle = {
                "code": version.code,
                "manifest": json.loads(version.manifest_json),
                "config": json.loads(version.config_json),
                "sdk": version.sdk_version,
                "image": version.image_digest,
                "kind": version.kind,
            }
            if fence:
                fence()
            if (
                digest(bundle) != version.digest
                or not source.enabled
                or source.active_version_id != expected_active
            ):
                raise ValueError("Candidate changed during validation or source paused")
            old = (
                session.get(ConnectorVersion, source.active_version_id)
                if source.active_version_id
                else None
            )
            refs = [first["fixture"], second["fixture"]]
            if old:
                old.status = "validated"
                old_report = json.loads(old.validation_json)
                refs += [old_report[k]["fixture"] for k in ("first", "second") if k in old_report]
            artifacts.pin(source_id, refs)
            activated = session.execute(
                update(EmployerSource)
                .where(
                    EmployerSource.id == source_id,
                    EmployerSource.enabled.is_(True),
                    EmployerSource.active_version_id.is_(None)
                    if expected_active is None
                    else EmployerSource.active_version_id == expected_active,
                )
                .values(active_version_id=version_id, health="active", last_error=None)
            )
            if activated.rowcount != 1:
                raise ValueError("Source changed during connector activation")
            version.status = "active"
            for incident in session.scalars(
                select(ConnectorIncident).where(
                    ConnectorIncident.source_id == source_id, ConnectorIncident.status == "open"
                )
            ):
                incident.status = "resolved"
        return version_id
    except Exception as exc:
        with get_session() as session:
            version = session.get(ConnectorVersion, version_id)
            source = session.get(EmployerSource, source_id)
            if source.active_version_id != version_id:
                version.status = "rejected"
                report = json.loads(version.validation_json)
                report["error"] = str(exc)[:2000]
                version.validation_json = json.dumps(report)
            else:
                version.status = "active"
        raise
