"""Ashby posting API, preserving rich descriptions, compensation and locations."""

from urllib.parse import quote, urlsplit

from ats_core import Entry, ParsedPosting, Portal, Request
from ats_core.parsing import (
    catalogue,
    html_text,
    identifier,
    json_object,
    locations,
    public_link,
    records,
    required,
    timestamp,
    workplace,
)


def detect(url, html=""):
    p = urlsplit(url)
    tenant = p.path.strip("/").split("/")[0]
    if p.hostname != "jobs.ashbyhq.com" or not tenant or tenant in {"meeting", "b", "api"}:
        return None
    return Portal("ashby", tenant, "https://jobs.ashbyhq.com", f"https://jobs.ashbyhq.com/{tenant}")


def catalogue_request(source, offset=0):
    return Request(
        f"https://api.ashbyhq.com/posting-api/job-board/{quote(source.tenant, safe='')}?includeCompensation=true"
    )


def parse_catalogue(source, text):
    return catalogue(
        records(json_object(text), "jobs"),
        lambda item: Entry(identifier(item.get("id")), public_link(item.get("jobUrl")), item),
    )


def detail_request(source, entry):
    return None


def parse_posting(source, entry, detail=None):
    item = entry.data
    compensation = item.get("compensation") or {}
    return ParsedPosting(
        entry.posting_id,
        required(item.get("title"), "title"),
        source.company or source.tenant,
        entry.detail_url,
        description=html_text(item.get("descriptionHtml") or item.get("descriptionPlain")),
        locations=locations(
            item.get("address") or item.get("location"), item.get("secondaryLocations")
        ),
        application_url=public_link(item.get("applyUrl"))
        if item.get("applyUrl")
        else entry.detail_url,
        employment_type=item.get("employmentType"),
        remote_ok=workplace(item.get("workplaceType") or item.get("isRemote")),
        posted_at=timestamp(item.get("publishedAt")),
        salary_raw=compensation.get("compensationTierSummary")
        or compensation.get("scrapeableCompensationSalarySummary"),
        extra={
            "compensation": compensation,
            "department": item.get("department"),
            "team": item.get("team"),
            "workplace_type": item.get("workplaceType"),
        },
    )
