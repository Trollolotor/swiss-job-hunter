"""Tests for deduplication logic."""
import pytest
from db.models import Job
from dedup.exact import _normalize


def test_normalize_lowercases():
    assert _normalize("Senior ML Engineer") == "senior ml engineer"


def test_normalize_strips_punctuation():
    assert _normalize("C++ Developer") == "c developer"


def test_dedup_hash_stable():
    h1 = Job.make_dedup_hash("senior ml engineer", "google", "zürich")
    h2 = Job.make_dedup_hash("senior ml engineer", "google", "zürich")
    assert h1 == h2


def test_dedup_hash_different_company():
    h1 = Job.make_dedup_hash("ml engineer", "google", "zürich")
    h2 = Job.make_dedup_hash("ml engineer", "microsoft", "zürich")
    assert h1 != h2


def test_scorer_fast_empty_jd():
    from analyzer.scorer import fast_score
    result = fast_score("Python developer with PyTorch", "")
    assert result.score == 0.0  # too short → unknown


def test_scorer_fast_full_match():
    from analyzer.scorer import fast_score
    cv = "Expert in Python, PyTorch, computer vision, radar sensor fusion"
    jd = "Requirements: python, pytorch, computer vision experience needed for this senior role"
    result = fast_score(cv, jd)
    assert 0 < result.score < 1.0  # partial CV keyword coverage is provisional
    assert len(result.matched_skills) >= 2
    assert len(result.missing_skills) == 0
