"""Trusted version-scoped details; generated code never gets a database handle."""

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from db.models import DetailCache
from db.session import get_session
from employers.contracts import CollectedPosting
from employers.network import CollectionLimit, Observation


def cached(context, entry):
    if getattr(context, "validation", True):
        return None
    with get_session() as session:
        row = (
            session.query(DetailCache)
            .filter_by(
                source_id=context.source_id, version_id=context.version_id, identity=entry.identity
            )
            .first()
        )
        if (
            not row
            or datetime.utcnow() - row.fetched_at >= timedelta(hours=24)
            or row.token != entry.change_token
        ):
            return None
        observations = [Observation(**o) for o in json.loads(row.evidence_json)]
        added = sum(len(o.text.encode()) for o in observations)
        if (
            context.bytes + getattr(context, "cached_bytes", 0) + added
            > context.settings.total_bytes
        ):
            raise CollectionLimit("Cached evidence exceeds collection budget")
        posting = CollectedPosting.model_validate_json(row.posting_json)
        if getattr(context, "manifest", {}).get("ats_library"):
            # Catalogue bytes are always fresh. Bind cached detail evidence to
            # this run's record instead of replaying an old whole-board snapshot.
            posting.evidence = [entry.metadata["catalogue_key"], *[o.key for o in observations]]
    context.cached_bytes = getattr(context, "cached_bytes", 0) + added
    context.observations.extend(observations)
    context.cache_hits = getattr(context, "cache_hits", 0) + 1
    return posting


def save(session, context, entries, postings):
    if getattr(context, "validation", True):
        return
    spec = getattr(context, "catalogue_spec", {})
    suffix = spec.get("path_suffix", "")
    for posting in postings:
        entry = next(
            (
                e
                for e in entries
                if e.posting_id == posting.posting_id
                and (not e.variant or e.variant == posting.variant)
            ),
            None,
        )
        if not entry:
            continue
        # Store detail evidence only; replaying old list pages would contaminate
        # current count/termination assessment. The new list is always fetched.
        observations = [
            o
            for o in context.observations
            if o.key in posting.evidence
            and (not suffix or not urlsplit(o.url).path.endswith(suffix))
        ]
        if not observations:
            continue
        existing = (
            session.query(DetailCache)
            .filter_by(
                source_id=context.source_id, version_id=context.version_id, identity=entry.identity
            )
            .first()
        )
        if not existing:
            existing = DetailCache(
                source_id=context.source_id, version_id=context.version_id, identity=entry.identity
            )
            session.add(existing)
        from employers.validation import posting_errors

        manifest = getattr(context, "manifest", None)
        checked = context.observations if manifest and "ats_library" in manifest else observations
        if posting_errors(posting, checked, manifest):
            # Some connectors need listing fields too; safely fetch details again.
            if existing.id is None:
                session.expunge(existing)
            continue
        # A cache hit must not extend its own TTL indefinitely.
        fresh_keys = getattr(context, "fresh_detail_keys", set())
        if existing.id is not None and not any(o.key in fresh_keys for o in observations):
            continue
        existing.token = entry.change_token
        existing.posting_json = posting.model_dump_json()
        existing.evidence_json = json.dumps([asdict(o) for o in observations])
        existing.fetched_at = datetime.utcnow()
    session.query(DetailCache).filter(
        DetailCache.fetched_at < datetime.utcnow() - timedelta(days=2)
    ).delete(synchronize_session=False)
    session.flush()
    from sqlalchemy import LargeBinary, cast, func

    size_expr = func.length(cast(DetailCache.evidence_json, LargeBinary)) + func.length(
        cast(DetailCache.posting_json, LargeBinary)
    )
    total = session.query(func.sum(size_expr)).scalar() or 0
    if total > 128 * 1024 * 1024:
        for row, size in session.query(DetailCache, size_expr).order_by(DetailCache.fetched_at):
            session.delete(row)
            total -= size
            if total <= 128 * 1024 * 1024:
                break
