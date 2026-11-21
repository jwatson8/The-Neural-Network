"""Tests for client-observed availability monitoring buckets."""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from pydantic import ValidationError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from src.database.interactions import course_request_availability
from src.database.models import CourseRequest
from src.monitoring.calculations import calculate_availability_buckets
from src.monitoring.schemas import (
    AvailabilitySummary,
    EvaluationWindow,
    MonitoringConfig,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def line(requested_at: datetime, status: int | None, ms: int = 100) -> str:
    stamp = requested_at.strftime("%Y-%m-%dT%H:%M:%S.%f") + "379"
    status_part = f", status {status}" if status is not None else ""
    return (
        f"{stamp},42,recommendation request host:8082{status_part}, "
        f"result: a+1999, b+2001, {ms} ms"
    )


class AvailabilitySummaryTests(unittest.TestCase):
    def test_outcomes_must_sum_to_request_count(self):
        with self.assertRaisesRegex(ValidationError, "sum to request_count"):
            AvailabilitySummary(request_count=3, success_count=1, no_response_count=1)

    def test_defaults_are_an_empty_bucket(self):
        self.assertEqual(AvailabilitySummary().request_count, 0)


class AvailabilityCalculationTests(unittest.TestCase):
    def window(self, offset_minutes: int) -> EvaluationWindow:
        start = START + timedelta(minutes=offset_minutes)
        return EvaluationWindow(start=start, end=start + timedelta(minutes=5))

    def test_rows_map_to_windows_and_derive_other_failures(self):
        aggregate = Mock(
            return_value=[
                {
                    "bucket_start": START.replace(tzinfo=None),
                    "request_count": 10,
                    "success_count": 7,
                    "no_response_count": 2,
                },
                {
                    "bucket_start": START + timedelta(hours=1),
                    "request_count": 5,
                    "success_count": 5,
                    "no_response_count": 0,
                },
            ]
        )
        first, second = self.window(0), self.window(5)

        summaries = calculate_availability_buckets(
            [first, second], MonitoringConfig(), aggregate=aggregate
        )

        aggregate.assert_called_once_with(
            start=first.start, end=second.end, bucket_minutes=5
        )
        self.assertEqual(
            summaries[first.start],
            AvailabilitySummary(
                request_count=10,
                success_count=7,
                no_response_count=2,
                other_failure_count=1,
            ),
        )
        self.assertEqual(summaries[second.start], AvailabilitySummary())
        self.assertEqual(set(summaries), {first.start, second.start})

    def test_no_windows_skips_the_database(self):
        aggregate = Mock()
        self.assertEqual(
            calculate_availability_buckets([], MonitoringConfig(), aggregate=aggregate),
            {},
        )
        aggregate.assert_not_called()


class CourseRequestAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        engine_patch = patch(
            "src.database.database.get_engine", return_value=self.engine
        )
        engine_patch.start()
        self.addCleanup(engine_patch.stop)
        self.offset = 0

    def add(self, raw_line: str, received_at: datetime):
        with Session(self.engine) as session:
            session.add(
                CourseRequest(
                    topic="movielog",
                    partition_id=0,
                    offset_id=self.offset,
                    received_at=received_at,
                    raw_line=raw_line,
                )
            )
            session.commit()
        self.offset += 1

    def test_buckets_by_logged_request_time_and_classifies_status(self):
        late = START + timedelta(minutes=30)
        for seconds, status in ((0, 200), (10, 200), (20, 0), (30, 503), (40, None)):
            requested_at = START + timedelta(seconds=seconds)
            # Received long after it was logged: still counts in its own bucket.
            self.add(line(requested_at, status), late)
        self.add(line(START + timedelta(minutes=5), 200), START + timedelta(minutes=5))
        # Outside the half-open range on both sides.
        self.add(line(START - timedelta(seconds=1), 200), START)
        self.add(line(START + timedelta(minutes=10), 0), late)
        # Unparseable timestamp falls back to received_at.
        self.add("not-a-time,42,recommendation request host:8082, status 0", START)

        rows = course_request_availability.aggregate(
            start=START, end=START + timedelta(minutes=10), bucket_minutes=5
        )

        self.assertEqual(
            rows,
            [
                {
                    "bucket_start": START,
                    "request_count": 6,
                    "success_count": 2,
                    "no_response_count": 2,
                },
                {
                    "bucket_start": START + timedelta(minutes=5),
                    "request_count": 1,
                    "success_count": 1,
                    "no_response_count": 0,
                },
            ],
        )

    def test_postgres_parses_and_groups_in_database(self):
        session = Mock()
        session.execute.return_value.mappings.return_value = []

        rows = course_request_availability._postgres_aggregate(
            session, start=START, end=START + timedelta(minutes=5), bucket_minutes=5
        )

        statement = str(session.execute.call_args.args[0])
        parameters = session.execute.call_args.args[1]
        self.assertEqual(rows, [])
        self.assertIn("date_bin", statement)
        self.assertIn("status = '200'", statement)
        self.assertIn("status = '0'", statement)
        self.assertEqual(
            parameters["received_from"],
            START - course_request_availability.RECEIVED_SKEW,
        )


if __name__ == "__main__":
    unittest.main()
