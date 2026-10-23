"""Separate SuccessFactors RMK RSS and legacy Recruiting Management XML protocols."""

import re
from urllib.parse import parse_qs, urlencode, urlsplit

from ats_core import Entry, ParsedPosting, Portal, Request, SchemaError
from ats_core.parsing import (
    catalogue,
    html_text,
    identifier,
    locations,
    public_link,
    required,
    timestamp,
    xml_fields,
    xml_root,
)


def detect(url, html=""):
    p = urlsplit(url)
    host = p.hostname or ""
    query = parse_qs(p.query)
    company = (query.get("company") or [""])[0]
    legacy = re.fullmatch(r"[a-z0-9-]+\.(?:successfactors\.(?:com|eu)|sapsf\.(?:com|eu|cn))", host)
    if legacy and company:
        return Portal(
            "successfactors",
            company,
            f"https://{host}",
            url,
            variant="legacy",
            locale=(query.get("rcm_site_locale") or [""])[0],
        )
    # Custom branded RMK sites need observed vendor script/config evidence;
    # seeing the word SuccessFactors in ordinary page copy is insufficient.
    marker = re.search(
        r'(?:src=["\'][^"\']*(?:rmkcdn|successfactors)|careerSiteBuilder|ssoCompanyId)',
        html,
        re.IGNORECASE,
    )
    if marker:
        return Portal("successfactors", host, f"https://{host}", url, variant="rmk")
    return None


def catalogue_request(source, offset=0):
    if source.variant == "legacy":
        query = {"company": source.tenant, "career_ns": "job_listing_summary", "resultType": "XML"}
        if source.locale:
            query["rcm_site_locale"] = source.locale
        return Request(source.origin + "/career?" + urlencode(query))
    if source.variant != "rmk":
        raise SchemaError("Unrecognized SuccessFactors protocol variant")
    return Request(source.origin + "/sitemal.xml")


def parse_catalogue(source, text):
    root = xml_root(text, {"Job-Listing"} if source.variant == "legacy" else {"rss"})
    if source.variant == "legacy":
        items = [xml_fields(item) for item in root.findall("./Job")]

        def entry(item):
            pid = identifier(item.get("ReqId"))
            url = (
                source.origin
                + "/sfcareer/jobreqcareer?"
                + urlencode({"jobId": pid, "company": source.tenant})
            )
            return Entry(pid, url, item, variant=source.locale)
    else:
        if root.find("channel") is None:
            raise SchemaError("SuccessFactors RSS has no channel")
        items = [xml_fields(item) for item in root.findall("./channel/item")]

        def entry(item):
            link = public_link(item.get("link"))
            pid = (
                item.get("id")
                or item.get("guid")
                or urlsplit(link).path.rstrip("/").rsplit("/", 1)[-1]
            )
            return Entry(identifier(pid), link, item, variant=source.locale)

    return catalogue(items, entry)


def detail_request(source, entry):
    return None


def parse_posting(source, entry, detail=None):
    item = entry.data
    if source.variant == "legacy":
        title, description = item.get("JobTitle"), item.get("Job-Description") or ""
        # Substitute only observed source fields. Unknown tokens must remain
        # visible as a schema issue, not disappear from the requirements.
        description = re.sub(
            r"\[\[([^\[\]]+)\]\]", lambda m: item.get(m.group(1), m.group(0)), description
        )
        if re.search(r"\[\[.*?\]\]", description):
            raise SchemaError("Unresolved SuccessFactors description template field")
        loc = item.get("Location") or ", ".join(
            v for v in [item.get("City"), item.get("State"), item.get("Country")] if v
        )
        date = timestamp(item.get("Posted-Date"), legacy_locale=source.locale or None)
        req = entry.posting_id
    else:
        title, description, loc = item.get("title"), item.get("description"), item.get("location")
        date, req = timestamp(item.get("pubDate")), None
    return ParsedPosting(
        entry.posting_id,
        required(title, "title"),
        source.company or item.get("employer") or source.tenant,
        entry.detail_url,
        description=html_text(description),
        variant=entry.variant,
        requisition_id=req,
        locations=locations(loc),
        posted_at=date,
        employment_type=item.get("job_type"),
        extra={
            "department": item.get("job_function") or item.get("Department"),
            "valid_through": item.get("expiration_date"),
            "locale": source.locale,
        },
    )
