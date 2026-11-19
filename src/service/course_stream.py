"""Consume course events in replay-safe PostgreSQL batches."""

import logging
import os
import re
import time
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import unquote
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from kafka import KafkaConsumer

from src.data_quality.quality_logging import log_validation_failure
from src.service.store import ProfileStore

logger = logging.getLogger(__name__)
WATCH_RE = re.compile(
    r"GET /data/m/(?P<movie>[^/\s]+)/(?P<minute>\d+)\.mpg(?:\s+HTTP/\S+)?"
)
RATE_RE = re.compile(
    r"GET /rate/(?P<movie>[^=\s]+)=(?P<rating>\d+)(?:\s+HTTP/\S+)?"
)
KINDS = ("account", "recommendation", "watch", "rating", "invalid", "ignored")


def _event_time(value):
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=ZoneInfo(os.getenv("COURSE_LOG_TIMEZONE", "UTC"))
        )
    return parsed


def _event_id(topic, partition, offset):
    return uuid5(NAMESPACE_URL, f"kafka:{topic}:{partition}:{offset}")


def _error(field, message):
    return {"loc": (field,), "type": "value_error", "msg": message}


def _valid_user_id(user_id):
    return re.fullmatch(r"[0-9]+", user_id) is not None


def _parse_line(topic, partition, offset, line):
    basic_parts = line.strip().split(",", 2)
    if len(basic_parts) != 3:
        return {"kind": "ignored"}
    timestamp, user_id, raw_event = [part.strip() for part in basic_parts]
    event = raw_event.split(",", 1)[0].strip()
    watch = WATCH_RE.fullmatch(event)
    rate = RATE_RE.fullmatch(event)
    account = re.fullmatch(r"GET /create_account/?(?:\s+HTTP/\S+)?", event)
    recommendation = event.startswith("recommendation request ")
    if (watch or rate or account) and not _valid_user_id(user_id):
        return {
            "kind": "invalid",
            "errors": [_error("user_id", "User identifier must contain ASCII digits")],
        }
    if watch or rate:
        try:
            parsed_time = _event_time(timestamp)
        except ValueError:
            return {"kind": "invalid", "errors": [_error("event_time", "Invalid event time")]}
        match = watch or rate
        kind = "watch" if watch else "rating"
        values = {
            "event_id": _event_id(topic, partition, offset),
            "user_id": user_id,
            "event_type": kind,
            "event_time": parsed_time,
            # Movie IDs are opaque catalog keys. Percent-decode, but preserve '+'.
            "movie_id": unquote(match.group("movie")),
        }
        if watch:
            values["minute_index"] = watch.group("minute")
        else:
            values["rating"] = rate.group("rating")
        return {"kind": kind, "event": values}
    if account:
        try:
            parsed_time = _event_time(timestamp)
        except ValueError:
            return {"kind": "invalid", "errors": [_error("event_time", "Invalid event time")]}
        return {
            "kind": "account",
            "event": {
                "event_id": _event_id(topic, partition, offset),
                "user_id": user_id,
                "event_type": "account_created",
                "event_time": parsed_time,
            },
        }
    if recommendation:
        return {
            "kind": "recommendation",
            "request": {
                "topic": topic,
                "partition_id": partition,
                "offset_id": offset,
                "raw_line": line,
            },
        }
    return {"kind": "ignored"}


def _deduplicate_errors(errors):
    unique = {}
    for error in errors:
        key = (tuple(error.get("loc", ())), error.get("type"))
        unique.setdefault(key, error)
    return list(unique.values())


def _process_records(store, records):
    counts = {kind: 0 for kind in KINDS}
    stream_records = []
    request_records = []
    errors = []
    rejected_count = 0
    for record in records:
        try:
            line = record.value.decode("utf-8")
        except UnicodeDecodeError:
            counts["invalid"] += 1
            rejected_count += 1
            errors.append(_error("query_params", "Invalid UTF-8 record"))
            continue
        parsed = _parse_line(record.topic, record.partition, record.offset, line)
        kind = parsed["kind"]
        if kind == "ignored":
            counts[kind] += 1
        elif kind == "invalid":
            counts[kind] += 1
            rejected_count += 1
            errors.extend(parsed["errors"])
        elif kind == "recommendation":
            request_records.append(parsed["request"])
        else:
            stream_records.append(parsed)

    result = store.record_batch(stream_records, request_records)
    for kind, count in result["counts"].items():
        counts[kind] += count
    counts["invalid"] += result["rejected_count"]
    errors.extend(result["errors"])
    rejected_count += result["rejected_count"]
    if errors:
        log_validation_failure(
            "kafka", _deduplicate_errors(errors),
            rejected_record_count=rejected_count,
        )
    return counts, result["inserted"], result["duplicates"]


def handle_line(store, topic, partition, offset, line):
    """Compatibility entry point for processing one decoded course line."""
    record = SimpleNamespace(
        topic=topic, partition=partition, offset=offset, value=line.encode("utf-8")
    )
    counts, _, _ = _process_records(store, [record])
    for kind in ("invalid", "account", "recommendation", "watch", "rating"):
        if counts[kind]:
            return kind
    return "ignored"


def _consumer_options():
    options = dict(
        bootstrap_servers=os.environ["KAFKA_BOOTSTRAP_SERVERS"].split(","),
        group_id=os.environ["KAFKA_GROUP_ID"], enable_auto_commit=False,
        auto_offset_reset="latest",
        security_protocol=os.getenv("KAFKA_SECURITY_PROTOCOL", "PLAINTEXT"),
        max_poll_records=int(os.getenv("KAFKA_MAX_POLL_RECORDS", "200")),
    )
    for env, name in (
        ("KAFKA_SASL_MECHANISM", "sasl_mechanism"),
        ("KAFKA_SASL_USERNAME", "sasl_plain_username"),
        ("KAFKA_SASL_PASSWORD", "sasl_plain_password"),
        ("KAFKA_SSL_CAFILE", "ssl_cafile"),
    ):
        if os.getenv(env):
            options[name] = os.environ[env]
    return options


def _lag(consumer):
    partitions = consumer.assignment()
    if not partitions:
        return 0, 0
    end_offsets = consumer.end_offsets(partitions)
    lags = [
        max(0, end_offsets[partition] - consumer.position(partition))
        for partition in partitions
    ]
    return sum(lags), max(lags, default=0)


def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    for key in ("KAFKA_BOOTSTRAP_SERVERS", "KAFKA_TOPIC", "KAFKA_GROUP_ID"):
        if not os.getenv(key):
            raise ValueError(f"Set {key} before enabling the course stream")
    consumer = KafkaConsumer(os.environ["KAFKA_TOPIC"], **_consumer_options())
    store = ProfileStore()
    metrics_interval = float(os.getenv("KAFKA_METRICS_INTERVAL_SECONDS", "30"))
    metrics_started = time.monotonic()
    interval_records = 0
    logger.info(
        "course_stream_started topic=%s new_group_starts_at=latest",
        os.environ["KAFKA_TOPIC"],
    )
    try:
        while True:
            batch = consumer.poll(timeout_ms=1000)
            records = [record for values in batch.values() for record in values]
            started = time.monotonic()
            counts, inserted, duplicates = _process_records(store, records)
            if batch:
                consumer.commit()
            duration = time.monotonic() - started
            interval_records += len(records)
            if records:
                logger.info(
                    "course_events records=%d inserted=%d duplicates=%d accounts=%d recommendations=%d watches=%d ratings=%d invalid=%d ignored=%d batch_seconds=%.3f",
                    len(records), inserted, duplicates, counts["account"],
                    counts["recommendation"], counts["watch"], counts["rating"],
                    counts["invalid"], counts["ignored"], duration,
                )
            now = time.monotonic()
            if now - metrics_started >= metrics_interval:
                total_lag, max_partition_lag = _lag(consumer)
                elapsed = now - metrics_started
                logger.info(
                    "course_stream_metrics throughput_per_second=%.2f lag_total=%d lag_max_partition=%d",
                    interval_records / elapsed, total_lag, max_partition_lag,
                )
                metrics_started = now
                interval_records = 0
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
