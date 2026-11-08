from pydantic import BaseModel, Field, field_validator, model_validator


class MonitoringConfig(BaseModel):
    """
    Establishes operating setting for the monitoring script.
    """

    interval_seconds: int = Field(default=60, gt=0)
    window_hours: int = Field(default=24, gt=0)
    attribution_hours: int = Field(default=24, gt=0)
    positive_rating_threshold: int = Field(default=6, ge=1, le=10)
    page_size: int = Field(default=1000, ge=1, le=1000)

    @model_validator(mode="after")
    def check_window_coverage(self):
        if self.interval_seconds > self.window_hours * 3600:
            raise ValueError("interval_seconds must not excdeed the evaluation window")
        return self
