"""Persistent, non-secret runtime configuration."""
from __future__ import annotations

import json
import time
from copy import deepcopy
from typing import Any

from config.settings import settings

_CACHE_TTL_SECONDS = 5.0
_settings_cache: dict[str, tuple[float, dict[str, Any]]] = {}

LLM_OPERATIONS = (
    "cv_parsing",
    "keyword_extraction",
    "job_screening",
    "company_summary",
    "translation",
    "cv_tailoring",
    "cover_letter",
)

DEFAULT_LLM_CONFIG: dict[str, Any] = {
    "default_model": "",
    "global_fallbacks": [],
    "operations": {name: {"model": "", "fallbacks": []} for name in LLM_OPERATIONS},
    "timeout": 120,
    "retries": 1,
    "concurrency": 10,
}

DEFAULT_PRIORITY_CONFIG: dict[str, Any] = {
    "match_weight": 0.8,
    "freshness_weight": 0.2,
    "buckets": [
        {"max_hours": 24, "score": 1.0, "label": "TODAY"},
        {"max_hours": 72, "score": 0.8, "label": "3D"},
        {"max_hours": 168, "score": 0.6, "label": "7D"},
        {"max_hours": 336, "score": 0.3, "label": "14D"},
    ],
}

DEFAULT_AUTOMATION_CONFIG: dict[str, Any] = {
    "enabled": True,
    "timezone": "Europe/Zurich",
    "weekdays": [0, 1, 2, 3, 4],
    "start_hour": 7,
    "end_hour": 22,
    "interval_minutes": 30,
    "sources": ["jobs.ch", "jobscout24.ch", "jobup.ch", "linkedin.com"],
    "pages_per_source": 3,
    "max_age_days": 14,
    "include_unknown_dates": True,
    "enrich": True,
    "screen": True,
    "company_enrichment": True,
    "enrich_limit": 100,
    "date_backfill_limit": 20,
    "screen_limit_per_profile": 20,
    "company_limit": 10,
    "availability_limit": 20,
    "availability_interval_hours": 24,
    "concurrency": 5,
}


def _merge(default: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
    result = default.copy()
    for key, item in value.items():
        if isinstance(item, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], item)
        else:
            result[key] = item
    return result


def get_setting(key: str, default: dict[str, Any]) -> dict[str, Any]:
    from db.models import AppSetting
    from db.session import get_session, init_db

    cached = _settings_cache.get(key)
    if cached and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
        return deepcopy(cached[1])
    init_db()
    with get_session() as session:
        row = session.get(AppSetting, key)
        if not row:
            value = deepcopy(default)
            _settings_cache[key] = (time.monotonic(), value)
            return deepcopy(value)
        try:
            value = _merge(default, json.loads(row.value_json))
        except (TypeError, json.JSONDecodeError):
            value = deepcopy(default)
        _settings_cache[key] = (time.monotonic(), value)
        return deepcopy(value)


def put_setting(key: str, value: dict[str, Any]) -> None:
    from db.models import AppSetting
    from db.session import get_session, init_db

    init_db()
    with get_session() as session:
        row = session.get(AppSetting, key)
        encoded = json.dumps(value, ensure_ascii=False)
        if row:
            row.value_json = encoded
        else:
            session.add(AppSetting(key=key, value_json=encoded))
    _settings_cache.pop(key, None)


def get_llm_config() -> dict[str, Any]:
    config = get_setting("llm", DEFAULT_LLM_CONFIG)
    if not config["default_model"]:
        config["default_model"] = settings.openrouter_default_model or settings.openrouter_model
    return config


def get_automation_config() -> dict[str, Any]:
    return get_setting("automation", DEFAULT_AUTOMATION_CONFIG)


def validate_automation_config(value: dict[str, Any]) -> dict[str, Any]:
    config = _merge(DEFAULT_AUTOMATION_CONFIG, value)
    config["interval_minutes"] = max(5, min(1440, int(config["interval_minutes"])))
    config["start_hour"] = max(0, min(23, int(config["start_hour"])))
    config["end_hour"] = max(1, min(24, int(config["end_hour"])))
    if config["start_hour"] >= config["end_hour"]:
        raise ValueError("start_hour must be before end_hour")
    config["pages_per_source"] = max(1, min(20, int(config["pages_per_source"])))
    config["max_age_days"] = max(1, min(365, int(config["max_age_days"])))
    config["availability_limit"] = max(0, min(1000, int(config["availability_limit"])))
    config["enrich_limit"] = max(0, min(1000, int(config["enrich_limit"])))
    config["date_backfill_limit"] = max(0, min(1000, int(config["date_backfill_limit"])))
    config["screen_limit_per_profile"] = max(0, min(500, int(config["screen_limit_per_profile"])))
    config["company_limit"] = max(0, min(500, int(config["company_limit"])))
    config["availability_interval_hours"] = max(1, min(720, int(config["availability_interval_hours"])))
    config["concurrency"] = max(1, min(50, int(config["concurrency"])))
    config["weekdays"] = sorted({int(day) for day in config.get("weekdays", []) if 0 <= int(day) <= 6})
    config["sources"] = list(dict.fromkeys(str(s).strip() for s in config.get("sources", []) if str(s).strip()))
    return config


def validate_llm_config(value: dict[str, Any]) -> dict[str, Any]:
    config = _merge(DEFAULT_LLM_CONFIG, value)
    if not str(config.get("default_model", "")).strip():
        raise ValueError("default_model is required")
    config["timeout"] = max(5, min(600, int(config["timeout"])))
    config["retries"] = max(0, min(5, int(config["retries"])))
    config["concurrency"] = max(1, min(50, int(config["concurrency"])))
    for model in [config["default_model"], *config.get("global_fallbacks", [])]:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model slugs must be non-empty strings")
    return config


def get_priority_config() -> dict[str, Any]:
    return get_setting("search_priority", DEFAULT_PRIORITY_CONFIG)


def validate_priority_config(value: dict[str, Any]) -> dict[str, Any]:
    config = _merge(DEFAULT_PRIORITY_CONFIG, value)
    mw = float(config["match_weight"])
    fw = float(config["freshness_weight"])
    if mw < 0 or fw < 0 or abs(mw + fw - 1.0) > 0.0001:
        raise ValueError("match_weight + freshness_weight must equal 1.0")
    last = 0
    for bucket in config["buckets"]:
        hours = int(bucket["max_hours"])
        score = float(bucket["score"])
        if hours <= last or not 0 <= score <= 1:
            raise ValueError("freshness buckets must be ascending with scores between 0 and 1")
        last = hours
    return config
