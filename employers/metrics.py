"""Measured source value with explicit onboarding and observation baselines."""

import json
from datetime import datetime, timedelta

from sqlalchemy import select

from db.models import (
    BoardSearchRun,
    BudgetLedger,
    ConnectorIncident,
    EmployerSource,
    SearchProfile,
    EmployerTask,
    JobProfile,
    JobSource,
    SourceRun,
)


def source_metrics(session):
    from analyzer.config import get_config

    now = datetime.utcnow()
    since = now - timedelta(days=30)
    threshold = get_config().shortlist_threshold
    board_ids = select(JobSource.job_id).where(JobSource.source_id.is_(None))
    relevant_ids = select(JobProfile.job_id).where(
        JobProfile.profile_id.in_(select(SearchProfile.id).where(SearchProfile.active.is_(True))),
        JobProfile.score_method == "llm",
        JobProfile.score_stale.is_(False),
        JobProfile.match_score >= threshold,
    )
    result = []
    for source in session.scalars(select(EmployerSource)):
        runs = session.scalars(
            select(SourceRun).where(
                SourceRun.source_id == source.id,
                SourceRun.validation.is_(False),
                SourceRun.started_at >= since,
            )
        ).all()
        complete = [r for r in runs if r.outcome in {"complete", "empty"}]
        postings = session.scalars(select(JobSource).where(JobSource.source_id == source.id)).all()
        new = [
            p
            for p in postings
            if not p.historical
            and source.baseline_at
            and p.first_seen_at
            and p.first_seen_at > source.baseline_at
            and p.job_id
        ]
        eligible = {p.job_id for p in new}
        relevant = set(session.scalars(relevant_ids.where(JobProfile.job_id.in_(eligible))))
        board_seen = set(session.scalars(board_ids.where(JobSource.job_id.in_(eligible))))
        additional = relevant - board_seen
        task_ids = select(EmployerTask.id).where(
            (EmployerTask.source_id == source.id) | (EmployerTask.employer_id == source.employer_id)
        )
        costs = session.scalars(
            select(BudgetLedger).where(
                BudgetLedger.task_id.in_(task_ids),
                BudgetLedger.category == "employer",
                BudgetLedger.created_at >= since,
            )
        ).all()
        cost = sum(c.actual_usd if c.actual_usd is not None else c.reserved_usd for c in costs)
        stats = [json.loads(r.stats_json or "{}") for r in runs]
        enough = bool(
            source.baseline_at
            and now - source.baseline_at >= timedelta(days=7)
            and len(complete) >= 10
        )
        # Suggestions are transparent and inert until selected in Settings.
        suggestion = (2 if len(additional) >= 3 else 4 if not new else 3) if enough else None
        incident_rows = session.scalars(
            select(ConnectorIncident).where(
                ConnectorIncident.source_id == source.id, ConnectorIncident.created_at >= since
            )
        ).all()
        timing = []
        for posting in new[:20]:
            boards = session.scalars(
                select(JobSource).where(
                    JobSource.job_id == posting.job_id,
                    JobSource.source_id.is_(None),
                    JobSource.historical.is_(False),
                    JobSource.first_seen_at.is_not(None),
                )
            ).all()
            for board in boards:
                coverage = session.scalar(
                    select(BoardSearchRun)
                    .where(
                        BoardSearchRun.source == board.namespace.removeprefix("board:"),
                        BoardSearchRun.started_at
                        <= min(posting.first_seen_at, board.first_seen_at),
                        BoardSearchRun.started_at >= since,
                    )
                    .order_by(BoardSearchRun.started_at.desc())
                )
                if coverage:
                    timing.append(
                        {
                            "job_id": posting.job_id,
                            "direct_first_seen_at": posting.first_seen_at.isoformat(),
                            "board_first_seen_at": board.first_seen_at.isoformat(),
                            "observation_gap_seconds": (
                                board.first_seen_at - posting.first_seen_at
                            ).total_seconds(),
                            "board_search_run_id": coverage.id,
                            "board_search_outcome": coverage.outcome,
                            "board_keyword": coverage.keyword,
                            "board_location": coverage.location,
                            "direct_poll_hours": source.poll_hours,
                            "note": "Linked source observations; gap includes polling/search coverage and does not establish publication time",
                        }
                    )
        result.append(
            {
                "source_id": source.id,
                "employer_id": source.employer_id,
                "baseline_at": source.baseline_at.isoformat() if source.baseline_at else None,
                "observed_after_baseline": len({p.job_id for p in new}),
                "additional_relevant_vacancies": len(additional),
                "runs": {
                    state: sum(r.outcome == state for r in runs)
                    for state in ("complete", "empty", "partial", "failed")
                },
                "requests": sum(v.get("requests", 0) for v in stats),
                "bytes": sum(v.get("bytes", 0) for v in stats),
                "runtime_seconds": sum(
                    (r.finished_at - r.started_at).total_seconds() for r in runs if r.finished_at
                ),
                "repair_incidents": len(incident_rows),
                "repair_attempts": sum(r.failed_candidates for r in incident_rows),
                "repair_resolved": sum(r.status == "resolved" for r in incident_rows),
                "cost_upper_bound_usd": cost,
                "cost_per_additional_relevant_upper_bound_usd": cost / len(additional)
                if additional
                else None,
                "poll_hours": source.poll_hours,
                "suggested_poll_hours": suggestion,
                "suggestion_reason": "At least 7 days and 10 complete runs; based on observed yield"
                if enough
                else "Waiting for 7 days and 10 complete runs",
                "scope_note": "Not yet observed on our monitored boards; initial backfills excluded",
                "timing_samples": timing[:20],
            }
        )
    board_runs = session.scalars(
        select(BoardSearchRun)
        .where(BoardSearchRun.started_at >= since)
        .order_by(BoardSearchRun.id.desc())
        .limit(100)
    ).all()
    return {
        "sources": result,
        "board_search_coverage": [
            {
                "source": r.source,
                "keyword": r.keyword,
                "location": r.location,
                "outcome": r.outcome,
                "started_at": r.started_at.isoformat(),
            }
            for r in board_runs
        ],
        "window_days": 30,
        "relevant_threshold": threshold,
    }
