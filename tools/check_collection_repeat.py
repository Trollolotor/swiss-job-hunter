#!/usr/bin/env python3
"""Repeat real collections and verify stable identities/history with zero LLM calls."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def snapshot(source_ids):
    from sqlalchemy import func, select
    from db.models import BudgetLedger, Job, JobProfile, JobSource, LLMCallLog
    from db.session import get_session

    with get_session() as session:
        rows = session.execute(
            select(JobSource, Job)
            .join(Job, Job.id == JobSource.job_id)
            .where(JobSource.source_id.in_(source_ids))
        ).all()
        identities = {
            json.dumps([r.source_id, r.posting_id, r.variant]): [
                r.id,
                j.id,
                j.dedup_hash,
                j.status.value,
                j.status_origin,
                j.user_stars,
            ]
            for r, j in rows
        }
        return {
            "identities": identities,
            "profile_links": session.scalar(
                select(func.count())
                .select_from(JobProfile)
                .where(JobProfile.job_id.in_({j.id for _, j in rows}))
            ),
            "llm_calls": session.scalar(select(func.count()).select_from(LLMCallLog)),
            "ledger_rows": session.scalar(select(func.count()).select_from(BudgetLedger)),
        }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", type=int, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from db.models import EmployerTask, SourceRun
    from db.session import get_session
    from employers.config import get_config
    from employers.tasks import claim, enqueue, execute_task

    if get_config().enabled:
        raise SystemExit("Disable automatic employer work during this explicit acceptance check")
    before = snapshot(args.sources)
    runs = []
    for source_id in args.sources:
        tid = enqueue("collect", source_id=source_id)
        task = claim(tid)
        if not task:
            raise SystemExit("Collection is already leased or is waiting for retry")
        await execute_task(task)
        with get_session() as session:
            task = session.get(EmployerTask, tid)
            if task.status != "completed":
                raise SystemExit(f"Collection task {tid}: {task.status}: {task.error}")
            run = (
                session.query(SourceRun)
                .filter_by(source_id=source_id, validation=False)
                .order_by(SourceRun.id.desc())
                .first()
            )
            stats = json.loads(run.stats_json)
            runs.append(
                {
                    "id": run.id,
                    "source_id": source_id,
                    "version_id": run.version_id,
                    "outcome": run.outcome,
                    "count": stats.get("observed_count"),
                    "requests": stats.get("requests"),
                    "cache_hits": stats.get("cache_hits"),
                }
            )
    after = snapshot(args.sources)
    stable = all(
        after["identities"].get(key) == value for key, value in before["identities"].items()
    )
    result = {
        "runs": runs,
        "stable_existing_source_and_job_ids_and_history": stable,
        "source_postings": len(after["identities"]),
        "profile_links": after["profile_links"],
        "llm_calls": after["llm_calls"] - before["llm_calls"],
        "ledger_rows": after["ledger_rows"] - before["ledger_rows"],
        "snapshot": after,
    }
    fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(result, stream)
    print(
        json.dumps({key: value for key, value in result.items() if key != "snapshot"}), flush=True
    )
    if not stable or result["llm_calls"] or result["ledger_rows"]:
        raise SystemExit("Repeated collection acceptance failed")


if __name__ == "__main__":
    asyncio.run(main())
