"""Bounded public observations for known portal protocols, never collection code."""

from urllib.parse import urlsplit, urlunsplit


def samples(observations):
    """Keep catalogue context and actual structured details, ahead of HTML probes."""
    details = []
    for observation in observations:
        try:
            data = observation.json()
        except (ValueError, TypeError):
            continue
        if (
            observation.method == "GET"
            and isinstance(data, dict)
            and any(
                key in data for key in ("jobPostingInfo", "jobAd", "jobDescription", "description")
            )
        ):
            details.append(observation)
    return list({o.key: o for o in observations[:1] + (details[-2:] or observations[-2:])}.values())


async def prime(source, context):
    """Observe one catalogue page and one detail before asking for a connector.

    Workday's tenant has already been evidenced by the official site's link.
    These two brokered requests supply source evidence, not a maintained adapter:
    pagination, normalization and routine collection remain generated code.
    """
    if source["platform"] != "workday":
        return []
    portal = urlsplit(source["portal_url"])
    base = f"/wday/cxs/{source['tenant']}/{source['board']}"
    url = urlunsplit((portal.scheme, portal.netloc, base + "/jobs", "", ""))
    listing = await context.fetch(
        url, "POST", {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
    )
    observations = [listing]
    items = listing.json().get("jobPostings", [])
    if items:
        path = items[0].get("externalPath", "")
        if not isinstance(path, str) or not path.startswith("/job/") or "?" in path or "#" in path:
            raise ValueError("Workday listing does not evidence a public detail path")
        detail_url = urlunsplit((portal.scheme, portal.netloc, base + path, "", ""))
        observations.append(await context.fetch(detail_url))
    return [
        {
            "url": o.url,
            "method": o.method,
            "body": o.request_body,
            "key": o.key,
            "text": o.text[:60000],
        }
        for o in observations
    ]
