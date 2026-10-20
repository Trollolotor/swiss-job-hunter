#!/usr/bin/env python3
"""Read-only portal pilot in a new temporary database. Makes no LLM calls."""

import argparse
import asyncio
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--employer", default="Swissquote")
    parser.add_argument("--official", default="https://www.swissquote.com/en/careers")
    parser.add_argument(
        "--portal", default="https://careers.smartrecruiters.com/Swissquote/all-jobs"
    )
    parser.add_argument(
        "--output", required=True, help="New directory for the disposable database/evidence"
    )
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.environ["DATABASE_URL"] = f"sqlite:///{output / 'pilot.db'}"
    os.environ["CV_TEXT_PATH"] = str(output / "no-cv.txt")
    os.environ["EMPLOYER_ARTIFACT_DIR"] = str(output / "evidence")
    # Do not read real provider credentials, even if the repository has a .env.
    for key in ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "OLLAMA_BASE_URL"):
        os.environ[key] = ""
    os.environ["ANTHROPIC_API_KEY"] = "unused-pilot-placeholder"
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from config.runtime import put_setting
    from db.models import Employer
    from db.session import get_session, init_db
    from employers.config import EmployerSettings
    from employers.contracts import digest
    from employers.service import candidate, run_version, validate_activate, verify_and_create

    init_db()
    put_setting("employer_sources", EmployerSettings(enabled=True, shadow=True).model_dump())
    with get_session() as session:
        employer = Employer(name=args.employer, identity_key=digest(args.employer))
        session.add(employer)
        session.flush()
        employer_id = employer.id

    async def pilot():
        source_id = await verify_and_create(employer_id, args.official, args.portal)
        print(json.dumps({"source_id": source_id, "stage": "verified"}), flush=True)
        version_id = candidate(source_id)
        await validate_activate(version_id)
        print(
            json.dumps({"version_id": version_id, "stage": "validated and activated"}), flush=True
        )
        result = await run_version(version_id)
        print(json.dumps({"stage": "routine shadow collection", "result": result}), flush=True)

    try:
        asyncio.run(pilot())
    except Exception:
        from db.models import ConnectorVersion

        with get_session() as session:
            version = session.query(ConnectorVersion).order_by(ConnectorVersion.id.desc()).first()
            if version:
                print(
                    json.dumps({"validation_failure": json.loads(version.validation_json)}),
                    flush=True,
                )
        raise


if __name__ == "__main__":
    main()
