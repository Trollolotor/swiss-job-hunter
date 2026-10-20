#!/usr/bin/env python3
"""Explicit, budgeted pilot against stored board evidence; no automatic dispatch.

Run in the backend environment after migration and pricing configuration. This
tool intentionally uses the real durable queue, paid agent and restricted runner.
It never substitutes a fixture connector for provider-generated code.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def report(employer_id):
    from sqlalchemy import func, select

    from db.models import BudgetLedger, EmployerSource, EmployerTask, JobSource, SourceRun
    from db.session import get_session

    with get_session() as session:
        sources = session.scalars(
            select(EmployerSource).where(EmployerSource.employer_id == employer_id)
        ).all()
        ids = [source.id for source in sources]
        tasks = session.scalars(
            select(EmployerTask)
            .where((EmployerTask.employer_id == employer_id) | EmployerTask.source_id.in_(ids))
            .order_by(EmployerTask.id.desc())
            .limit(12)
        ).all()
        summary = {
            "employer_id": employer_id,
            "sources": [
                {
                    "id": source.id,
                    "platform": source.platform,
                    "health": source.health,
                    "version": source.active_version_id,
                    "error": source.last_error,
                }
                for source in sources
            ],
            "tasks": [
                {"id": task.id, "kind": task.kind, "status": task.status, "error": task.error}
                for task in tasks
            ],
            "postings": session.scalar(
                select(func.count()).select_from(JobSource).where(JobSource.source_id.in_(ids))
            ),
            "runs": [
                {
                    "id": run.id,
                    "version": run.version_id,
                    "outcome": run.outcome,
                    "stats": {
                        key: value
                        for key, value in json.loads(run.stats_json).items()
                        if key != "identities"
                    },
                }
                for run in session.scalars(
                    select(SourceRun)
                    .where(SourceRun.source_id.in_(ids))
                    .order_by(SourceRun.id.desc())
                    .limit(3)
                )
            ],
        }
        summary["employer_spend_usd"] = session.scalar(
            select(
                func.coalesce(
                    func.sum(func.coalesce(BudgetLedger.actual_usd, BudgetLedger.reserved_usd)), 0
                )
            ).where(BudgetLedger.category == "employer")
        )
        return summary


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--employer", required=True, help="Exact stored employer label")
    parser.add_argument(
        "--official-candidate",
        help="Public candidate page supplied by the operator; still independently verified",
    )
    parser.add_argument("--action", choices=["resolve", "next", "collect", "status"], required=True)
    args = parser.parse_args()
    from sqlalchemy import select

    from db.models import Employer, EmployerEvidence, EmployerSource, EmployerTask
    from db.session import get_session, init_db
    from employers.config import get_config
    from employers.tasks import claim, enqueue, execute_task

    init_db()
    if args.action != "status" and get_config().enabled:
        raise SystemExit("Disable automatic employer dispatch during the controlled pilot")
    with get_session() as session:
        employer = session.scalar(select(Employer).where(Employer.name == args.employer))
        if not employer:
            raise SystemExit("Employer must first be discovered from stored board evidence")
        employer_id = employer.id
        if args.official_candidate:
            if args.action != "resolve":
                raise SystemExit("Official candidate applies only to resolution")
            from employers.contracts import digest
            from employers.network import public_url

            public_url(args.official_candidate)
            key = digest([employer_id, "operator_candidate", args.official_candidate])
            if not session.scalar(
                select(EmployerEvidence.id).where(EmployerEvidence.evidence_key == key)
            ):
                session.add(
                    EmployerEvidence(
                        employer_id=employer_id,
                        evidence_key=key,
                        kind="operator_candidate",
                        source="operator",
                        url=args.official_candidate,
                        payload_json=json.dumps({"official_url": args.official_candidate}),
                    )
                )
        source_ids = list(
            session.scalars(
                select(EmployerSource.id).where(EmployerSource.employer_id == employer_id)
            )
        )
    task_id = None
    if args.action == "resolve":
        if source_ids:
            raise SystemExit("Portal already resolved; use next or collect")
        task_id = enqueue("resolve", employer_id=employer_id)
    elif args.action == "collect":
        if len(source_ids) != 1:
            raise SystemExit("Pilot expects exactly one connected source")
        task_id = enqueue("collect", source_id=source_ids[0])
    elif args.action == "next":
        with get_session() as session:
            task_id = session.scalar(
                select(EmployerTask.id)
                .where(
                    (
                        (EmployerTask.employer_id == employer_id)
                        | EmployerTask.source_id.in_(source_ids)
                    ),
                    EmployerTask.status == "queued",
                )
                .order_by(EmployerTask.id)
                .limit(1)
            )
        if not task_id:
            raise SystemExit("No queued pilot task; inspect status")
    if task_id:
        task = claim(task_id)
        if task is None:
            raise SystemExit(
                "Task is not claimable; its lease or retry schedule remains authoritative"
            )
        print(
            json.dumps({"stage": "running", "task_id": task_id, "kind": task["kind"]}), flush=True
        )
        await execute_task(task)
    result = report(employer_id)
    print(json.dumps(result, default=str), flush=True)
    if (
        task_id
        and next(task["status"] for task in result["tasks"] if task["id"] == task_id) != "completed"
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
