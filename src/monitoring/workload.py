"""Reconcile and persist bounded monitoring buckets."""

from datetime import UTC, datetime, timedelta

from src.database.interactions import monitoring_runs
from src.monitoring.buckets import floor_to_bucket, utc_datetime, windows_between
from src.monitoring.calculations import (
    calculate_license_fee_buckets,
    calculate_live_model_accuracy_buckets,
    calculate_llm_buckets,
)
from src.monitoring.schemas import MonitoringConfig, MonitorResult


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _target_windows(config: MonitoringConfig, bucket_end: datetime):
    history_start = bucket_end - timedelta(hours=config.backfill_hours)
    all_windows = windows_between(
        history_start,
        bucket_end,
        config.bucket_minutes,
    )
    stored = {
        (utc_datetime(start), utc_datetime(end))
        for start, end in monitoring_runs.list_windows(
            start=history_start,
            end=bucket_end,
        )
    }
    reconciliation_start = bucket_end - timedelta(
        hours=config.reconciliation_hours
    )
    return [
        window
        for window in all_windows
        if window.start >= reconciliation_start
        or (window.start, window.end) not in stored
    ]


def run_monitoring_workload(config: MonitoringConfig):
    """Calculate and atomically upsert recent and missing completed buckets."""
    generated_at = _utc_now()
    bucket_end = floor_to_bucket(generated_at, config.bucket_minutes)
    windows = _target_windows(config, bucket_end)
    if not windows:
        return []

    licensing = calculate_license_fee_buckets(windows, config)
    llm = calculate_llm_buckets(windows, config)
    quality = calculate_live_model_accuracy_buckets(windows, config)
    results = [
        MonitorResult(
            generated_at=generated_at,
            window=window,
            licensing=licensing[window.start],
            llm=llm[window.start],
            quality=quality[window.start],
        )
        for window in windows
    ]
    return monitoring_runs.upsert_many(results)
