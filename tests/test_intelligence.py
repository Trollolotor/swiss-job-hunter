from datetime import datetime, timezone

import pytest


def test_prompt_defaults_have_all_required_placeholders():
    from llm.prompt_manager import DEFINITIONS, _default_user, validate_prompt
    for key, definition in DEFINITIONS.items():
        value = validate_prompt(key, {
            "system_template": definition.system,
            "user_template": _default_user(definition),
            "temperature": definition.temperature,
            "max_tokens": definition.max_tokens,
        })
        assert value["required_variables"] == list(definition.variables)


def test_prompt_rejects_missing_or_unknown_variables():
    from llm.prompt_manager import validate_prompt
    with pytest.raises(ValueError, match="missing required placeholders"):
        validate_prompt("cv_parsing", {"system_template": "system", "user_template": "no CV"})
    with pytest.raises(ValueError, match="unknown placeholders"):
        validate_prompt("cv_parsing", {
            "system_template": "system", "user_template": "{cv_text} {secret}"})


def test_company_name_normalization():
    from company_service import normalize_company_name
    assert normalize_company_name("PwC AG") == "pwc"
    assert normalize_company_name(" Example  GmbH ") == "example"


def test_publication_extraction_precedence_and_provenance():
    from analyzer.publication import extract_publication_from_html
    html = """
      <html><head><meta property="article:published_time" content="2026-06-20T10:00:00+02:00">
      <script type="application/ld+json">{"@type":"JobPosting","datePosted":"2026-06-21"}</script>
      </head><body><time datetime="2026-06-19">old</time></body></html>
    """
    result = extract_publication_from_html(html)
    assert result.source == "json_ld"
    assert result.confidence == .95
    assert result.value == datetime(2026, 6, 21)


def test_publication_relative_text():
    from analyzer.publication import extract_publication_from_html
    result = extract_publication_from_html("<div class='posted'>3 days ago</div>")
    assert result.source == "relative_text"
    assert result.value is not None


def test_automation_defaults_and_validation():
    from config.runtime import DEFAULT_AUTOMATION_CONFIG, validate_automation_config
    config = validate_automation_config(DEFAULT_AUTOMATION_CONFIG)
    assert config["timezone"] == "Europe/Zurich"
    assert config["weekdays"] == [0, 1, 2, 3, 4]
    assert (config["start_hour"], config["end_hour"], config["interval_minutes"]) == (7, 22, 30)
    assert config["screen_limit_per_profile"] == 20
    assert config["date_backfill_limit"] == 20


def test_automation_groups_duplicate_profile_searches():
    from automation import group_profile_searches
    profiles = [
        (1, "base", "Base", "", ["AI Engineer", "Python"], "cv1"),
        (2, "variant", "Variant", "", ["ai engineer"], "cv2"),
    ]
    groups = group_profile_searches(profiles)
    assert len(groups) == 2
    ai = next(group for group in groups if group["keyword"].casefold() == "ai engineer")
    assert ai["profiles"] == [(1, "base"), (2, "variant")]


def test_scraped_job_populates_date_metadata():
    from scrapers.base import ScrapedJob
    job = ScrapedJob(title="x", company="y", location="z", description="", url="u",
                     source="test", posted_at=datetime.now(timezone.utc),
                     posted_at_source="api")
    assert job.posted_at_raw
    assert job.posted_at_confidence == 1.0


def test_vacancy_archives_only_after_two_confirmations():
    from db.models import JobStatus
    from job_lifecycle import mark_unavailable
    job = type("Job", (), {"status": JobStatus.NEW, "unavailable_checks": 0,
                            "expired_at": None, "expired_reason": None,
                            "availability_checked_at": None})()
    assert mark_unavailable(job, "404") is False
    assert job.status == JobStatus.NEW
    assert mark_unavailable(job, "404") is True
    assert job.status == JobStatus.ARCHIVED
    assert job.expired_at is not None


def test_active_application_is_flagged_but_not_archived():
    from db.models import JobStatus
    from job_lifecycle import mark_unavailable
    job = type("Job", (), {"status": JobStatus.APPLIED, "unavailable_checks": 1,
                            "expired_at": None, "expired_reason": None,
                            "availability_checked_at": None})()
    assert mark_unavailable(job, "closed") is True
    assert job.status == JobStatus.APPLIED
    assert job.expired_at is not None
