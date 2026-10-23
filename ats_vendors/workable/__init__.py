"""Workable widget catalogue with full-body Markdown fallback."""

import re
from urllib.parse import quote, urlsplit

from ats_core import Entry, ParsedPosting, Portal, Request, SchemaError
from ats_core.parsing import (
    catalogue,
    identifier,
    json_object,
    locations,
    public_link,
    records,
    required,
    sections,
    timestamp,
    workplace,
)


def detect(url, html=""):
    p = urlsplit(url)
    tenant = p.path.strip("/").split("/")[0]
    if p.hostname != "apply.workable.com" or not tenant or tenant in {"api", "j", "accounts"}:
        return None
    return Portal(
        "workable", tenant, "https://apply.workable.com", f"https://apply.workable.com/{tenant}"
    )


def catalogue_request(source, offset=0):
    return Request(
        f"{source.origin}/api/v1/widget/accounts/{quote(source.tenant, safe='')}?details=true"
    )


def parse_catalogue(source, text):
    data = json_object(text)

    def entry(item):
        pid = identifier(item.get("shortcode") or item.get("code") or item.get("id"))
        url = public_link(item.get("url") or item.get("shortlink"))
        return Entry(pid, url, item, inline=bool(item.get("description")))

    page = catalogue(records(data, "jobs"), entry)
    # A job can be repeated for different locations. Merge only records whose
    # substantive fields agree; disagreement remains visible as a schema issue.
    unique = {}
    for current in page.entries:
        old = unique.get(current.identity)
        if old is None:
            unique[current.identity] = current
        else:
            keys = ["title", "description", "requirements", "benefits", "url"]
            if any(old.data.get(k) != current.data.get(k) for k in keys):
                page.diagnostics.append("Conflicting records for one Workable shortcode")
            old.data = {
                **old.data,
                "locations": locations(
                    old.data.get("locations") or old.data.get("location") or old.data,
                    current.data.get("locations") or current.data.get("location") or current.data,
                ),
            }
    page.entries = list(unique.values())
    return page


def detail_request(source, entry):
    if entry.inline:
        return None
    return Request(
        f"{source.origin}/{quote(source.tenant, safe='')}/jobs/view/{quote(entry.posting_id, safe='')}.md"
    )


def parse_posting(source, entry, detail=None):
    item = entry.data
    if entry.inline:
        description = sections(
            item.get("description"), item.get("requirements"), item.get("benefits")
        )
    else:
        description = (detail or "").strip()
        if re.search(r"<(?:!doctype|html|head|body)\b", description, re.IGNORECASE):
            raise SchemaError("Workable Markdown endpoint returned an HTML error page")
    return ParsedPosting(
        entry.posting_id,
        required(item.get("title"), "title"),
        source.company or source.tenant,
        entry.detail_url,
        description=description,
        locations=locations(item.get("locations") or item.get("location") or item),
        application_url=public_link(item.get("application_url"))
        if item.get("application_url")
        else entry.detail_url,
        posted_at=timestamp(item.get("published_on")),
        employment_type=item.get("type") or item.get("employment_type"),
        remote_ok=workplace(
            item.get("telecommuting") if "telecommuting" in item else item.get("remote")
        ),
        extra={
            "department": item.get("department"),
            "description_format": "html" if entry.inline else "markdown",
        },
    )
