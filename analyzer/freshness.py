"""Publication-date normalization and dynamic job priority."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from dateutil import parser as date_parser


def utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def parse_posted_at(value: Any, now: Optional[datetime] = None) -> tuple[Optional[datetime], str]:
    if isinstance(value, datetime):
        return utc_naive(value), "api"
    text = str(value or "").strip()
    if not text:
        return None, "unknown"
    try:
        return utc_naive(date_parser.parse(text)), "api"
    except (ValueError, TypeError, OverflowError):
        pass
    match = re.search(
        r"(\d+)\s*(minute|hour|day|week|monat|tag|stunde|jour|heure)s?\s*(?:ago|old|zuvor|her|avant)?",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None, "unknown"
    count = int(match.group(1))
    unit = match.group(2).lower()
    hours = count
    if unit.startswith(("minute",)):
        hours = count / 60
    elif unit.startswith(("day", "tag", "jour")):
        hours = count * 24
    elif unit.startswith(("week",)):
        hours = count * 24 * 7
    elif unit.startswith(("monat",)):
        hours = count * 24 * 30
    base = utc_naive(now or datetime.now(timezone.utc))
    return base - timedelta(hours=hours), "relative_text"


def freshness(posted_at: Optional[datetime], config: dict[str, Any], now: Optional[datetime] = None) -> tuple[float, str, Optional[int]]:
    if not posted_at:
        return 0.0, "UNKNOWN", None
    base = utc_naive(now or datetime.now(timezone.utc))
    age_hours = max(0.0, (base - utc_naive(posted_at)).total_seconds() / 3600)
    age_days = int(age_hours // 24)
    for bucket in config["buckets"]:
        if age_hours <= int(bucket["max_hours"]):
            label = "TODAY" if age_hours <= 24 else f"{max(1, age_days)}D"
            return float(bucket["score"]), label, age_days
    return 0.0, "14D+", age_days


def priority(match_score: Optional[float], freshness_score: float, config: dict[str, Any]) -> float:
    return round(
        float(config["match_weight"]) * float(match_score or 0.0)
        + float(config["freshness_weight"]) * freshness_score,
        4,
    )

