"""SQLite durable work with leases, fencing, bounded retries, and incident limits."""

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import case, func, select, update

from db.models import (
    BudgetLedger,
    ConnectorIncident,
    ConnectorVersion,
    Employer,
    EmployerEvidence,
    EmployerSource,
    EmployerTask,
    SourceRun,
)
from db.session import get_session, init_db
from employers.agent import step
from employers.budget import BudgetUnavailable
from employers.config import get_config
from employers.network import CollectionContext, PolicyError
from employers.runtime import RuntimeUnavailable
from employers.service import (
    candidate,
    row_dict,
    run_version,
    validate_activate,
    verify_and_create,
)

_running = set()


class StaleTaskError(RuntimeError):
    pass


def enqueue(kind, *, employer_id=None, source_id=None, payload=None, delay=0):
    init_db()
    base = f"{kind}:{source_id or employer_id or 'global'}:"
    with get_session() as session:
        existing = session.scalar(
            select(EmployerTask).where(
                EmployerTask.dedupe_key.like(base + "%"),
                EmployerTask.status.in_(["queued", "running", "budget_wait"]),
            )
        )
        if existing:
            return existing.id
        task = EmployerTask(
            kind=kind,
            employer_id=employer_id,
            source_id=source_id,
            payload_json=json.dumps(payload or {}),
            dedupe_key=base + uuid.uuid4().hex,
            due_at=datetime.utcnow() + timedelta(seconds=delay),
        )
        session.add(task)
        session.flush()
        return task.id


def claim(task_id=None, allowed_kinds=None):
    now = datetime.utcnow()
    with get_session() as session:
        query = (
            select(EmployerTask)
            .where(
                EmployerTask.due_at <= now,
                (
                    (EmployerTask.status == "queued")
                    | ((EmployerTask.status == "running") & (EmployerTask.lease_until <= now))
                ),
            )
            .order_by(
                case(
                    (EmployerTask.kind.in_(["collect", "health"]), 0),
                    (EmployerTask.kind == "repair", 1),
                    else_=2,
                ),
                EmployerTask.created_at,
            )
        )
        if allowed_kinds:
            query = query.where(EmployerTask.kind.in_(allowed_kinds))
        if task_id:
            query = query.where(EmployerTask.id == task_id)
        task = session.scalar(query.limit(1))
        if not task:
            return None
        token = uuid.uuid4().hex
        changed = session.execute(
            update(EmployerTask)
            .where(
                EmployerTask.id == task.id,
                (
                    (EmployerTask.status == "queued")
                    | ((EmployerTask.status == "running") & (EmployerTask.lease_until <= now))
                ),
            )
            .values(
                status="running",
                token=token,
                lease_until=now + timedelta(minutes=2),
                attempts=EmployerTask.attempts + 1,
            )
        )
        if changed.rowcount != 1:
            return None
        session.flush()
        session.refresh(task)
        return row_dict(task)


def fenced(task):
    with get_session() as session:
        current = session.get(EmployerTask, task["id"])
        if (
            not current
            or current.token != task["token"]
            or current.status != "running"
            or not current.lease_until
            or current.lease_until < datetime.utcnow()
        ):
            raise StaleTaskError("Task lease was replaced; stale result rejected")


async def _heartbeat(task):
    while True:
        await asyncio.sleep(30)
        with get_session() as session:
            session.execute(
                update(EmployerTask)
                .where(
                    EmployerTask.id == task["id"],
                    EmployerTask.token == task["token"],
                    EmployerTask.status == "running",
                )
                .values(lease_until=datetime.utcnow() + timedelta(minutes=2))
            )


def _finish(task, status, error=None, delay=0):
    with get_session() as session:
        session.execute(
            update(EmployerTask)
            .where(EmployerTask.id == task["id"], EmployerTask.token == task["token"])
            .values(
                status=status,
                error=error,
                lease_until=None,
                token=None,
                due_at=datetime.utcnow() + timedelta(seconds=delay),
            )
        )


async def _resolve(task):
    with get_session() as session:
        employer = session.get(Employer, task["employer_id"])
        if employer.classification == "agency" and not get_config().include_agencies:
            return
        employer.resolution_status = "resolving"
        evidence = {
            "employer": employer.name,
            "classification": employer.classification,
            "observations": [
                row_dict(v)
                for v in session.scalars(
                    select(EmployerEvidence)
                    .where(EmployerEvidence.employer_id == employer.id)
                    .order_by(
                        case((EmployerEvidence.kind == "board", 0), else_=1).desc(),
                        EmployerEvidence.observed_at.desc(),
                    )
                    .limit(10)
                )
            ],
        }
    from employers.resolution import from_official_links

    deterministic = (
        await from_official_links(task, evidence, fenced)
        if evidence["classification"] in {"direct_employer", "agency"}
        else None
    )
    if deterministic:
        enqueue("onboard", source_id=deterministic)
        return
    for _ in range(5):
        known_website = any(
            o.get("payload", {}).get("official_url")
            for o in evidence["observations"]
            if isinstance(o, dict)
        )
        answer = await step(
            "employer_resolution",
            evidence,
            task["id"],
            search=not evidence.get("searched") and not known_website,
        )
        evidence["searched"] = True
        if answer.classification == "agency" and not get_config().include_agencies:
            with get_session() as session:
                employer = session.get(Employer, task["employer_id"])
                employer.classification = "agency"
                employer.resolution_status = "unresolved"
            return
        if answer.classification in {"direct_employer", "agency"}:
            evidence["classification"] = answer.classification
        if answer.official_url:
            # A model may find the official site before it finds a career
            # portal. Follow its public links under the same trusted verifier.
            evidence.setdefault("candidate_official_urls", []).append(answer.official_url)
            deterministic = (
                await from_official_links(task, evidence, fenced)
                if evidence["classification"] in {"direct_employer", "agency"}
                else None
            )
            if deterministic:
                enqueue("onboard", source_id=deterministic)
                return
        if answer.action == "fetch" and answer.url:
            context = CollectionContext(
                [{"host": urlsplit(answer.url).hostname, "path": "/"}], get_config()
            )
            try:
                obs = await context.fetch(answer.url)
                evidence["observations"].append({"url": obs.url, "html": obs.text[:40000]})
            finally:
                await context.close()
        elif answer.action == "resolved" and answer.official_url and answer.portal_url:
            fenced(task)
            source_id = await verify_and_create(
                task["employer_id"], answer.official_url, answer.portal_url, answer.classification
            )
            enqueue("onboard", source_id=source_id)
            return
        elif answer.official_url:
            evidence["verification_feedback"] = (
                "Official site proposed, but no verified career portal found. Inspect the official career/vacancy page and its ATS link."
            )
        else:
            with get_session() as session:
                employer = session.get(Employer, task["employer_id"])
                employer.resolution_status = "unresolved"
                if answer.classification in {"agency", "unresolved"}:
                    employer.classification = answer.classification
            return
    raise ValueError("Employer resolution exceeded investigation step limit")


async def _generate(task, source, repair=False, incident=None):
    with get_session() as session:
        previous = (
            session.get(ConnectorVersion, source["active_version_id"])
            if source["active_version_id"]
            else None
        )
        if repair and previous is None:
            previous = session.scalar(
                select(ConnectorVersion)
                .where(ConnectorVersion.source_id == source["id"])
                .order_by(ConnectorVersion.id.desc())
            )
        evidence = {
            "source": source,
            "sdk_version": previous.sdk_version if repair and previous else "2",
            "previous": row_dict(previous) if previous else None,
            "incident": incident,
            "observations": [],
        }
        references = []
        if previous:
            report = json.loads(previous.validation_json)
            references.extend(
                ("last_success", report[k].get("fixture"))
                for k in ("first", "second")
                if k in report and report[k].get("outcome") in {"complete", "empty"}
            )
        if repair:
            latest = session.scalar(
                select(SourceRun)
                .where(SourceRun.source_id == source["id"])
                .order_by(SourceRun.id.desc())
            )
            if latest:
                references.append(("current_failure", json.loads(latest.stats_json).get("fixture")))
        from employers import artifacts

        for label, reference in references:
            if not reference:
                continue
            try:
                saved = artifacts.load(reference)
                from employers.investigation import samples

                evidence[label] = [
                    {
                        "url": o.url,
                        "method": o.method,
                        "body": o.request_body,
                        "status": o.status,
                        "key": o.key,
                        "text": o.text[:20000],
                    }
                    for o in samples(saved)
                ]
            except (OSError, ValueError):
                evidence[label] = [{"error": "Saved diagnostic evidence is unavailable"}]
    context = CollectionContext(
        evidence["previous"]["manifest"].get("network_policy", source["policy"])
        if evidence["previous"]
        else source["policy"],
        get_config(),
    )
    from employers.contracts import CatalogueResult, CollectionResult

    evidence["sdk"] = {
        "version": evidence["sdk_version"],
        "runtime": "sdk is passed as a function argument. Return plain Python dictionaries; do not import SDK, at_sdk, application models or any non-installed package.",
        "methods": {
            "sdk.json(url, method='GET', body=None)": "returns (parsed_json, evidence_key)",
            "sdk.fetch(url, method='GET', body=None)": "returns dict with text, url, status, headers, key; not a requests.Response",
            "sdk.browser(url, actions=[])": "returns bounded HTML and approved observations",
        },
        "functions": {
            "enumerate(config, sdk)": "returns CatalogueResult dictionary; fetch all listing pages, never details",
            "fetch_detail(config, entry, sdk)": "returns a dictionary containing fetched source data and its real evidence key",
            "normalize(config, entry, detail)": "returns ONE CollectedPosting dictionary, not CollectionResult; use evidence keys from SDK responses",
        },
        "manifest": {
            "catalogue": {
                "format": "json",
                "path_suffix": "actual listing endpoint suffix",
                "items": "dot.path.to.items",
                "id": "dot.path.within.item; list index uses .0, not [0]",
                "total": "dot.path.to.total",
                "pagination": "offset",
            }
        },
        "catalogue_schema": CatalogueResult.model_json_schema(),
        "result_schema": CollectionResult.model_json_schema(),
    }
    if evidence["sdk_version"] == "1":
        evidence["sdk"]["functions"] = {
            "collect(config, sdk)": "returns CollectionResult dictionary"
        }
    try:
        obs = await context.fetch(source["portal_url"])
        evidence["observations"].append({"url": obs.url, "html": obs.text[:60000]})
        # Static script dependencies are eligible only when the verified portal
        # actually references them. Persist exact paths before use by generated code.
        from urllib.parse import urljoin

        from bs4 import BeautifulSoup

        for tag in BeautifulSoup(obs.text, "html.parser").select("script[src]")[:20]:
            url = urljoin(obs.url, tag["src"])
            from employers.network import public_url

            p = public_url(url)
            context.policy.append({"host": p.hostname, "path": p.path, "methods": ["GET"]})
        if not repair or not evidence.get("last_success"):
            from employers.investigation import prime

            evidence["observations"].extend(await prime(source, context))
            for _ in range(6):
                answer = await step("connector_investigation", evidence, task["id"])
                if answer.action == "fetch" and answer.url:
                    obs = await context.fetch(answer.url, answer.method, answer.body)
                    evidence["observations"].append(
                        {
                            "url": obs.url,
                            "method": obs.method,
                            "body": obs.request_body,
                            "key": obs.key,
                            "text": obs.text[:60000],
                        }
                    )
                elif answer.action == "browser":
                    from employers.runtime import run_isolated

                    result = await run_isolated(
                        "browser",
                        {"url": source["portal_url"]},
                        context,
                        image_digest=os.environ.get("EMPLOYER_BROWSER_IMAGE"),
                    )
                    evidence["observations"].append(result)
                else:
                    # Do not generate from a catalogue plus a generic portal
                    # shell. A separately observed detail is required.
                    def has_detail(observation):
                        if observation.method != "GET" or observation.url == source["portal_url"]:
                            return False
                        try:
                            value = observation.json()
                            return isinstance(value, dict) and any(
                                key in value
                                for key in (
                                    "jobPostingInfo",
                                    "jobAd",
                                    "description",
                                    "jobDescription",
                                )
                            )
                        except (ValueError, TypeError):
                            from bs4 import BeautifulSoup

                            for node in BeautifulSoup(observation.text, "html.parser").select(
                                'script[type="application/ld+json"]'
                            ):
                                try:
                                    value = json.loads(node.get_text())
                                    if (
                                        isinstance(value, dict)
                                        and value.get("@type") == "JobPosting"
                                        and value.get("description")
                                    ):
                                        return True
                                except (ValueError, TypeError):
                                    continue
                            return False

                    if not any(has_detail(observation) for observation in context.observations):
                        evidence["verification_feedback"] = (
                            "Inspect a real full detail response before generation. The portal HTML and listing summaries are insufficient. For structured platforms prefer their read-only JSON detail endpoint, evidenced by the listing path and public portal."
                        )
                        continue
                    evidence["investigation"] = answer.model_dump()
                    break
            else:
                raise ValueError("Investigation did not obtain independent detail evidence")
        if repair:
            # Attempt accounting occurs before paid candidate generation so crashes
            # cannot reset or bypass the per-incident cap.
            fenced(task)
            with get_session() as session:
                paid_before = (
                    session.query(func.count(BudgetLedger.id))
                    .filter_by(task_id=task["id"], operation="connector_repair")
                    .scalar()
                )
                changed = session.execute(
                    update(ConnectorIncident)
                    .where(
                        ConnectorIncident.id == incident["id"],
                        ConnectorIncident.failed_candidates < 2,
                        ConnectorIncident.status == "open",
                    )
                    .values(failed_candidates=ConnectorIncident.failed_candidates + 1)
                )
                if changed.rowcount != 1:
                    raise ValueError("Repair incident reached manual-review limit")
        try:
            answer = await step(
                "connector_repair" if repair else "connector_generation", evidence, task["id"]
            )
        except BudgetUnavailable:
            if repair:
                with get_session() as session:
                    paid_after = (
                        session.query(func.count(BudgetLedger.id))
                        .filter_by(task_id=task["id"], operation="connector_repair")
                        .scalar()
                    )
                    if paid_after == paid_before:
                        session.execute(
                            update(ConnectorIncident)
                            .where(
                                ConnectorIncident.id == incident["id"],
                                ConnectorIncident.failed_candidates > 0,
                            )
                            .values(failed_candidates=ConnectorIncident.failed_candidates - 1)
                        )
            raise
        fenced(task)
        version_id = candidate(
            source["id"],
            code=answer.code,
            manifest={**answer.manifest, "generation_task_id": task["id"]},
            config=answer.config or source["config"],
            parent_id=evidence["previous"]["id"]
            if evidence["previous"]
            else source["active_version_id"],
            image_digest=os.environ.get("EMPLOYER_RUNNER_IMAGE", ""),
            policy=context.policy,
        )
        return version_id
    finally:
        await context.close()


async def execute_task(task):
    from employers.agent import fence_context

    token = fence_context.set(lambda: fenced(task))
    heartbeat = asyncio.create_task(_heartbeat(task))
    try:
        if task["kind"] == "discover":
            from employers.discovery import backfill

            result = backfill()
            if not result["done"]:
                _finish(task, "queued", delay=1)
                return
        elif task["kind"] == "resolve":
            await _resolve(task)
        else:
            with get_session() as session:
                row = session.get(EmployerSource, task["source_id"])
                if not row or not row.enabled:
                    _finish(task, "cancelled")
                    return
                source = row_dict(row)
            if task["kind"] in {"collect", "health"}:
                if not source["active_version_id"]:
                    raise ValueError("Source has no validated active connector")
                result = await run_version(source["active_version_id"], fence=lambda: fenced(task))
                if "collapse_confirmation" in result:
                    enqueue("health", source_id=source["id"], delay=900)
                    fenced(task)
                    _finish(task, "completed")
                    return
                if result["outcome"] in {"partial", "failed"}:
                    raise ValueError("; ".join(result["errors"]))
            elif task["kind"] == "revalidate":
                with get_session() as session:
                    current = session.get(ConnectorVersion, source["active_version_id"])
                    if not current:
                        raise ValueError("No active connector is available for revalidation")
                    saved = row_dict(current)
                version_id = candidate(
                    source["id"],
                    code=saved["code"],
                    manifest=saved["manifest"],
                    config=saved["config"],
                    parent_id=saved["id"],
                    image_digest=os.environ.get("EMPLOYER_RUNNER_IMAGE", ""),
                    policy=saved["manifest"].get("network_policy"),
                )
                await validate_activate(version_id, fence=lambda: fenced(task))
            elif task["kind"] == "validate":
                version_id = task["payload"]["version_id"]
                with get_session() as session:
                    version = session.get(ConnectorVersion, version_id)
                    if not version or version.source_id != source["id"]:
                        raise ValueError("Connector version does not belong to this source")
                await validate_activate(version_id, fence=lambda: fenced(task))
                enqueue("collect", source_id=source["id"])
            elif task["kind"] == "rollback":
                await validate_activate(task["payload"]["version_id"], fence=lambda: fenced(task))
            elif task["kind"] in {"onboard", "repair"}:
                incident = None
                with get_session() as session:
                    row = session.scalar(
                        select(ConnectorIncident).where(
                            ConnectorIncident.source_id == source["id"],
                            ConnectorIncident.status.in_(["open", "needs_review"]),
                        )
                    )
                    if row:
                        incident = row_dict(row)
                repair_mode = task["kind"] == "repair" or incident is not None
                if repair_mode and (
                    not incident
                    or incident["failed_candidates"] >= 2
                    or incident["status"] == "needs_review"
                ):
                    with get_session() as session:
                        if incident:
                            session.get(ConnectorIncident, incident["id"]).status = "needs_review"
                        if not source["active_version_id"]:
                            session.get(EmployerSource, source["id"]).health = "blocked"
                    _finish(task, "needs_review")
                    return
                if source["platform"] == "smartrecruiters" and not repair_mode:
                    version_id = candidate(source["id"], parent_id=source["active_version_id"])
                else:
                    version_id = await _generate(task, source, repair_mode, incident)
                await validate_activate(version_id, fence=lambda: fenced(task))
                enqueue("collect", source_id=source["id"])
            else:
                raise ValueError("Unsupported employer task kind")
        fenced(task)
        _finish(task, "completed")
    except RuntimeUnavailable as exc:
        if task["source_id"]:
            with get_session() as session:
                source = session.get(EmployerSource, task["source_id"])
                source.enabled, source.health, source.last_error = False, "blocked", str(exc)
        _finish(task, "needs_review", str(exc))
    except StaleTaskError:
        return
    except BudgetUnavailable as exc:
        _finish(task, "budget_wait", str(exc))
    except (httpx.TransportError, httpx.HTTPStatusError, TimeoutError) as exc:
        delay = min(14400, 900 * 4 ** min(task["attempts"] - 1, 2))
        if task["source_id"]:
            with get_session() as session:
                source = session.get(EmployerSource, task["source_id"])
                source.health = "degraded"
                source.last_error = (
                    f"Transient portal failure ({type(exc).__name__}); retry scheduled"
                )
        _finish(task, "queued", f"Transient failure: {type(exc).__name__}", delay)
    except PolicyError as exc:
        with get_session() as session:
            if task["source_id"]:
                source = session.get(EmployerSource, task["source_id"])
                source.enabled, source.health, source.last_error = False, "blocked", str(exc)
        _finish(task, "needs_review", str(exc))
    except asyncio.CancelledError:
        _finish(task, "queued", "Task interrupted; will resume", 60)
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:2000]
        repair = False
        with get_session() as session:
            if task["source_id"]:
                source = session.get(EmployerSource, task["source_id"])
                source.health, source.last_error = "degraded", error
                source.next_due_at = datetime.utcnow() + timedelta(hours=get_config().poll_hours)
                incident = session.scalar(
                    select(ConnectorIncident).where(
                        ConnectorIncident.source_id == source.id,
                        ConnectorIncident.status.in_(["open", "needs_review"]),
                    )
                )
                if not incident:
                    incident = ConnectorIncident(
                        source_id=source.id,
                        category="structural",
                        details_json=json.dumps({"error": error, "task_id": task["id"]}),
                    )
                    session.add(incident)
                    session.flush()
                if incident.failed_candidates >= 2:
                    incident.status = "needs_review"
                    if not source.active_version_id:
                        source.health = "blocked"
                repair = incident.status == "open" and incident.failed_candidates < 2
            if task["employer_id"]:
                session.get(Employer, task["employer_id"]).resolution_status = "needs_review"
        _finish(task, "failed", error)
        if repair:
            enqueue("repair", source_id=task["source_id"])
    finally:
        fence_context.reset(token)
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


async def dispatch():
    config = get_config()
    if not config.enabled:
        return
    from employers import artifacts

    artifacts.cleanup()
    now = datetime.utcnow()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with get_session() as session:
        discovered_today = session.scalar(
            select(EmployerTask.id).where(
                EmployerTask.kind == "discover", EmployerTask.created_at >= day
            )
        )
        sources = session.scalars(
            select(EmployerSource).where(
                EmployerSource.enabled.is_(True),
                EmployerSource.next_due_at <= now,
                (EmployerSource.health != "blocked")
                | EmployerSource.active_version_id.is_not(None),
            )
        ).all()
        source_work = [(s.id, "collect" if s.active_version_id else "onboard") for s in sources]
        today = (
            session.query(EmployerTask)
            .filter(EmployerTask.kind == "resolve", EmployerTask.created_at >= day)
            .count()
        )
        employers = session.scalars(
            select(Employer)
            .where(Employer.resolution_status == "discovered")
            .where((Employer.classification != "agency") | get_config().include_agencies)
            .order_by(Employer.last_seen_at.desc())
            .limit(max(0, config.onboarding_per_day - today))
        ).all()
        employer_ids = [e.id for e in employers]
        # Keep one daily slot for the oldest unseen employer, even when recent
        # board observations continually replenish the front of the queue.
        if config.onboarding_per_day > today and employers:
            oldest = session.scalar(
                select(Employer)
                .where(Employer.resolution_status == "discovered")
                .where((Employer.classification != "agency") | config.include_agencies)
                .order_by(Employer.created_at, Employer.id)
                .limit(1)
            )
            if oldest and oldest.id not in employer_ids:
                employer_ids[-1] = oldest.id
    if not discovered_today:
        enqueue("discover")
    for source_id, kind in source_work:
        enqueue(kind, source_id=source_id)
    for employer_id in employer_ids:
        enqueue("resolve", employer_id=employer_id)
    # Reconsider daily budget waits without altering incident attempt counters.
    with get_session() as session:
        if config.daily_budget_usd:
            session.execute(
                update(EmployerTask)
                .where(EmployerTask.status == "budget_wait", EmployerTask.due_at < day)
                .values(status="queued", due_at=now)
            )
    while len(_running) < config.concurrency:
        busy_onboarding = any(w.get_name() in {"resolve", "onboard", "repair"} for w in _running)
        task = claim(
            allowed_kinds={"collect", "health", "discover", "rollback", "revalidate"}
            if busy_onboarding
            else None
        )
        if not task:
            break
        worker = asyncio.create_task(execute_task(task))
        worker.set_name(task["kind"])
        _running.add(worker)
        worker.add_done_callback(_running.discard)


async def stop():
    for task in list(_running):
        task.cancel()
    await asyncio.gather(*list(_running), return_exceptions=True)
