"""Validated, nonsecret feature settings; unset budgets never authorize spending."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EmployerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    enabled: bool = False
    shadow: bool = True
    coverage: Literal["swiss", "swiss_remote", "worldwide"] = "swiss_remote"
    poll_hours: int = Field(default=3, ge=2, le=168)
    concurrency: int = Field(default=2, ge=1, le=4)
    onboarding_per_day: int = Field(default=3, ge=0, le=100)
    include_agencies: bool = False
    max_connected: int = Field(default=10, ge=1, le=1000)
    daily_budget_usd: float | None = Field(default=None, gt=0, le=1000)
    repair_budget_fraction: float = Field(default=0.3, ge=0, le=1)
    max_task_usd: float = Field(default=1.0, gt=0, le=100)
    prices: dict[str, dict[str, float]] = Field(default_factory=dict)
    max_requests: int = Field(default=500, ge=1, le=2000)
    max_pages: int = Field(default=100, ge=1, le=500)
    response_bytes: int = Field(default=5 * 1024 * 1024, ge=1024, le=5 * 1024 * 1024)
    total_bytes: int = Field(default=100 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    runtime_seconds: int = Field(default=600, ge=10, le=600)
    domain_delay_seconds: float = Field(default=2, ge=2, le=60)


def get_config() -> EmployerSettings:
    from config.runtime import get_setting

    return EmployerSettings.model_validate(
        get_setting("employer_sources", EmployerSettings().model_dump())
    )


def enabled() -> bool:
    return get_config().enabled
