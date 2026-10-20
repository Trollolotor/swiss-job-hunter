#!/usr/bin/env python3
"""Explicit schema-fault probe for a real generated Workday connector.

The controlled probe renames a description field in fetched evidence. It cannot
ingest or close vacancies. Repair remains a separately claimed, budgeted task,
using the unchanged validator and two real portal collections before promotion.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-id", type=int, required=True)
    parser.add_argument("--action", choices=["inject", "verify"], required=True)
    args = parser.parse_args()
    from sqlalchemy import select
    from db.models import (
        ConnectorIncident,
        ConnectorVersion,
        EmployerSource,
        EmployerTask,
        JobSource,
    )
    from db.session import get_session
    from employers import service, tasks
    from employers.config import get_config
    from employers.network import CollectionContext

    if get_config().enabled:
        raise SystemExit("Disable automatic employer dispatch for the explicit fault probe")
    with get_session() as session:
        source = session.get(EmployerSource, args.source_id)
        version = session.get(ConnectorVersion, source.active_version_id) if source else None
        if not version or version.kind != "generated" or source.platform != "workday":
            raise SystemExit("A genuinely generated and active Workday version is required")
        version_id = version.id
        before = [
            (r.id, r.missing_checks, r.availability)
            for r in session.scalars(
                select(JobSource).where(JobSource.source_id == source.id).order_by(JobSource.id)
            )
        ]

    class FaultContext(CollectionContext):
        async def fetch(self, *arguments, **keywords):
            observation = await super().fetch(*arguments, **keywords)
            try:
                data = observation.json()
            except (ValueError, TypeError):
                return observation
            info = data.get("jobPostingInfo") if isinstance(data, dict) else None
            if isinstance(info, dict) and "jobDescription" in info:
                info["description"] = info.pop("jobDescription")
                observation.text = json.dumps(data)
            return observation

    if args.action == "inject":
        original = tasks.run_version

        async def probe(version_id, **kwargs):
            # Force live details, disable persistence and bypass routine caches.
            return await service.run_version(
                version_id, validation=True, context_factory=FaultContext, **kwargs
            )

        tasks.run_version = probe
        tid = tasks.enqueue(
            "health", source_id=args.source_id, payload={"probe": "description-schema-fault"}
        )
        task = tasks.claim(tid)
        if not task:
            raise SystemExit("Probe task cannot be claimed")
        try:
            await tasks.execute_task(task)
        finally:
            tasks.run_version = original
        with get_session() as session:
            row = session.get(EmployerTask, tid)
            incident = session.scalar(
                select(ConnectorIncident).where(
                    ConnectorIncident.source_id == args.source_id,
                    ConnectorIncident.status == "open",
                )
            )
            if row.status != "failed" or not incident:
                raise SystemExit(
                    "Fault did not trigger a structural incident; no repair acceptance claimed"
                )
            details = json.loads(incident.details_json)
            details["controlled_probe"] = (
                "Fetched Workday jobDescription renamed to description; real website and immutable connector unchanged"
            )
            incident.details_json = json.dumps(details)
            print(
                json.dumps(
                    {
                        "stage": "fault incident",
                        "task_id": tid,
                        "incident_id": incident.id,
                        "version_id": version_id,
                        "error": row.error,
                    }
                ),
                flush=True,
            )
    else:
        result = await service.run_version(
            version_id, validation=True, context_factory=FaultContext
        )
        if result["outcome"] not in {"complete", "empty"}:
            raise SystemExit("Repaired connector did not pass the changed-schema probe")
        print(
            json.dumps(
                {
                    "stage": "repaired schema probe",
                    "version_id": version_id,
                    "outcome": result["outcome"],
                    "count": result["observed_count"],
                }
            ),
            flush=True,
        )
    with get_session() as session:
        after = [
            (r.id, r.missing_checks, r.availability)
            for r in session.scalars(
                select(JobSource)
                .where(JobSource.source_id == args.source_id)
                .order_by(JobSource.id)
            )
        ]
    if before != after:
        raise SystemExit("Controlled probe changed source availability")
    print(json.dumps({"availability_preserved": True}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
