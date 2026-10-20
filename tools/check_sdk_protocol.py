#!/usr/bin/env python3
"""Exercise SDK v2 through the installed launcher with an offline HTTP broker."""

import argparse
import asyncio
import json
import os
import tempfile

CODE = """
def enumerate(config, sdk):
    data, evidence = sdk.json("https://catalogue.example/jobs?offset=0")
    return {"entries":[{"posting_id":item["id"],"detail_url":item["url"]} for item in data["jobs"]],
            "observed_ids":[item["id"] for item in data["jobs"]],
            "reported_total":data["total"],"termination":"total"}

def fetch_detail(config, entry, sdk):
    print("Diagnostic output must stay out of the JSON protocol")
    data, evidence = sdk.json(entry["detail_url"])
    return {"data":data,"evidence":evidence}

def normalize(config, entry, raw):
    data=raw["data"]
    return {"posting_id":entry["posting_id"],"evidence":[raw["evidence"]],
            "application_url":data["url"],
            "job":{"title":data["title"],"company":data["company"],"location":"Bern",
                   "description":data["description"],"url":data["url"],"source":"fixture"}}
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="sjh-sdk-check-") as directory:
        os.environ["DATABASE_URL"] = f"sqlite:///{directory}/check.db"
        os.environ["CV_TEXT_PATH"] = f"{directory}/missing.txt"
        for name in ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "OLLAMA_BASE_URL"):
            os.environ[name] = ""
        os.environ["ANTHROPIC_API_KEY"] = "unused"
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import httpx

        from employers.config import EmployerSettings
        from employers.contracts import CollectionResult
        from employers.network import CollectionContext
        from employers.runtime import run_isolated
        from employers.validation import assess

        def handler(request):
            if request.url.path == "/jobs":
                return httpx.Response(
                    200,
                    json={
                        "total": 1,
                        "jobs": [{"id": "one", "url": "https://catalogue.example/job/one"}],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "title": "Engineer",
                    "company": "Acme",
                    "url": str(request.url),
                    "description": "Maintain Python services in Bern.",
                },
            )

        async def check():
            context = CollectionContext(
                [{"host": "catalogue.example", "path": "/"}],
                EmployerSettings(domain_delay_seconds=2),
                transport=httpx.MockTransport(handler),
            )
            context.validation, context.staged, context.fresh_detail_keys = True, [], set()
            try:
                raw = await run_isolated(
                    "python",
                    {"code": CODE, "config": {}, "sdk_version": "2"},
                    context,
                    image_digest=args.image,
                )
                result = CollectionResult.model_validate(raw)
                report = assess(
                    result,
                    context.observations,
                    {
                        "contract_version": "2",
                        "catalogue": {
                            "format": "json",
                            "path_suffix": "/jobs",
                            "items": "jobs",
                            "id": "id",
                            "total": "total",
                            "pagination": "offset",
                        },
                    },
                )
                if report["outcome"] != "complete" or len(context.staged) != 1:
                    raise RuntimeError(json.dumps(report))
                print(
                    json.dumps(
                        {
                            "sdk": "2",
                            "outcome": report["outcome"],
                            "requests": context.requests,
                            "staged": len(context.staged),
                            "logging_isolated": True,
                        }
                    )
                )
            finally:
                await context.close()

        asyncio.run(check())


if __name__ == "__main__":
    main()
