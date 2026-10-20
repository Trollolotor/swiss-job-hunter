"""Representative generated-code fixture. Deliberately absent from adapter registry."""

import re
from html import unescape
from urllib.parse import urlsplit


def collect(config, sdk):
    portal = config["portal_url"].rstrip("/")
    origin = f"https://{urlsplit(portal).hostname}"
    base = f"{origin}/wday/cxs/{config['tenant']}/{config['board']}"
    postings, observed = [], []
    offset = 0
    total = None
    for _ in range(100):
        data, evidence = sdk.json(
            base + "/jobs",
            "POST",
            {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
        )
        total = data["total"]
        items = data["jobPostings"]
        if not items and offset < total:
            break
        for item in items:
            pid = item["externalPath"]
            if pid in observed:
                return {
                    "postings": postings,
                    "observed_ids": observed,
                    "reported_total": total,
                    "termination": "incomplete",
                    "diagnostics": ["Repeated page"],
                }
            observed.append(pid)
            detail, detail_evidence = sdk.json(base + pid)
            info = detail["jobPostingInfo"]
            description = unescape(re.sub("<[^>]+>", " ", info["jobDescription"]))
            description = " ".join(description.split())
            raw = info.get("location", item.get("locationsText", ""))
            locations = [
                {
                    "raw": v,
                    "country": "CH"
                    if any(
                        s in v.casefold() for s in ["switzerland", "schweiz", "suisse", "svizzera"]
                    )
                    else None,
                }
                for v in [raw, *info.get("additionalLocations", [])]
            ]
            url = info.get("externalUrl") or portal + pid
            postings.append(
                {
                    "job": {
                        "title": info["title"],
                        "company": config.get("company", config["tenant"]),
                        "location": raw,
                        "description": description,
                        "url": url,
                        "source": "employer:" + config["tenant"],
                        "source_job_id": pid,
                    },
                    "posting_id": pid,
                    "requisition_id": info.get("jobReqId"),
                    "locations": locations,
                    "application_url": url,
                    "evidence": [evidence, detail_evidence],
                }
            )
        offset += len(items)
        if offset >= total:
            return {
                "postings": postings,
                "observed_ids": observed,
                "reported_total": total,
                "termination": "total" if total else "explicit_empty",
            }
    return {
        "postings": postings,
        "observed_ids": observed,
        "reported_total": total,
        "termination": "incomplete",
        "diagnostics": ["Catalogue did not terminate"],
    }
