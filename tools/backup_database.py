#!/usr/bin/env python3
"""SQLite online backup plus restore verification, without logging private rows."""

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path


def quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def snapshot(connection, specification=None):
    tables = specification or {
        row[0]: {
            "columns": [c[1] for c in connection.execute(f"PRAGMA table_info({quote(row[0])})")]
        }
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    result = {}
    for name, spec in tables.items():
        columns = ",".join(map(quote, spec["columns"]))
        digest, count = hashlib.sha256(), 0
        for row in connection.execute(f"SELECT {columns} FROM {quote(name)} ORDER BY {columns}"):
            digest.update(json.dumps(row, ensure_ascii=False, default=str).encode() + b"\n")
            count += 1
        result[name] = {"columns": spec["columns"], "count": count, "sha256": digest.hexdigest()}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--verify-manifest", type=Path)
    args = parser.parse_args()
    if args.verify_manifest:
        expected = json.loads(args.verify_manifest.read_text())
        with sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True) as db:
            actual = snapshot(db, expected)
        if actual != expected:
            raise SystemExit("Existing table data changed; inspect the preserved backup")
        print(json.dumps({"preserved_tables": len(actual), "verified": True}))
        return
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True) as source:
        with sqlite3.connect(args.output) as backup:
            source.backup(backup, pages=256)
            expected = snapshot(backup)
            with tempfile.TemporaryDirectory(prefix="sjh-restore-") as directory:
                with sqlite3.connect(Path(directory) / "restore.db") as restored:
                    backup.backup(restored)
                    if (
                        restored.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                        or snapshot(restored) != expected
                    ):
                        raise SystemExit("Backup restoration verification failed")
    manifest = args.output.with_suffix(".manifest.json")
    fd = os.open(manifest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(expected, stream)
    print(
        json.dumps(
            {
                "backup": str(args.output),
                "restore_verified": True,
                "counts": {name: spec["count"] for name, spec in expected.items()},
            }
        )
    )


if __name__ == "__main__":
    main()
