"""Extraction quality is evidence about content, not a character-count heuristic."""

import json
import re
from datetime import datetime, timedelta

QUALITIES = {"complete", "preview", "failed", "insufficient", "unknown"}
_STUB = re.compile(r"^we are looking for .{0,300} to join our team[.!]?$", re.I)


def detail_is_complete(html, description):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    norm = lambda s: " ".join(s.split()).casefold()
    target = norm(description)
    if not target:
        return False
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            nodes = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        pending = nodes if isinstance(nodes, list) else [nodes]
        while pending:
            node = pending.pop()
            if not isinstance(node, dict):
                continue
            pending.extend(node.get("@graph", []))
            if node.get("@type") == "JobPosting" and node.get("description"):
                text = BeautifulSoup(node["description"], "lxml").get_text(" ", strip=True)
                if norm(text) == target:
                    return True
    for node in soup.select(
        '[data-cy="vacancy-description"], [class*="grid-area_description"], '
        '[itemprop="description"], [data-testid="job-description"]'
    ):
        if norm(node.get_text(" ", strip=True)) == target:
            return True
    return False


def quality(text, *, trusted_detail=False, declared=None):
    value = (text or "").strip()
    if not value:
        return "failed" if declared == "failed" else "insufficient"
    if _STUB.match(value) or declared == "preview":
        return "preview"
    if declared in {"failed", "insufficient"}:
        return declared
    if trusted_detail:
        return "complete"
    return "unknown"


def set_description(
    session, job, text, *, trusted_detail=False, source="unknown", evidence=None, declared=None
):
    from db.models import JobProfile

    text = (text or "").strip()
    state = quality(text, trusted_detail=trusted_detail, declared=declared)
    if job.description_quality == "complete" and state != "complete":
        return False
    changed = (job.description or "").strip() != text
    if text:
        job.description = text
    job.description_quality = state
    job.extraction_json = json.dumps(
        {"source": source, "evidence": evidence or [], "quality": state}
    )
    if state == "complete":
        job.enrichment_attempts, job.enrichment_due_at = 0, None
    if changed:
        session.query(JobProfile).filter_by(job_id=job.id).update(
            {JobProfile.score_stale: True}, synchronize_session=False
        )
    return changed


def extraction_failed(job, reason):
    # A failed refresh does not destroy previously verified content.
    attempts = (job.enrichment_attempts or 0) + 1
    job.enrichment_attempts = attempts
    job.enrichment_due_at = datetime.utcnow() + timedelta(hours=min(168, 2 ** min(attempts, 8)))
    if job.description_quality != "complete":
        job.description_quality = "failed"
        job.extraction_json = json.dumps({"quality": "failed", "reason": str(reason)[:500]})


def needs_enrichment(job, now=None):
    return job.description_quality != "complete" and (
        not job.enrichment_due_at or job.enrichment_due_at <= (now or datetime.utcnow())
    )
