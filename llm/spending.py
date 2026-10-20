"""Matching reservations at the billable attempt boundary, including JSON repair."""

import json
import math
from contextvars import ContextVar
from datetime import datetime

from sqlalchemy import select

context = ContextVar("matching_context", default={})
response_metadata = ContextVar("llm_response_metadata", default={})
OPERATIONS = {"job_screening", "keyword_extraction"}


class SpendingUnavailable(RuntimeError):
    pass


def remember(response):
    usage = getattr(response, "usage", None)
    response_metadata.set(
        {
            "usage": usage.model_dump() if hasattr(usage, "model_dump") else {},
            "request_id": getattr(response, "id", None),
        }
    )


def reserve(operation, model, prompt, max_tokens, *, free=False):
    if operation not in OPERATIONS or free:
        return None
    from analyzer.config import get_config
    from db.models import BudgetLedger
    from db.session import engine

    config = get_config()
    prices = config.prices.get(model, {})
    if not config.daily_budget_usd:
        raise SpendingUnavailable("Configure a separate matching spending cap in Settings")
    if not all(
        isinstance(prices.get(k), (int, float)) and math.isfinite(prices[k]) and prices[k] >= 0
        for k in ("input_per_million", "output_per_million")
    ):
        raise SpendingUnavailable(f"Configure conservative matching prices for {model}")
    estimate = (
        (len(prompt.encode()) + 16000) * prices["input_per_million"]
        + max_tokens * prices["output_per_million"]
    ) / 1e6
    if estimate > config.max_attempt_usd:
        raise SpendingUnavailable("Matching attempt exceeds its spending limit")
    day = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    with engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        rows = conn.execute(
            select(BudgetLedger.reserved_usd, BudgetLedger.actual_usd).where(
                BudgetLedger.category == "matching", BudgetLedger.created_at >= day
            )
        ).all()
        total = sum(r.actual_usd if r.actual_usd is not None else r.reserved_usd for r in rows)
        if total + estimate > config.daily_budget_usd:
            conn.rollback()
            raise SpendingUnavailable("Matching daily budget exhausted")
        result = conn.execute(
            BudgetLedger.__table__.insert().values(
                operation=operation,
                category="matching",
                model=model,
                reserved_usd=estimate,
                status="reserved",
                context_json=json.dumps(context.get()),
                created_at=datetime.utcnow(),
            )
        )
        conn.commit()
        return result.inserted_primary_key[0]


def settle(identifier, metadata=None, *, error=None, cache_hit=False, operation=None, model=None):
    from db.models import BudgetLedger
    from db.session import get_session

    if identifier is None and not (cache_hit and operation in OPERATIONS):
        return
    value = dict(metadata or {})
    if error:
        value["error"] = str(error)[:500]
    with get_session() as session:
        row = (
            session.get(BudgetLedger, identifier)
            if identifier
            else BudgetLedger(
                category="matching",
                operation=operation,
                model=model,
                reserved_usd=0,
                actual_usd=0,
                status="cached",
                context_json=json.dumps(context.get()),
            )
        )
        if not identifier:
            session.add(row)
        row.metadata_json = json.dumps({**value, "cache_hit": cache_hit}, default=str)
        cost = value.get("usage", {}).get("cost")
        if isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0:
            row.actual_usd = cost
        if identifier:
            row.status = "failed" if error else "settled"
