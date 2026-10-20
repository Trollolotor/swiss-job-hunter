"""Trusted completeness validation. Connector claims are never sufficient."""

import json
import re
from datetime import timezone
from urllib.parse import parse_qs, unquote, urlsplit

from bs4 import BeautifulSoup

from employers.contracts import CollectionResult, digest


def path(value, key):
    for part in key.split(".") if key else []:
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def complete_description(posting, observations, manifest):
    """Compare the returned ad with full source fields, not an arbitrary length."""
    candidates = []

    def extract(value):
        if isinstance(value, dict):
            if isinstance(value.get("jobAd"), dict) and isinstance(
                value["jobAd"].get("sections"), dict
            ):
                candidates.append(
                    " ".join(str(v.get("text", "")) for v in value["jobAd"]["sections"].values())
                )
            for key, item in value.items():
                if key.casefold() in {
                    "description",
                    "jobdescription",
                    "job_description",
                } and isinstance(item, str):
                    candidates.append(item)
                elif isinstance(item, (dict, list)):
                    extract(item)
        elif isinstance(value, list):
            for item in value:
                extract(item)

    suffix = manifest.get("catalogue", {}).get("path_suffix", "")
    normalize = lambda value: " ".join(
        BeautifulSoup(value, "html.parser").get_text(" ", strip=True).split()
    ).casefold()
    for observation in observations:
        if observation.key not in posting.evidence or (
            suffix and urlsplit(observation.url).path.endswith(suffix)
        ):
            continue
        try:
            extract(observation.json())
        except (ValueError, TypeError):
            soup = BeautifulSoup(observation.text, "html.parser")
            for script in soup.select('script[type="application/ld+json"]'):
                try:
                    extract(json.loads(script.string or ""))
                except (ValueError, TypeError):
                    pass
            selector = manifest.get("detail", {}).get("description_selector")
            if selector:
                candidates.extend(str(n) for n in soup.select(selector))
    expected = normalize(posting.job.description)
    return bool(expected) and expected in {normalize(text) for text in candidates}


def posting_errors(posting, observations):
    """Individually check extracted fields against bytes collected by trusted tools."""
    from employers.network import public_url

    errors = []
    job = posting.job
    if not job.title.strip() or not job.company.strip() or not job.description.strip():
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
    if not (
        posting.posting_id in values
        or any(
            posting.posting_id == evidence[key].url
            or posting.posting_id in unquote(urlsplit(evidence[key].url).path)
            for key in posting.evidence
            if key in evidence
        )
        or any(
            posting.posting_id in unquote(urlsplit(value).path)
            for value in values
            if value.startswith(("https://", "http://"))
        )
    ):
        errors.append("Posting identity is not supported by observed source fields or URLs")
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
    if job.posted_at is not None:
        # Release/update/observation timestamps are deliberately excluded.
        from analyzer.publication import _json_dates, extract_publication_from_html, publication

        dates = []
        for key in posting.evidence:
            if key not in evidence:
                continue
            observation = evidence[key]
            try:
                dates.extend(publication(raw, "api") for raw in _json_dates(observation.json()))
            except (ValueError, TypeError):
                dates.append(extract_publication_from_html(observation.text))

        def utc(value):
            return (
                value.replace(tzinfo=timezone.utc)
                if value.tzinfo is None
                else value.astimezone(timezone.utc)
            )

        if not any(
            date.value and date.source != "relative_text" and utc(date.value) == utc(job.posted_at)
            for date in dates
        ):
            errors.append("Publication date lacks independently observed publication evidence")
    return errors


def assess(result: CollectionResult, observations, manifest):
    errors = list(result.diagnostics)
    spec = manifest.get("catalogue", {})
    pages, observed, totals, cursors, reported_totals = [], [], [], [], []
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
                if type(total) is not int and not (
                    isinstance(total, str) and re.fullmatch(r"\d+", total)
                ):
                    raise ValueError("Catalogue total must be an integer count")
                total = int(total)
                reported_totals.append(total)
                # Workday CXS supplies a count on offset zero and a zero sentinel
                # on subsequent nonempty pages. This protocol-specific exception
                # still requires the initial total to reconcile with every ID.
                p = urlsplit(obs.url)
                workday_sentinel = (
                    spec["format"] == "json"
                    and obs.method == "POST"
                    and bool(items)
                    and total == 0
                    and int((obs.request_body or {}).get("offset", 0)) > 0
                    and bool(totals)
                    and totals[0] > 0
                    and re.fullmatch(r"[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com", p.hostname or "")
                    and re.fullmatch(
                        rf"/wday/cxs/{re.escape(p.hostname.split('.')[0])}/[^/]+/jobs", p.path
                    )
                )
                if not workday_sentinel:
                    totals.append(total)
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
            if manifest.get("contract_version") == "2" and not complete_description(
                posting, observations, manifest
            ):
                errors.append(f"{posting.posting_id}: Complete description field was not extracted")
            errors.extend(
                f"{posting.posting_id}: {e}" for e in posting_errors(posting, observations)
            )
        if result.termination == "incomplete":
            errors.append("Connector did not reach catalogue termination")
        if not observed and not totals:
            errors.append("Empty catalogue lacks affirmative zero-count evidence")
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        errors.append(f"Observed catalogue schema changed: {type(exc).__name__}: {str(exc)[:200]}")
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
        "validator_version": "2",
        "catalogue_totals": reported_totals,
    }


def repeatable(first, second):
    return (
        first["outcome"] in {"complete", "empty"}
        and second["outcome"] in {"complete", "empty"}
        and first["identities"] == second["identities"]
    )
