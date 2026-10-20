"""Independent matching policy; board display filters never change processing."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MatchingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    shortlist_threshold: float = Field(default=0.3, ge=0, le=1)
    archive_threshold: float = Field(default=0.1, ge=0, le=1)
    concurrency: int = Field(default=5, ge=1, le=20)
    daily_budget_usd: float | None = Field(default=None, gt=0, le=1000)
    max_attempt_usd: float = Field(default=1, gt=0, le=100)
    prices: dict[str, dict[str, float]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def thresholds(self):
        if self.archive_threshold > self.shortlist_threshold:
            raise ValueError("archive threshold must not exceed shortlist threshold")
        return self


def get_config():
    from config.runtime import get_setting

    return MatchingSettings.model_validate(get_setting("matching", MatchingSettings().model_dump()))
