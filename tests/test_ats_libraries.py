"""Offline public-protocol fixtures; no LLM calls or real employer data."""

import copy
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from ats_core import SchemaError
from ats_vendors.registry import VENDORS, detect, manifest, vendor, wrapper
from employers.contracts import CollectionResult
from employers.network import Observation
from employers.validation import assess, complete_description, posting_errors

FIXTURES = Path(__file__).parent / "fixtures" / "ats"


def fixture(name):
    data = json.loads((FIXTURES / (name + ".json")).read_text())
    source = detect(data["portal"], data.get("html", ""))
    assert source is not None
    return replace(source, company="Acme"), data


class FixtureSDK:
    def __init__(self, source, data):
        self.source, self.data, self.observations, self.calls = source, data, [], []

    def fetch(self, url, method="GET", body=None):
        self.calls.append((url, method, body))
        request = vendor(self.source.vendor).catalogue_request(
            self.source, (body or {}).get("offset", 0)
        )
        data = (
            self.data["catalogue"]
            if (url, method, body) == (request.url, request.method, request.body)
            else self.data.get("detail")
        )
        if data is None:
            raise AssertionError(f"Unexpected request: {url}")
        text = data if isinstance(data, str) else json.dumps(data)
        obs = Observation(url, method, 200, text, {"content-type": "application/json"}, body)
        self.observations.append(obs)
        return {
            "url": url,
            "method": method,
            "status": 200,
            "text": text,
            "key": obs.key,
            "headers": obs.headers,
        }


def run(source, data, **config):
    scope = {}
    exec(wrapper(source.vendor), scope)  # noqa: S102 -- exercise reviewed static SDK wrapper
    sdk = FixtureSDK(source, data)
    cfg = {**source.config(), **config}
    catalogue = scope["enumerate"](cfg, sdk)
    postings = []
    for entry in catalogue["entries"]:
        raw = scope["fetch_detail"](cfg, entry, sdk)
        postings.append(scope["normalize"](cfg, entry, raw))
    result = CollectionResult.model_validate(
        {k: v for k, v in catalogue.items() if k != "entries"} | {"postings": postings}
    )
    return result, sdk


@pytest.mark.parametrize("name", [*VENDORS, "successfactors_legacy"])
def test_ready_protocol_normalizes_full_posting_and_validates(name):
    source, data = fixture(name)
    result, sdk = run(source, data)
    report = assess(result, sdk.observations, manifest(source))
    assert report["outcome"] == "complete", report
    assert len(result.postings) == 1
    posting = result.postings[0]
    assert posting.job.title == "Platform Engineer"
    assert "Python" in posting.job.description and "SQL" in posting.job.description
    assert any(loc.country == "CH" for loc in posting.locations)
    assert not posting.remote_countries  # Remote does not establish Swiss eligibility.
    assert len(sdk.calls) == (2 if name == "workday" else 1)
    assert complete_description(posting, sdk.observations, manifest(source))
    if name == "successfactors":
        assert posting.job.posted_at is None  # An expiration date is not publication.
    if name == "successfactors_legacy":
        assert posting.job.posted_at.month == 10 and posting.job.posted_at.day == 1
    if name in {"lever", "ashby", "workday", "teamtailor"}:
        assert posting.job.remote_ok is None  # Preserve hybrid uncertainty.


@pytest.mark.parametrize("name", VENDORS)
def test_schema_changes_are_not_reported_as_zero_jobs(name):
    source, data = fixture(name)
    data["catalogue"] = "<html><body>Maintenance</body></html>"
    with pytest.raises(SchemaError):
        run(source, data)


@pytest.mark.parametrize("name", VENDORS)
def test_explicit_empty_catalogues_have_independent_evidence(name):
    source, data = fixture(name)
    if name in {"successfactors", "teamtailor"}:
        data["catalogue"] = "<rss><channel><title>Acme</title></channel></rss>"
    else:
        key = {
            "workable": "jobs",
            "greenhouse": "jobs",
            "ashby": "jobs",
            "recruitee": "offers",
            "workday": "jobPostings",
        }.get(name)
        data["catalogue"] = {key: []} if key else []
        if name == "workday":
            data["catalogue"]["total"] = 0
    result, sdk = run(source, data)
    assert assess(result, sdk.observations, manifest(source))["outcome"] == "empty"


@pytest.mark.parametrize("name", VENDORS)
def test_evidence_is_specific_to_the_posting_not_any_text_in_the_feed(name):
    source, data = fixture(name)
    result, sdk = run(source, data)
    posting = result.postings[0]
    original = posting.job.description
    posting.job.description = "Another posting's requirements"
    assert posting_errors(posting, sdk.observations, manifest(source))
    assert not complete_description(posting, sdk.observations, manifest(source))
    posting.job.description = original
    posting.job.remote_ok = not posting.job.remote_ok
    assert any(
        "Remote" in error for error in posting_errors(posting, sdk.observations, manifest(source))
    )


def test_workable_markdown_fallback_and_no_silent_description_truncation():
    source, data = fixture("workable")
    data["catalogue"]["jobs"][0].pop("description")
    data["detail"] = (
        "# Platform Engineer\n\n## Requirements\nPython and SQL\n" + "Full source text. " * 2000
    )
    result, sdk = run(source, data)
    assert result.postings[0].job.description == data["detail"].strip()
    assert len(sdk.calls) == 2 and sdk.calls[-1][0].endswith("/jobs/view/ABC123.md")
    assert assess(result, sdk.observations, manifest(source))["outcome"] == "complete"


def test_workable_duplicate_locations_merge_but_conflicting_content_is_partial():
    source, data = fixture("workable")
    other = copy.deepcopy(data["catalogue"]["jobs"][0])
    other["locations"] = [{"city": "Geneva", "country": "CH"}]
    data["catalogue"]["jobs"].append(other)
    result, sdk = run(source, data)
    assert len(result.postings) == 1 and len(result.postings[0].locations) == 3
    assert assess(result, sdk.observations, manifest(source))["outcome"] == "complete"
    other["title"] = "Different role"
    result, sdk = run(source, data)
    assert assess(result, sdk.observations, manifest(source))["outcome"] == "partial"


def test_lever_sections_are_not_lost_to_short_plain_description():
    source, data = fixture("lever")
    result, _ = run(source, data)
    assert "Requirements" in result.postings[0].job.description
    assert "Python and SQL" in result.postings[0].job.description


def test_workday_cap_and_changed_totals_cannot_be_complete():
    source, data = fixture("workday")
    data["catalogue"]["total"] = 2000
    result, sdk = run(source, data, max_pages=1)
    assert result.termination == "incomplete"
    assert assess(result, sdk.observations, manifest(source))["outcome"] == "partial"
    data["catalogue"]["total"] = 2
    result, sdk = run(source, data, max_pages=2)
    assert any(
        "Repeated" in error
        for error in assess(result, sdk.observations, manifest(source))["errors"]
    )


@pytest.mark.parametrize("name", ["teamtailor", "successfactors"])
def test_xml_rejects_dtd_and_truncated_documents(name):
    source, data = fixture(name)
    for text in [
        '<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel/></rss>',
        "<rss><channel>",
    ]:
        data["catalogue"] = text
        with pytest.raises(SchemaError):
            run(source, data)


def test_unknown_legacy_template_fields_remain_an_explicit_failure():
    source, data = fixture("successfactors_legacy")
    data["catalogue"] = data["catalogue"].replace("[[City]]", "[[UnknownField]]")
    with pytest.raises(SchemaError, match="Unresolved"):
        run(source, data)


def test_url_matching_is_exact_and_regional_hosts_are_retained():
    assert detect("https://jobs.eu.lever.co/acme").variant == "eu"
    assert detect("https://job-boards.eu.greenhouse.io/acme").variant == "eu"
    assert (
        detect("https://acme.wd3.myworkdayjobs.com/en-US/Careers/job/Bern/R123").board == "Careers"
    )
    for url in [
        "https://apply.workable.com.attacker.test/acme",
        "https://user@jobs.lever.co/acme",
        "https://jobs.lever.co/acme%2fother",
        "https://jobs.lever.co:444/acme",
        "http://jobs.lever.co/acme",
    ]:
        assert detect(url) is None
    assert detect("https://careers.example.test", "<p>We use SuccessFactors</p>") is None


def test_library_revision_mismatch_cannot_run_in_a_stale_image():
    source, data = fixture("ashby")
    with pytest.raises(RuntimeError, match="runtime failed"):
        run(source, data, library_fingerprint="wrong-image")


@pytest.mark.parametrize("name", VENDORS)
def test_missing_description_remains_partial_instead_of_complete(name):
    source, data = fixture(name)
    if name in {"successfactors", "teamtailor"}:
        data["catalogue"] = re.sub(
            r"<description>.*?</description>", "<description/>", data["catalogue"], flags=re.DOTALL
        )
    elif name == "workday":
        data["detail"]["jobPostingInfo"]["jobDescription"] = ""
    elif name == "lever":
        for field in ["description", "descriptionPlain", "additional", "additionalPlain", "lists"]:
            data["catalogue"][0].pop(field, None)
    else:
        item = data["catalogue"]["offers" if name == "recruitee" else "jobs"][0]
        for field in [
            "description",
            "requirements",
            "benefits",
            "descriptionHtml",
            "descriptionPlain",
            "content",
        ]:
            item.pop(field, None)
        data["detail"] = ""
    result, sdk = run(source, data)
    assert not complete_description(result.postings[0], sdk.observations, manifest(source))
    assert assess(result, sdk.observations, manifest(source))["outcome"] == "partial"


def test_creation_and_update_dates_are_not_invented_publication_dates():
    source, data = fixture("lever")
    data["catalogue"][0]["createdAt"] = "2026-10-01T10:00:00Z"
    result, _ = run(source, data)
    assert result.postings[0].job.posted_at is None
    assert result.postings[0].job.extra["source_created_at"] == "2026-10-01T10:00:00Z"
    source, data = fixture("workday")
    info = data["detail"]["jobPostingInfo"]
    info.pop("datePosted", None)
    info["startDate"] = "2026-10-01"
    assert run(source, data)[0].postings[0].job.posted_at is None


def test_inline_feed_validation_reuses_an_item_index_without_trusting_stale_bytes(monkeypatch):
    source, data = fixture("greenhouse")
    template = data["catalogue"]["jobs"][0]
    data["catalogue"]["jobs"] = [
        {**template, "id": n, "title": f"Engineer {n}", "content": f"<p>Python role {n}</p>"}
        for n in range(1, 101)
    ]
    result, sdk = run(source, data)
    protocol, calls = vendor("greenhouse"), []
    original = protocol.parse_catalogue

    def parse(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(protocol, "parse_catalogue", parse)
    spec = manifest(source)
    assert assess(result, sdk.observations, spec)["outcome"] == "complete"
    assert len(calls) == 1  # One feed parse, even when validating hundreds of fields.
    sdk.observations[0].text = '{"jobs": []}'
    assert posting_errors(result.postings[0], sdk.observations, spec)


def test_workday_page_budget_is_a_limitation_rather_than_structural_repair():
    source, data = fixture("workday")
    data["catalogue"]["total"] = 5
    result, sdk = run(source, data, max_pages=1)
    spec = manifest(source)
    spec["catalogue"]["max_pages"] = 1
    report = assess(result, sdk.observations, spec)
    assert report["outcome"] == "partial" and report["limitation"] == "page_limit"
