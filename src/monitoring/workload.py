"""Run and persist one complete monitoring evaluation."""

from datetime import UTC, datetime, timedelta

from src.database.interactions import monitoring_runs
from src.monitoring.calculations import (
    calculate_license_fees,
    calculate_live_model_accuracy,
    calculate_llm,
)
from src.monitoring.schemas import EvaluationWindow, MonitoringConfig, MonitorResult


def _utc_now() -> datetime:
    return datetime.now(UTC)


def run_monitoring_workload(config: MonitoringConfig):
    """Calculate, persist, and return one complete monitoring run."""
    window_end = _utc_now()
    window = EvaluationWindow(
        start=window_end - timedelta(hours=config.window_hours),
        end=window_end,
    )

    licensing = calculate_license_fees(window, config)
    llm = calculate_llm(window, config)
    quality = calculate_live_model_accuracy(window, config)
    result = MonitorResult(
        generated_at=_utc_now(),
        window=window,
        licensing=licensing,
        llm=llm,
        quality=quality,
    )
    return monitoring_runs.create(result)
