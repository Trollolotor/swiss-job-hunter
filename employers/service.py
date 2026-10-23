"""Employer verification, version validation, trusted ingestion, and lifecycle."""

import ast
import json
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
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
from employers.network import CollectionContext, CollectionLimit, PolicyError, public_url
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


def detect_platform(url, html=""):
    p = public_url(url)
    parts = [v for v in p.path.split("/") if v]
    if p.hostname == "careers.smartrecruiters.com" and parts:
        return "smartrecruiters", parts[0], "default"
    if re.fullmatch(r"[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com", p.hostname):
        board = parts[-1] if parts else ""
        if not board:
            raise ValueError("Workday board is missing")
        return "workday", p.hostname.split(".")[0], board
    from ats_vendors.registry import detect

    ready = detect(url, html)
    if ready:
        return ready.vendor, ready.tenant, ready.board
    return "custom", p.hostname, "default"


async def verify_and_create(
    employer_id, official_url, portal_url, classification="direct_employer"
):
    official, portal = public_url(official_url), public_url(portal_url)
    config = get_config()
    if classification != "direct_employer" and not (
        classification == "agency" and config.include_agencies
    ):
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
        # Legacy SuccessFactors tenants share a hostname AND a /career path.
        # The company query parameter is part of the employer identity.
        company = (
            (parse_qs(p.query).get("company") or [""])[0]
            if re.fullmatch(
                r"[a-z0-9-]+\.(?:successfactors\.(?:com|eu)|sapsf\.(?:com|eu|cn))",
                p.hostname or "",
            )
            else ""
        )
        return p.hostname, p.path.rstrip("/"), company

    if canonical(portal_url) not in {canonical(u) for u in links}:
        raise ValueError("Official page does not visibly link to the proposed career portal")
    from ats_vendors.registry import detect, network_rules

    ready = detect(portal_url)
    portal_observation = None
    if ready is None and detect_platform(portal_url)[0] == "custom":
        # A branded SuccessFactors site needs observed vendor configuration,
        # rather than a guessed hostname or text-only brand mention.
        portal_context = CollectionContext([{"host": portal.hostname, "path": "/"}], config)
        try:
            portal_observation = await portal_context.fetch(portal_url)
            ready = detect(portal_url, portal_observation.text)
        except (httpx.HTTPError, TimeoutError, PolicyError, CollectionLimit):
            # This optional fingerprint probe must not prevent the established
            # custom-portal investigation/browser flow from onboarding a source.
            pass
        finally:
            await portal_context.close()
    with get_session() as session:
        employer = session.get(Employer, employer_id)
        if not employer:
            raise ValueError("Employer not found")
        if employer.classification == "agency":
            if not config.include_agencies:
                raise ValueError("Agency onboarding is excluded by the current settings")
            classification = "agency"
        from company_service import normalize_company_name

        name = normalize_company_name(employer.name)
        if not name or name not in normalize_company_name(soup.get_text(" ", strip=True)):
            raise ValueError("Employer identity is not evidenced on the official page")
        if session.query(EmployerSource).filter_by(enabled=True).count() >= config.max_connected:
            raise ValueError("Connected-source limit reached")
        platform, tenant, board = (
            (ready.vendor, ready.tenant, ready.board) if ready else detect_platform(portal_url)
        )
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
        source_config = {"tenant": tenant, "board": board, "portal_url": portal_url}
        if ready:
            from dataclasses import replace

            ready = replace(ready, company=employer.name)
            source_config = ready.config()
            rules = network_rules(ready)
        source = EmployerSource(
            employer_id=employer_id,
            namespace=namespace,
            platform=platform,
            tenant=tenant,
            board=board,
            portal_url=portal_url,
            config_json=json.dumps(source_config),
            policy_json=json.dumps(rules),
        )
        session.add(source)
        employer.official_url, employer.resolution_status = official_url, "resolved"
        employer.classification, employer.confidence = classification, 1.0
        session.add(
            EmployerEvidence(
                employer_id=employer_id,
                kind="verified_portal",
                evidence_key=digest([employer_id, observation.key, portal_url]),
                url=official_url,
                source="public_observation",
                payload_json=json.dumps(
                    {
                        "portal_url": portal_url,
                        "observation_hash": observation.key,
                        "ats_protocol": (
                            {
                                "vendor": ready.vendor,
                                "variant": ready.variant,
                                "observation_hash": portal_observation.key,
                            }
                            if ready and portal_observation
                            else None
                        ),
                    }
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
        if manifest and "ats_library" in manifest:
            from employers.ats_validation import definition
            from ats_vendors.registry import from_config

            defined, _ = definition(manifest)
            bound = from_config(source.platform, json.loads(source.config_json))
            configured = from_config(source.platform, config or json.loads(source.config_json))
            if defined != bound or configured != bound:
                raise ValueError("Ready connector library does not match its source configuration")
        kind = "builtin" if source.platform in ADAPTERS and not code else "generated"
        if kind == "generated":
            if len(code.encode()) > 100_000:
                raise ValueError("Connector code exceeds limit")
            tree = ast.parse(code)
            functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
            if (
                "collect" not in functions
                and not {"enumerate", "fetch_detail", "normalize"} <= functions
            ):
                raise ValueError(
                    "Connector must define SDK v1 collect or SDK v2 enumerate/fetch_detail/normalize"
                )
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_digest):
                raise ValueError(
                    "Generated connector requires a pinned approved runtime image digest"
                )
        manifest = dict(manifest or SMARTRECRUITERS_MANIFEST)
        manifest["network_policy"] = (
            policy if policy is not None else json.loads(source.policy_json)
        )
        if kind == "builtin":
            manifest["adapter_fingerprint"] = adapter_fingerprint()
        if kind == "generated":
            import os

            manifest = dict(manifest)
            manifest["browser_image_digest"] = os.environ.get("EMPLOYER_BROWSER_IMAGE", "")
        config = config or json.loads(source.config_json)
        sdk_version = (
            "2"
            if kind == "builtin" or {"enumerate", "fetch_detail", "normalize"} <= functions
            else "1"
        )
        manifest["contract_version"] = sdk_version
        bundle = {
            "code": code,
            "manifest": manifest,
            "config": config,
            "sdk": sdk_version,
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
            sdk_version=sdk_version,
        )
        session.add(version)
        session.flush()
        return version.id


async def execute(version, source, context):
    if version.kind == "builtin":
        return await ADAPTERS[source.platform](json.loads(version.config_json), context)
    return await collect_generated(version, context)


def run_memberships(context, postings):
    """Keep enumerated identities even when a later detail cannot be retrieved."""
    identities = {posting.identity for posting in postings}
    variants = {}
    for posting in postings:
        variants.setdefault(posting.posting_id, set()).add(posting.variant)
    for entry in context.catalogue_entries:
        if entry.posting_id not in variants or (
            entry.variant and entry.variant not in variants[entry.posting_id]
        ):
            identities.add(entry.identity)
    return identities


async def run_version(version_id, *, validation=False, context_factory=None, fence=None):
    context_factory = context_factory or CollectionContext
    config = get_config()
    with get_session() as session:
        version = session.get(ConnectorVersion, version_id)
        source = session.get(EmployerSource, version.source_id)
        if not source.enabled:
            raise ValueError("Source is paused")
        if (
            version.kind == "builtin"
            and json.loads(version.manifest_json).get("adapter_fingerprint")
            != adapter_fingerprint()
        ):
            from employers.runtime import RuntimeUnavailable

            raise RuntimeUnavailable(
                "Maintained adapter or dependencies changed; retry the source to validate a new version"
            )
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
        saved_manifest = json.loads(version_obj.manifest_json)
        if "ats_library" in saved_manifest:
            from employers.ats_validation import definition

            try:
                definition(saved_manifest)
            except ValueError as exc:
                from employers.runtime import RuntimeUnavailable

                raise RuntimeUnavailable(str(exc)) from exc
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
    context.source_id, context.version_id, context.validation = (
        source_obj.id,
        version_id,
        validation,
    )
    context.fence = fence
    context.catalogue_spec = json.loads(version_obj.manifest_json).get("catalogue", {})
    context.manifest = json.loads(version_obj.manifest_json)
    context.fresh_detail_keys, context.staged, context.catalogue_entries = set(), [], []
    result = None
    try:
        import asyncio

        async with asyncio.timeout(config.runtime_seconds):
            result = await execute(version_obj, source_obj, context)
        assessment = assess(result, context.observations, json.loads(version_obj.manifest_json))
        from employers.validation import complete_description

        for posting in result.postings:
            posting.job.extra["description_quality"] = (
                "complete"
                if complete_description(
                    posting, context.observations, json.loads(version_obj.manifest_json)
                )
                else "preview"
            )
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
        assessment.update(
            fixture=ref,
            requests=context.requests,
            bytes=context.bytes,
            cache_hits=getattr(context, "cache_hits", 0),
        )
        with get_session() as session:
            run = session.get(SourceRun, run_id)
            source = session.get(EmployerSource, source_obj.id)
            run.outcome, run.finished_at = assessment["outcome"], datetime.utcnow()
            run.stats_json = json.dumps(assessment)
            for identity in run_memberships(context, result.postings):
                session.add(SourceRunMember(run_id=run_id, identity=identity))
            if fence:
                fence()
            if not validation:
                from employers.cache import save

                save(
                    session,
                    context,
                    context.catalogue_entries,
                    [
                        p for p in result.postings
                        if not posting_errors(p, context.observations, context.manifest)
                    ],
                )
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
                    context.manifest,
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
            if not validation and context.staged:
                if fence:
                    fence()
                from employers.contracts import CollectionResult

                partial = CollectionResult(
                    postings=context.staged,
                    observed_ids=list({p.posting_id for p in context.staged}),
                    diagnostics=[str(exc)[:1000]],
                )
                assessment = {
                    "outcome": "partial",
                    "errors": [str(exc)[:1000]],
                    "observed_count": len({entry.posting_id for entry in context.catalogue_entries})
                    or len(partial.observed_ids),
                }
                source = session.get(EmployerSource, source_obj.id)
                _finalize_collection(
                    session,
                    source,
                    version_id,
                    scope,
                    partial,
                    assessment,
                    config,
                    run_id,
                    context.observations,
                    context.manifest,
                )
                run.outcome = "partial"
                assessment.update(
                    fixture=ref,
                    requests=context.requests,
                    bytes=context.bytes,
                    cache_hits=getattr(context, "cache_hits", 0),
                )
                run.stats_json = json.dumps(assessment)
            for identity in run_memberships(context, context.staged):
                session.add(SourceRunMember(run_id=run_id, identity=identity))
        raise
    finally:
        await context.close()


def _finalize_collection(
    session, source, version_id, scope, result, assessment, config, run_id, observations, manifest=None
):
    if not source.enabled or source.active_version_id != version_id:
        return
    current = get_config()
    if current.coverage != config.coverage or (current.shadow and not config.shadow):
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
            if posting_errors(posting, observations, manifest):
                continue
            job, _, record = ingest(session, posting.job, source=source, posting=posting)
            from analyzer.matching import local_evaluate

            for profile in active_profiles:
                local_evaluate(session, job, profile)
        if complete:
            observed = {p.identity for p in result.postings}
            for row in session.scalars(select(JobSource).where(JobSource.source_id == source.id)):
                if json.dumps([row.posting_id, row.variant], separators=(",", ":")) in observed:
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
        if not config.shadow and source.baseline_at is None:
            # Ingestion timestamps are assigned inside the loop above. The
            # baseline must follow them so initial catalogue jobs are excluded.
            source.baseline_at = datetime.utcnow()
        source.last_success_at, source.health, source.last_error = now, "active", None
    else:
        source.health, source.last_error = "degraded", "; ".join(assessment["errors"])[:2000]
    source.next_due_at = now + timedelta(hours=source.poll_hours or config.poll_hours)


async def validate_activate(version_id, *, context_factory=None, fence=None):
    with get_session() as session:
        version = session.get(ConnectorVersion, version_id)
        version.status = "validating"
        source_id = version.source_id
        expected_active = session.get(EmployerSource, source_id).active_version_id
    try:
        first = await run_version(
            version_id, validation=True, context_factory=context_factory, fence=fence
        )
        second = await run_version(
            version_id, validation=True, context_factory=context_factory, fence=fence
        )
        with get_session() as session:
            version = session.get(ConnectorVersion, version_id)
            report = {"first": first, "second": second, "repeatable": repeatable(first, second)}
            version.validation_json = json.dumps(report)
        if not report["repeatable"]:
            if first.get("limitation") or second.get("limitation"):
                raise CollectionLimit(
                    "Vendor catalogue completeness is limited by the source's result cap/page budget"
                )
            failures = list(dict.fromkeys(first.get("errors", []) + second.get("errors", [])))
            raise ValueError(
                "Candidate failed completeness or stable identity validation: "
                + "; ".join(failures[:5])[:1200]
            )
        # Replay saved observations through the same code and trusted validator.
        from employers.replay import replay_context

        for item in (first, second):
            replayed = await run_version(
                version_id,
                validation=True,
                context_factory=replay_context(artifacts.load(item["fixture"])),
                fence=fence,
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
