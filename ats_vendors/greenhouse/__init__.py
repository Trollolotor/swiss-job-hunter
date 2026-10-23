"""Greenhouse public board JSON, with full content and regional routing."""

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
)

HOSTS = {
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "boards.eu.greenhouse.io",
    "job-boards.eu.greenhouse.io",
}


def detect(url, html=""):
    p = urlsplit(url)
    parts = p.path.strip("/").split("/")
    if p.hostname not in HOSTS or not parts[0]:
        return None
    return Portal(
        "greenhouse",
        parts[0],
        f"https://{p.hostname}",
        f"https://{p.hostname}/{parts[0]}",
        variant="eu" if ".eu." in p.hostname else "global",
    )


def catalogue_request(source, offset=0):
    host = "boards-api.eu.greenhouse.io" if source.variant == "eu" else "boards-api.greenhouse.io"
    return Request(f"https://{host}/v1/boards/{quote(source.tenant, safe='')}/jobs?content=true")


def parse_catalogue(source, text):
    data = json_object(text)
    return catalogue(
        records(data, "jobs"),
        lambda item: Entry(identifier(item.get("id")), public_link(item.get("absolute_url")), item),
    )


def detail_request(source, entry):
    return None


def parse_posting(source, entry, detail=None):
    item = entry.data
    req = item.get("requisition_id")
    req = str(req) if req and str(req).casefold() not in {"tbd", "n/a", "see opening id"} else None
    return ParsedPosting(
        entry.posting_id,
        required(item.get("title"), "title"),
        source.company or source.tenant,
        entry.detail_url,
        description=html_text(item.get("content")),
        requisition_id=req,
        locations=locations(item.get("offices"), item.get("location")),
        posted_at=timestamp(item.get("first_published")),
        source_updated_at=timestamp(item.get("updated_at")),
        extra={"departments": item.get("departments", []), "metadata": item.get("metadata", [])},
    )
