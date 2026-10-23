"""Trusted, item-scoped validation for product-owned vendor protocols.

Reparse broker observations independently of the isolated connector's output.
Legacy generated manifests continue to use the existing generic validator.
"""

from datetime import UTC, datetime

from ats_core import SchemaError
from ats_core.parsing import normalized
from ats_vendors import REVISION
from ats_vendors.registry import fingerprint, from_config, vendor
from employers.contracts import digest


def definition(manifest):
    recipe = manifest["ats_library"]
    name = recipe["vendor"]
    if recipe["revision"] != REVISION or recipe["fingerprint"] != fingerprint(name):
        raise SchemaError(
            "ATS library changed; validate a new connector with the matching runtime image"
        )
    source = from_config(name, recipe["source"])
    return source, vendor(name)


def catalogue_observation(source, protocol, observation):
    offset = 0
    if source.vendor == "workday":
        offset = (observation.request_body or {}).get("offset", 0)
        if type(offset) is not int or offset < 0:
            return False
    request = protocol.catalogue_request(source, offset)
    return (
        observation.status == 200
        and observation.url == request.url
        and observation.method == request.method
        and observation.request_body == request.body
    )


def observation_cache(observation):
    # Run-scoped and derived only from trusted broker bytes. Keeping the source
    # state also invalidates the index if a replay/test replaces an observation.
    state = (observation.url, observation.method, observation.status, observation.text)
    cached = getattr(observation, "_ats_validation", None)
    if cached is None or cached["state"] != state:
        cached = {"state": state, "key": observation.key, "catalogues": {}}
        observation._ats_validation = cached
    return cached


def indexed_catalogue(source, protocol, observation):
    cache = observation_cache(observation)["catalogues"]
    if source not in cache:
        page = protocol.parse_catalogue(source, observation.text)
        index = {}
        for entry in page.entries:
            index.setdefault(entry.identity, []).append(entry)
        cache[source] = page, index
    return cache[source]


def expected(posting, observations, manifest):
    source, protocol = definition(manifest)
    evidence = list(
        {
            observation_cache(o)["key"]: o
            for o in observations
            if observation_cache(o)["key"] in posting.evidence
        }.values()
    )
    matching = []
    for obs in evidence:
        if catalogue_observation(source, protocol, obs):
            _, index = indexed_catalogue(source, protocol, obs)
            matching.extend(index.get((posting.posting_id, posting.variant), []))
    if len(matching) != 1:
        raise SchemaError("Posting does not identify exactly one observed catalogue record")
    entry = matching[0]
    request = protocol.detail_request(source, entry)
    detail = None
    if request is not None:
        responses = [
            o
            for o in evidence
            if o.status == 200
            and o.url == request.url
            and o.method == request.method
            and o.request_body == request.body
        ]
        if not responses:
            raise SchemaError("Posting detail has no matching broker observation")
        detail = responses[-1].text
    return protocol.parse_posting(source, entry, detail)


def date_equal(actual, expected):
    def utc(value):
        if not value:
            return None
        value = datetime.fromisoformat(value) if isinstance(value, str) else value
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    return utc(actual) == utc(expected)


def posting_errors(posting, observations, manifest):
    from employers.network import public_url

    try:
        parsed = expected(posting, observations, manifest)
        wire = parsed.wire(manifest["ats_library"]["vendor"], posting.evidence)
        errors = []
        for link in (posting.job.url, posting.application_url):
            if link:
                public_url(link)
        for field in ("title", "company", "description", "location"):
            if normalized(getattr(posting.job, field) or "") != normalized(wire["job"][field]):
                errors.append(f"{field} differs from this posting's observed source fields")
        if posting.job.url != parsed.url or posting.application_url != wire["application_url"]:
            errors.append("Vacancy/application URL differs from this posting's source")
        if posting.requisition_id != parsed.requisition_id:
            errors.append("Requisition identity differs from source")
        if [loc.model_dump() for loc in posting.locations] != parsed.locations:
            errors.append("Locations differ from this posting's source")
        if (
            posting.remote_countries != parsed.remote_countries
            or posting.job.remote_ok != parsed.remote_ok
        ):
            errors.append("Remote eligibility differs from explicit source evidence")
        if (
            posting.job.employment_type != parsed.employment_type
            or posting.job.salary_raw != parsed.salary_raw
        ):
            errors.append("Employment or salary differs from source")
        if not date_equal(posting.job.posted_at, parsed.posted_at):
            errors.append("Publication date differs from explicit publication evidence")
        if not date_equal(posting.source_updated_at, parsed.source_updated_at):
            errors.append("Source update time differs from evidence")
        return errors
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        return [f"Vendor evidence rejected: {exc}"[:500]]


def complete_description(posting, observations, manifest):
    try:
        parsed = expected(posting, observations, manifest)
        return bool(parsed.description) and normalized(parsed.description) == normalized(
            posting.job.description
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def assess(result, observations, manifest):
    errors, observed, pages, totals, offsets = list(result.diagnostics), [], [], [], []
    limitation = None
    try:
        source, protocol = definition(manifest)
        for obs in observations:
            if not catalogue_observation(source, protocol, obs):
                continue
            page, _ = indexed_catalogue(source, protocol, obs)
            errors.extend(page.diagnostics)
            pages.append(page)
            identities = [entry.identity for entry in page.entries]
            if len(identities) != len(set(identities)):
                errors.append("Duplicate posting identities in catalogue")
            observed.extend(identities)
            if source.vendor == "workday":
                totals.append(page.total)
                offsets.append(obs.request_body["offset"])
        if not pages:
            errors.append("No matching broker-observed vendor catalogue")
        if len(observed) != len(set(observed)):
            errors.append("Repeated/truncated catalogue pagination")
        if source.vendor != "workday" and len(pages) != 1:
            errors.append("Single-response catalogue must have exactly one fresh observation")
        if source.vendor == "workday" and totals:
            total = totals[0]
            if total == 2000:
                limitation = "catalogue_cap"
                errors.append("Workday query cap does not establish complete employer coverage")
            max_pages = manifest["catalogue"].get("max_pages", 500)
            if (
                result.termination == "incomplete"
                and len(pages) >= max_pages
                and len(observed) < total
            ):
                limitation = limitation or "page_limit"
                errors.append("Configured page limit prevents complete employer coverage")
            if any(v not in (0, total) for v in totals[1:]) or len(observed) != total:
                errors.append("Workday count does not reconcile with observed source IDs")
            expected_offset = 0
            for page, offset in zip(pages, offsets, strict=True):
                if offset != expected_offset:
                    errors.append("Workday pagination skipped a range or did not start at zero")
                expected_offset += len(page.entries)
        elif pages and result.reported_total != len(observed):
            errors.append("Reported total differs from the observed full-feed record count")
        if set(result.observed_ids) != {identity[0] for identity in observed}:
            errors.append("Reported IDs differ from observed catalogue IDs")
        if {(p.posting_id, p.variant) for p in result.postings} != set(observed):
            errors.append("Missing catalogue details")
        if len(result.postings) != len({p.identity for p in result.postings}):
            errors.append("Duplicate posting variants")
        for posting in result.postings:
            errors.extend(posting_errors(posting, observations, manifest))
            if not complete_description(posting, observations, manifest):
                errors.append(f"{posting.posting_id}: complete description unavailable")
        if result.termination == "incomplete":
            errors.append("Connector did not reach catalogue termination")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        errors.append(f"Vendor catalogue validation failed: {exc}"[:500])
    outcome = (
        ("partial" if result.postings else "failed")
        if errors
        else "complete"
        if observed
        else "empty"
    )
    return {
        "outcome": outcome,
        "errors": list(dict.fromkeys(errors)),
        "limitation": limitation,
        "observed_count": len(set(observed)),
        "identities": sorted(p.identity for p in result.postings),
        "evidence_hash": digest([observation_cache(o)["key"] for o in observations]),
        "validator_version": "ats-1",
        "catalogue_totals": totals,
    }
