"""Bounded diagnostic store, with active and previous-version fixtures pinned."""

import json
import os
import time
from pathlib import Path

from employers.contracts import digest

CAP = 1024**3


def root():
    p = Path(os.environ.get("EMPLOYER_ARTIFACT_DIR", "data/employer-diagnostics"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def save(observations, failure=False):
    value = [o.__dict__ for o in observations]
    raw = json.dumps(value, ensure_ascii=False).encode()
    if len(raw) > 100 * 1024 * 1024:
        raise ValueError("Diagnostic bundle exceeds limit")
    key = digest(value)
    cleanup()
    directory = root()
    if sum(p.stat().st_size for p in directory.glob("*.json")) + len(raw) > CAP:
        raise ValueError("Diagnostic retention cap exhausted")
    target = directory / (key + (".failure.json" if failure else ".success.json"))
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(raw)
    temporary.replace(target)
    return target.name


def load(key):
    from employers.network import Observation

    if not key or Path(key).name != key or not key.endswith(".json"):
        raise ValueError("Invalid diagnostic reference")
    raw = (root() / key).read_bytes()
    return [Observation(**v) for v in json.loads(raw)]


def pin(source_id, refs):
    # A single per-source index protects fixtures for active and rollback versions.
    (root() / f"source-{int(source_id)}.pins").write_text(json.dumps(list(set(refs))))


def cleanup():
    directory = root()
    pinned = set()
    for p in directory.glob("*.pins"):
        pinned.update(json.loads(p.read_text()))
    files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
    size = sum(p.stat().st_size for p in files)
    for p in files:
        age = time.time() - p.stat().st_mtime
        ttl = (14 if ".failure." in p.name else 7) * 86400
        if p.name not in pinned and (age > ttl or size > CAP):
            size -= p.stat().st_size
            p.unlink()
