"""Maintained platform adapters. Configuration is per tenant, not per CV."""

from urllib.parse import quote

from bs4 import BeautifulSoup

from employers.contracts import CollectedPosting, CollectionResult, Location
from scrapers.base import ScrapedJob


def text(value):
    return BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)


async def smartrecruiters(config, context):
    tenant = config["tenant"]
    base = f"https://api.smartrecruiters.com/v1/companies/{quote(tenant, safe='')}/postings"
    result = CollectionResult()
    offset, seen = 0, set()
    for _ in range(context.settings.max_pages):
        page = await context.fetch(f"{base}?limit=100&offset={offset}")
        data = page.json()
        items = data["content"]
        result.reported_total = data["totalFound"]
        fresh = [item for item in items if str(item["id"]) not in seen]
        if items and not fresh:
            result.diagnostics.append("Repeated catalogue page")
            break
        for item in fresh:
            pid = str(item["id"])
            seen.add(pid)
            result.observed_ids.append(pid)
            detail = await context.fetch(f"{base}/{quote(pid, safe='')}")
            d = detail.json()
            sections = d.get("jobAd", {}).get("sections", {})
            description = "\n\n".join(text(v.get("text", "")) for v in sections.values())
            loc = d.get("location") or item.get("location") or {}
            lang = d.get("language") or item.get("language") or {}
            job_url = (
                d.get("postingUrl") or f"https://jobs.smartrecruiters.com/{quote(tenant)}/{pid}"
            )
            # releasedDate is preserved explicitly; it is not guaranteed to be
            # the original publication time and does not manufacture freshness.
            job = ScrapedJob(
                title=d["name"],
                company=d.get("company", {}).get("name", tenant),
                location=loc.get("fullLocation") or loc.get("city", ""),
                description=description,
                url=job_url,
                source=f"employer:{tenant}",
                source_job_id=pid,
                employment_type=d.get("typeOfEmployment", {}).get("label"),
                remote_ok=loc.get("remote"),
                extra={"released_at": d.get("releasedDate")},
            )
            result.postings.append(
                CollectedPosting(
                    job=job,
                    posting_id=pid,
                    variant=lang.get("code", ""),
                    requisition_id=d.get("refNumber"),
                    locations=[
                        Location(country=loc.get("country"), city=loc.get("city"), raw=job.location)
                    ],
                    application_url=d.get("applyUrl") or job_url,
                    evidence=[page.key, detail.key],
                )
            )
        offset += len(items)
        if offset >= result.reported_total:
            result.termination = "total" if result.reported_total else "explicit_empty"
            break
        if not items:
            break
    return result


ADAPTERS = {"smartrecruiters": smartrecruiters}


def adapter_fingerprint():
    import hashlib
    from importlib.metadata import version
    from pathlib import Path
    return {"code":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "dependencies":{name:version(name) for name in
                            ("httpx","httpcore","beautifulsoup4","lxml")}}

SMARTRECRUITERS_MANIFEST = {
    "catalogue": {
        "format": "json",
        "path_suffix": "/postings",
        "items": "content",
        "id": "id",
        "total": "totalFound",
        "pagination": "offset",
        "page_size": 100,
    },
    "capabilities": {"browser": False, "incremental": False, "scope": "all countries"},
}
