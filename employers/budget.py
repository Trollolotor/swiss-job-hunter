"""Transactional reservations. Unknown/failed requests retain their reservation."""

import json
import math
from datetime import datetime

from sqlalchemy import select

from db.models import BudgetLedger
from db.session import engine, get_session
from employers.config import get_config


class BudgetUnavailable(RuntimeError):
    pass


def reserve(operation, model, prompt, max_tokens, task_id=None, search=False):
    config = get_config()
    if not config.daily_budget_usd:
        raise BudgetUnavailable(
            "Set the employer-source daily budget in Settings before onboarding"
        )
    prices = config.prices.get(model, {})
    if not all(
        k in prices and math.isfinite(prices[k]) and prices[k] >= 0
        for k in ("input_per_million", "output_per_million")
    ):
        raise BudgetUnavailable(f"Configure conservative input/output prices for {model}")
    # UTF-8 bytes upper-bound typical tokenization; include tool/schema overhead.
    estimated = (
        (len(prompt.encode()) + 16000 + (80000 if search else 0)) * prices["input_per_million"]
        + max_tokens * prices["output_per_million"]
    ) / 1_000_000
    if search:
        if (
            "search_request" not in prices
            or not math.isfinite(prices["search_request"])
            or prices["search_request"] < 0
        ):
            raise BudgetUnavailable("Configure search_request price before employer search")
        estimated += 2 * prices["search_request"]
    if estimated > config.max_task_usd:
        raise BudgetUnavailable("Request exceeds configured per-attempt cost limit")
    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    with engine.connect() as conn:
        # IMMEDIATE serializes read/reserve so concurrent calls cannot overspend.
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        rows = conn.execute(
            select(
                BudgetLedger.operation, BudgetLedger.reserved_usd, BudgetLedger.actual_usd
            ).where(BudgetLedger.created_at >= start, BudgetLedger.category == "employer")
        ).all()
        total = sum(r.actual_usd if r.actual_usd is not None else r.reserved_usd for r in rows)
        repair = operation == "connector_repair"
        category = sum(
            r.actual_usd if r.actual_usd is not None else r.reserved_usd
            for r in rows
            if (r.operation == "connector_repair") == repair
        )
        fraction = config.repair_budget_fraction if repair else 1 - config.repair_budget_fraction
        if (
            total + estimated > config.daily_budget_usd
            or category + estimated > config.daily_budget_usd * fraction
        ):
            conn.rollback()
            raise BudgetUnavailable("Employer-source daily budget exhausted")
        result = conn.execute(
            BudgetLedger.__table__.insert().values(
                operation=operation,
                model=model,
                task_id=task_id,
                reserved_usd=estimated,
                status="reserved",
                created_at=datetime.utcnow(),
            )
        )
        identifier = result.inserted_primary_key[0]
        conn.commit()
        return identifier


def settle(identifier, usage, metadata):
    with get_session() as session:
        row = session.get(BudgetLedger, identifier)
        row.metadata_json = json.dumps({"usage": usage, **metadata}, default=str)
        # Provider total includes search where available. Retain a conservative
        # reservation when billing metadata is absent rather than assuming zero.
        value = usage.get("cost")
        if (not metadata.get("search_enabled") and isinstance(value, (float, int))
                and math.isfinite(value) and value >= 0):
            row.actual_usd = value
        row.status = "settled"


def fail(identifier, error):
    with get_session() as session:
        row = session.get(BudgetLedger, identifier)
        metadata = json.loads(row.metadata_json or "{}")
        metadata["error"] = f"{type(error).__name__}: {error}"[:1000]
        row.metadata_json, row.status = json.dumps(metadata), "failed"
