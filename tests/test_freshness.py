from datetime import datetime, timedelta

import pytest

from analyzer.freshness import freshness, parse_posted_at, priority
from config.runtime import DEFAULT_PRIORITY_CONFIG, validate_priority_config


NOW = datetime(2026, 1, 15, 12, 0, 0)


@pytest.mark.parametrize(
    ("hours", "expected"),
    [(23, 1.0), (24, 1.0), (25, 0.8), (72, 0.8), (73, 0.6),
     (168, 0.6), (169, 0.3), (336, 0.3), (337, 0.0)],
)
def test_freshness_boundaries(hours, expected):
    score, _, _ = freshness(NOW - timedelta(hours=hours), DEFAULT_PRIORITY_CONFIG, NOW)
    assert score == expected


def test_unknown_date_has_no_freshness_bonus():
    assert freshness(None, DEFAULT_PRIORITY_CONFIG, NOW) == (0.0, "UNKNOWN", None)


def test_priority_keeps_match_and_freshness_separate():
    assert priority(0.5, 1.0, DEFAULT_PRIORITY_CONFIG) == 0.6


def test_relative_publication_date():
    parsed, source = parse_posted_at("3 days ago", NOW)
    assert source == "relative_text"
    assert parsed == NOW - timedelta(days=3)


def test_priority_weights_must_sum_to_one():
    with pytest.raises(ValueError):
        validate_priority_config({"match_weight": 0.8, "freshness_weight": 0.3})

