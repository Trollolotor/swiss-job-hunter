#!/usr/bin/env python3
"""Apply explicit pilot caps using verified conservative OpenRouter prices."""

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--employer-budget", required=True, type=float)
    parser.add_argument("--matching-budget", required=True, type=float)
    parser.add_argument("--pause-screening", action="store_true")
    parser.add_argument(
        "--prepare-screening-prompt",
        action="store_true",
        help="Create a prompt revision with room for requirement evidence; preserve prompt text",
    )
    parser.add_argument(
        "--bounded-reasoning",
        action="store_true",
        help="Disable default thinking for these structured operations only when every routed model advertises support",
    )
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from analyzer.config import MatchingSettings
    from analyzer.config import get_config as matching_config
    from config.runtime import (
        get_automation_config,
        get_llm_config,
        put_setting,
        validate_llm_config,
    )
    from employers.config import get_config as employer_config
    from llm.task_router import resolve_models

    operations = [
        "job_screening",
        "keyword_extraction",
        "employer_resolution",
        "connector_investigation",
        "connector_generation",
        "connector_repair",
    ]
    models = sorted({m for op in operations for m in resolve_models(op)})
    reasoning_overrides = {}
    if args.bounded_reasoning:
        with urlopen("https://openrouter.ai/api/v1/models", timeout=30) as response:
            metadata = {row["id"]: row for row in json.load(response)["data"]}
        llm = get_llm_config()
        for operation in operations:
            supported = [
                metadata.get(model, {}).get("reasoning", {}) for model in resolve_models(operation)
            ]
            if all(
                "none" in info.get("supported_efforts", []) and not info.get("mandatory")
                for info in supported
            ):
                llm["operations"][operation]["reasoning_effort"] = "none"
                reasoning_overrides[operation] = "none"
        llm = validate_llm_config(llm)
    prices = {}
    for model in models:
        url = "https://openrouter.ai/api/v1/models/" + model + "/endpoints"
        with urlopen(url, timeout=30) as response:
            endpoints = json.load(response)["data"]["endpoints"]
        if not endpoints:
            raise RuntimeError(f"No verified pricing available for {model}")
        maxima = {}
        for key in ("prompt", "completion"):
            values = [float(e["pricing"][key]) * 1e6 for e in endpoints]
            if not all(math.isfinite(value) and value >= 0 for value in values):
                raise RuntimeError("Invalid public pricing metadata")
            maxima[key] = math.ceil(max(values) * 1.25 * 100) / 100
        prices[model] = {
            "input_per_million": maxima["prompt"],
            "output_per_million": maxima["completion"],
            "search_request": 0.015,
        }
    employer = employer_config().model_copy(
        update={"daily_budget_usd": args.employer_budget, "prices": prices}
    )
    matching = MatchingSettings.model_validate(
        {
            **matching_config().model_dump(),
            "daily_budget_usd": args.matching_budget,
            "prices": prices,
        }
    )
    # Persist caps without enabling automatic employer work or changing coverage.
    put_setting(
        "employer_sources", type(employer).model_validate(employer.model_dump()).model_dump()
    )
    put_setting("matching", matching.model_dump())
    if args.bounded_reasoning:
        put_setting("llm", llm)
    if args.prepare_screening_prompt:
        from llm.prompt_manager import get_prompt, save_prompt

        prompt = get_prompt("job_screening")
        if prompt["max_tokens"] < 3000:
            save_prompt(
                "job_screening",
                {
                    "system_template": prompt["system_template"],
                    "user_template": prompt["user_template"],
                    "temperature": prompt["temperature"],
                    "max_tokens": 3000,
                },
            )
    if args.pause_screening:
        automation = get_automation_config()
        automation["screen"] = False
        put_setting("automation", automation)
    audit = {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "model_endpoint_sources": models,
        "margin": 1.25,
        "search_price_source": "https://openrouter.ai/docs/guides/features/server-tools/web-search",
        "prices": prices,
        "employer_cap_usd": args.employer_budget,
        "matching_cap_usd": args.matching_budget,
        "reasoning_overrides": reasoning_overrides,
    }
    put_setting("pilot_pricing_audit", audit)
    print(json.dumps(audit))


if __name__ == "__main__":
    main()
