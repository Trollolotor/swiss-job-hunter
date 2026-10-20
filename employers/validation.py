"""Trusted completeness validation. Connector claims are never sufficient."""

import re
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from employers.contracts import CollectionResult, digest


def path(value, key):
    for part in key.split(".") if key else []:
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def posting_errors(posting, observations):
    """Individually check extracted fields against bytes collected by trusted tools."""
    from employers.network import public_url

    errors = []
    job = posting.job
    if not job.title.strip() or not job.company.strip() or len(job.description.strip()) < 50:
        return ["Missing required vacancy fields"]
    try:
        public_url(job.url)
        if posting.application_url:
            public_url(posting.application_url)
    except ValueError:
        errors.append("Invalid vacancy/application URL")
    evidence = {o.key: o for o in observations}

    def leaves(value):
        if isinstance(value, dict):
            return [s for v in value.values() for s in leaves(v)]
        if isinstance(value, list):
            return [s for v in value for s in leaves(v)]
        return [str(value)] if value is not None else []

    def normalize(value):
        return " ".join(
            BeautifulSoup(value, "html.parser").get_text(" ", strip=True).split()
        ).casefold()

    values = []
    for key in posting.evidence:
        if key not in evidence:
            continue
        obs = evidence[key]
        try:
            values.extend(leaves(obs.json()))
        except (ValueError, TypeError):
            values.append(obs.text)
    body = normalize(" ".join(values))
    description_parts = [normalize(part) for part in job.description.split("\n\n") if part.strip()]
    if (
        not body
        or normalize(job.title) not in body
        or not all(part in body for part in description_parts)
    ):
        errors.append("Title/description are not supported by observed source content")
    for link in (job.url, posting.application_url):
        if not link:
            continue
        if not (
            link in values
            or any(link == evidence[k].url for k in posting.evidence if k in evidence)
            or (
                posting.posting_id in urlsplit(link).path
                and any(
                    urlsplit(evidence[k].url).hostname == urlsplit(link).hostname
                    for k in posting.evidence
                    if k in evidence
                )
            )
        ):
            errors.append("Vacancy/application link lacks source evidence")
    if posting.locations and not all(
        not location.raw or normalize(location.raw) in body for location in posting.locations
    ):
        errors.append("Location is not supported by observed source content")
    return errors


def assess(result: CollectionResult, observations, manifest):
    errors = list(result.diagnostics)
    spec = manifest.get("catalogue", {})
    pages, observed, totals, cursors = [], [], [], []
    try:
        for obs in observations:
            if spec.get("rendered", False) != (obs.method == "BROWSER"):
                continue
            if not urlsplit(obs.url).path.endswith(spec["path_suffix"]):
                continue
            if obs.status != 200:
                continue
            if spec["format"] == "json":
                data = obs.json()
                items = path(data, spec["items"])
                ids = [str(path(item, spec["id"])) for item in items]
                total = path(data, spec["total"]) if spec.get("total") else None
            else:
                soup = BeautifulSoup(obs.text, "html.parser")
                nodes = soup.select(spec["items"])
                ids = [n.get(spec.get("id_attribute", "href"), "") for n in nodes]
                total = None
                if not ids and not pages and spec.get("empty"):
                    empties = soup.select(spec["empty"])
                    for empty in empties:
                        hidden = (
                            empty.has_attr("hidden")
                            or "display:none" in empty.get("style", "").replace(" ", "").lower()
                            or set(empty.get("class", [])) & {"hidden", "is-hidden"}
                        )
                        if not hidden and re.search(
                            r"\b(no (?:open |current )?(?:jobs|vacancies|positions)|0 (?:jobs|vacancies)|"
                            r"keine offenen|aucun (?:poste|emploi)|nessuna posizione)\b",
                            empty.get_text(" ", strip=True),
                            re.IGNORECASE,
                        ):
                            total = 0
                            break
            if not all(ids) or len(ids) != len(set(ids)):
                errors.append("Missing or duplicate source IDs on a page")
            pages.append(ids)
            observed.extend(ids)
            if total is not None:
                totals.append(int(total))
            if spec.get("pagination") == "offset":
                cursor = (
                    (obs.request_body or {}).get("offset")
                    if obs.method == "POST"
                    else parse_qs(urlsplit(obs.url).query).get("offset", ["0"])[0]
                )
                cursors.append(int(cursor))
        if not pages:
            errors.append("No independently observed catalogue pages")
        if len(observed) != len(set(observed)):
            errors.append("Repeated/truncated pagination")
        if totals and (len(set(totals)) != 1 or len(set(observed)) != totals[0]):
            errors.append("Catalogue count does not reconcile with unique source IDs")
        if cursors:
            expected, offset = [], 0
            for ids in pages:
                expected.append(offset)
                offset += len(ids)
            if cursors != expected:
                errors.append("Pagination skipped a range or did not start at zero")
        if not totals:
            # HTML next-link termination must be independently observable.
            if not spec.get("next") or spec.get("format") != "html":
                errors.append("No trusted count or HTML pagination termination proof")
            elif pages:
                relevant = [
                    o
                    for o in observations
                    if urlsplit(o.url).path.endswith(spec["path_suffix"])
                    and spec.get("rendered", False) == (o.method == "BROWSER")
                ]
                from urllib.parse import urljoin

                for index, obs in enumerate(relevant[:-1]):
                    links = BeautifulSoup(obs.text, "html.parser").select(spec["next"])
                    if (
                        not links
                        or urljoin(obs.url, links[0].get("href", "")) != relevant[index + 1].url
                    ):
                        errors.append("HTML pagination skipped an observed next link")
                if BeautifulSoup(relevant[-1].text, "html.parser").select(spec["next"]):
                    errors.append("Next page remains available")
        if set(result.observed_ids) != set(observed):
            errors.append("Reported catalogue identities differ from observed pages")
        reported = {p.posting_id for p in result.postings}
        if reported != set(observed):
            errors.append("Missing catalogue details")
        identities = [p.identity for p in result.postings]
        if len(set(identities)) != len(identities):
            errors.append("Duplicate posting variants")
        for posting in result.postings:
            errors.extend(
                f"{posting.posting_id}: {e}" for e in posting_errors(posting, observations)
            )
        if result.termination == "incomplete":
            errors.append("Connector did not reach catalogue termination")
        if not observed and not totals:
            errors.append("Empty catalogue lacks affirmative zero-count evidence")
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        errors.append(f"Observed catalogue schema changed: {type(exc).__name__}")
    valid_postings = bool(result.postings)
    outcome = (
        ("partial" if valid_postings else "failed")
        if errors
        else ("complete" if observed else "empty")
    )
    return {
        "outcome": outcome,
        "errors": list(dict.fromkeys(errors)),
        "observed_count": len(set(observed)),
        "identities": sorted(p.identity for p in result.postings),
        "evidence_hash": digest([o.key for o in observations]),
        "validator_version": "1",
    }


def repeatable(first, second):
    return (
        first["outcome"] in {"complete", "empty"}
        and second["outcome"] in {"complete", "empty"}
        and first["identities"] == second["identities"]
    )
