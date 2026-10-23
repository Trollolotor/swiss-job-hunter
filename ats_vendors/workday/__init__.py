"""Workday CXS, with count reconciliation and conservative capped-run handling."""

import re
from urllib.parse import quote, urlsplit

from ats_core import Entry, ParsedPosting, Portal, Request, SchemaError
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
    if not re.fullmatch(r"[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com", p.hostname or ""):
        return None
    parts = [v for v in p.path.split("/") if v]
    if parts and re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", parts[0], re.IGNORECASE):
        parts = parts[1:]
    if not parts or parts[0] in {"job", "wday"}:
        return None
    origin = f"https://{p.hostname}"
    return Portal(
        "workday", p.hostname.split(".")[0], origin, origin + "/" + parts[0], board=parts[0]
    )


def base(source):
    return (
        source.origin + f"/wday/cxs/{quote(source.tenant, safe='')}/{quote(source.board, safe='')}"
    )


def catalogue_request(source, offset=0):
    return Request(
        base(source) + "/jobs",
        "POST",
        {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
    )


def parse_catalogue(source, text):
    data = json_object(text)
    total = data.get("total") if isinstance(data, dict) else None
    if type(total) is not int or total < 0:
        raise SchemaError("Workday catalogue lacks an integer total")

    def entry(item):
        path = identifier(item.get("externalPath"))
        if not path.startswith("/job/") or any(v in path for v in ["?", "#", "/../", "%"]):
            raise SchemaError("Workday catalogue has an invalid detail path")
        return Entry(path, public_link(source.portal_url + path), item, inline=False)

    return catalogue(records(data, "jobPostings"), entry, total=total, termination="incomplete")


def detail_request(source, entry):
    return Request(base(source) + entry.posting_id)


def parse_posting(source, entry, detail=None):
    data = json_object(detail)
    info = data.get("jobPostingInfo")
    if not isinstance(info, dict):
        raise SchemaError("Workday detail lacks jobPostingInfo")
    url = public_link(info.get("externalUrl") or entry.detail_url)
    return ParsedPosting(
        entry.posting_id,
        required(info.get("title"), "title"),
        source.company or source.tenant,
        url,
        description=html_text(info.get("jobDescription")),
        requisition_id=info.get("jobReqId"),
        locations=locations(info.get("location"), info.get("additionalLocations")),
        posted_at=timestamp(info.get("datePosted")),
        employment_type=info.get("timeType"),
        remote_ok=workplace(info.get("remoteType")),
        extra={
            "workplace_type": info.get("remoteType"),
            "country": info.get("country"),
            "valid_through": info.get("endDate"),
        },
    )
