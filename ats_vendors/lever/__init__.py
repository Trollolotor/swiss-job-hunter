"""Lever public postings: the complete body includes introduction AND section lists."""

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
    workplace,
)


def detect(url, html=""):
    p = urlsplit(url)
    tenant = p.path.strip("/").split("/")[0]
    if p.hostname not in {"jobs.lever.co", "jobs.eu.lever.co"} or not tenant:
        return None
    return Portal(
        "lever",
        tenant,
        f"https://{p.hostname}",
        f"https://{p.hostname}/{tenant}",
        variant="eu" if ".eu." in p.hostname else "global",
    )


def catalogue_request(source, offset=0):
    host = "api.eu.lever.co" if source.variant == "eu" else "api.lever.co"
    return Request(f"https://{host}/v0/postings/{quote(source.tenant, safe='')}?mode=json")


def parse_catalogue(source, text):
    return catalogue(
        records(json_object(text)),
        lambda item: Entry(identifier(item.get("id")), public_link(item.get("hostedUrl")), item),
    )


def detail_request(source, entry):
    return None


def parse_posting(source, entry, detail=None):
    item, parts = entry.data, []
    intro = html_text(item.get("description") or item.get("descriptionPlain"))
    if intro:
        parts.append(intro)
    for section in item.get("lists") or []:
        heading, body = html_text(section.get("text")), html_text(section.get("content"))
        if body:
            parts.append("\n\n".join(v for v in [heading, body] if v))
    if tail := html_text(item.get("additional") or item.get("additionalPlain")):
        parts.append(tail)
    categories, salary = item.get("categories") or {}, item.get("salaryRange") or {}
    return ParsedPosting(
        entry.posting_id,
        required(item.get("text"), "title"),
        source.company or source.tenant,
        entry.detail_url,
        description="\n\n".join(parts),
        locations=locations(categories.get("allLocations") or categories.get("location")),
        application_url=public_link(item.get("applyUrl"))
        if item.get("applyUrl")
        else entry.detail_url,
        employment_type=categories.get("commitment"),
        remote_ok=workplace(item.get("workplaceType")),
        salary_raw=str(salary) if salary else None,
        extra={
            "source_created_at": item.get("createdAt"),
            "department": categories.get("department"),
            "team": categories.get("team"),
            "workplace_type": item.get("workplaceType"),
            "salary": salary,
        },
    )
