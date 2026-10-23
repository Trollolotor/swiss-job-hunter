"""Recruitee's public tenant offers API."""

from urllib.parse import urlsplit

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
    if not p.hostname or not p.hostname.endswith(".recruitee.com"):
        return None
    tenant = p.hostname.removesuffix(".recruitee.com")
    if "." in tenant:
        return None
    return Portal("recruitee", tenant, f"https://{p.hostname}", f"https://{p.hostname}")


def catalogue_request(source, offset=0):
    return Request(source.origin + "/api/offers/")


def parse_catalogue(source, text):
    return catalogue(
        records(json_object(text), "offers"),
        lambda item: Entry(
            identifier(item.get("id")),
            public_link(item.get("careers_url") or item.get("url"), source.origin),
            item,
        ),
    )


def detail_request(source, entry):
    return None


def parse_posting(source, entry, detail=None):
    item = entry.data
    description = "\n\n".join(
        v for v in [html_text(item.get("description")), html_text(item.get("requirements"))] if v
    )
    return ParsedPosting(
        entry.posting_id,
        required(item.get("title"), "title"),
        source.company or source.tenant,
        entry.detail_url,
        description=description,
        locations=locations(item.get("locations") or item.get("location")),
        application_url=public_link(item.get("careers_apply_url"), source.origin)
        if item.get("careers_apply_url")
        else entry.detail_url,
        employment_type=item.get("employment_type"),
        remote_ok=workplace(item.get("remote")),
        posted_at=timestamp(item.get("published_at")),
        source_updated_at=timestamp(item.get("updated_at")),
        extra={"department": item.get("department"), "salary": item.get("salary")},
    )
