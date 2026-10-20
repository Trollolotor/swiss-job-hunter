"""Maintained platform adapters. Configuration is per tenant, not per CV."""

from urllib.parse import quote

from bs4 import BeautifulSoup

from employers.contracts import CollectedPosting, CollectionResult, Location
from scrapers.base import ScrapedJob


def text(value):
    return BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)


async def enumerate_smartrecruiters(config, context):
    from employers.contracts import CatalogueEntry, CatalogueResult, digest
    base = f"https://api.smartrecruiters.com/v1/companies/{quote(config['tenant'], safe='')}/postings"
    catalogue = CatalogueResult()
    offset, seen = 0, set()
    for _ in range(context.settings.max_pages):
        page = await context.fetch(f"{base}?limit=100&offset={offset}")
        data = page.json()
        items = data["content"]
        catalogue.reported_total = data["totalFound"]
        for item in items:
            pid = str(item["id"])
            if pid in seen:
                catalogue.diagnostics.append("Repeated catalogue identity")
                return catalogue
            seen.add(pid)
            catalogue.observed_ids.append(pid)
            catalogue.entries.append(CatalogueEntry(posting_id=pid,
                detail_url=f"{base}/{quote(pid, safe='')}", change_token=digest(item),
                metadata={"listing_key": page.key}))
        offset += len(items)
        if offset >= catalogue.reported_total:
            catalogue.termination = "total" if offset else "explicit_empty"
            break
        if not items:
            break
    return catalogue


def normalize_smartrecruiters(config, entry, detail):
    d = detail.json()
    sections = d.get("jobAd", {}).get("sections", {})
    description = "\n\n".join(text(v.get("text", "")) for v in sections.values())
    loc, lang = d.get("location") or {}, d.get("language") or {}
    tenant, pid = config["tenant"], entry.posting_id
    job_url = d.get("postingUrl") or f"https://jobs.smartrecruiters.com/{quote(tenant)}/{pid}"
    job = ScrapedJob(title=d["name"], company=d.get("company", {}).get("name", tenant),
        location=loc.get("fullLocation") or loc.get("city", ""), description=description,
        url=job_url, source=f"employer:{tenant}", source_job_id=pid,
        employment_type=d.get("typeOfEmployment", {}).get("label"), remote_ok=loc.get("remote"),
        extra={"released_at": d.get("releasedDate"), "description_quality": "complete"})
    return CollectedPosting(job=job, posting_id=pid, variant=lang.get("code", ""),
        requisition_id=d.get("refNumber"),
        locations=[Location(country=loc.get("country"), city=loc.get("city"), raw=job.location)],
        application_url=d.get("applyUrl") or job_url, evidence=[detail.key])


async def smartrecruiters(config, context):
    from employers.cache import cached
    catalogue = await enumerate_smartrecruiters(config, context)
    context.catalogue_entries = catalogue.entries
    context.staged = []
    result = CollectionResult(**catalogue.model_dump(exclude={"entries"}))
    for entry in catalogue.entries:
        posting = cached(context, entry)
        if posting is None:
            detail = await context.fetch(entry.detail_url)
            context.fresh_detail_keys.add(detail.key)
            posting = normalize_smartrecruiters(config, entry, detail)
        result.postings.append(posting)
        context.staged.append(posting)
    result.observed_identities = [p.identity for p in result.postings]
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
    "capabilities": {"browser": False, "incremental": True, "scope": "all countries"},
}
