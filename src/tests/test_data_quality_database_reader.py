"""Mocked reads using Nathan's real page/cursor and record shapes."""

import json
import logging
import unittest
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock, patch
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from src.database.interactions import SearchCursor, SearchPage
from src.database.models import LLMUsage, RecommendationServe, StreamEvent
from src.data_quality.database_reader import (
    DatabaseReadError, DatabaseReader, detect_database_drift, read_event_snapshots,
)


class DatabaseReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 1, 1, tzinfo=UTC)
        self.end = self.start + timedelta(days=1)
        self.logger = logging.getLogger(f"database-reader-test.{self.id()}")
        self.search = Mock(return_value=SearchPage([], None))
        self.reader = DatabaseReader(event_search=self.search, serve_search=self.search,
                                     usage_search=self.search, page_size=2, logger=self.logger)
        self.event = StreamEvent(
            event_id=UUID(int=1), user_id="private-user", movie_id="private-movie",
            event_type="rating", event_time=self.start, stored_at=self.end, rating=7,
        )

    def read(self, method="read_events", **kwargs):
        return getattr(self.reader, method)(start=self.start, end=self.end, **kwargs)

    def windows(self) -> dict:
        return dict(baseline_start=self.start, baseline_end=self.end,
                    recent_start=self.end, recent_end=self.end + timedelta(days=1))

    def test_success_preserves_event_data(self) -> None:
        self.search.return_value = SearchPage([self.event], None)
        rows = self.read()
        self.assertEqual(rows, [self.event.model_dump()])
        self.assertIsInstance(rows[0]["event_id"], UUID)
        self.assertIs(rows[0]["event_time"], self.start)
        self.assertEqual(rows[0]["stored_at"], self.end)

    def test_empty_reads(self) -> None:
        for method in ("read_events", "read_watch_events", "read_rating_events",
                       "read_recommendation_serves", "read_llm_usage"):
            with self.subTest(method=method):
                self.assertEqual(self.read(method), [])

    def test_filters_and_window_are_forwarded(self) -> None:
        self.read("read_rating_events", user_id="private-user", movie_id="private-movie",
                  time_basis="stored_at")
        self.search.assert_called_once_with(
            start=self.start, end=self.end, cursor=None, limit=2, event_type="rating",
            time_basis="stored_at", user_id="private-user", movie_id="private-movie",
        )
        self.search.reset_mock()
        self.read("read_watch_events")
        self.assertEqual(self.search.call_args.kwargs["event_type"], "watch")

    def test_all_event_types_are_requested_without_filter(self) -> None:
        self.read()
        self.assertIsNone(self.search.call_args.kwargs["event_type"])

    def test_utc_normalization_and_half_open_window_contract(self) -> None:
        local = timezone(timedelta(hours=-5))
        self.reader.read_events(start=self.start.astimezone(local), end=self.end.astimezone(local))
        self.assertEqual(self.search.call_args.kwargs["start"].tzinfo, UTC)
        self.assertEqual(self.search.call_args.kwargs["end"], self.end)
        # Filtering itself remains Nathan's responsibility; fake it using his
        # verified start-inclusive/end-exclusive policy to test the adapter.
        records = [{"event_time": when} for when in (
            self.start - timedelta(seconds=1), self.start, self.end,
        )]
        self.search.side_effect = lambda **kw: SearchPage([
            row for row in records if kw["start"] <= row["event_time"] < kw["end"]
        ], None)
        self.assertEqual(self.read(), [{"event_time": self.start}])

    def test_pagination_exhausts_all_pages_without_sorting(self) -> None:
        first = SearchCursor("stream_events:event_time", self.start, UUID(int=3))
        second = SearchCursor("stream_events:event_time", self.start, UUID(int=2))
        rows = [{"event_id": UUID(int=index), "rating": index} for index in (3, 2, 1)]
        self.search.side_effect = [SearchPage(rows[:1], first), SearchPage(rows[1:2], second),
                                   SearchPage(rows[2:], None)]
        self.assertEqual(self.read(), rows)
        self.assertEqual([call.kwargs["cursor"] for call in self.search.call_args_list],
                         [None, first, second])
        for call in self.search.call_args_list:
            self.assertEqual(call.kwargs["start"], self.start)
            self.assertEqual(call.kwargs["limit"], 2)

    def test_audit_readers_paginate(self) -> None:
        for method in ("read_recommendation_serves", "read_llm_usage"):
            with self.subTest(method=method):
                self.search.side_effect = [SearchPage([{"request_id": UUID(int=1)}], "cursor"),
                                           SearchPage([{"request_id": UUID(int=2)}], None)]
                self.assertEqual(len(self.read(method)), 2)

    def test_serve_telemetry_preserved(self) -> None:
        serve = RecommendationServe(
            request_id=UUID(int=10), user_id="private-user", served_at=self.end,
            model_route="test-route", model_version="test-version",
            recommendations=[{"movie_id": "private-z", "model_score": 0.8},
                             {"movie_id": "private-a", "model_score": -0.5}],
            request_received_at=self.start, serve_duration_ms=1.25,
        )
        self.search.return_value = SearchPage([serve], None)
        rows = self.read("read_recommendation_serves", model_version="test-version", model_route="test-route")
        self.assertEqual(rows, [serve.model_dump()])
        self.assertEqual(self.search.call_args.kwargs["model_version"], "test-version")

    def test_llm_cost_and_tokens_preserved(self) -> None:
        usage = LLMUsage(
            request_id=UUID(int=10), user_id="private-user", occurred_at=self.start,
            provider="test-provider", model_name="test-model", status="succeeded",
            input_tokens=100, output_tokens=50, estimated_cost_usd=Decimal("0.00123456"),
        )
        self.search.return_value = SearchPage([usage], None)
        rows = self.read("read_llm_usage", provider="test-provider", model_name="test-model", status="succeeded")
        self.assertEqual(rows, [usage.model_dump()])
        self.assertIsInstance(rows[0]["estimated_cost_usd"], Decimal)
        self.assertEqual(self.search.call_args.kwargs["status"], "succeeded")

    def test_mapping_rows_are_copied_without_dropping_bad_values(self) -> None:
        raw = {"rating": None, "recommendations": [{"movie_id": "private-movie"}]}
        self.search.return_value = SearchPage([raw], None)
        rows = self.read()
        self.assertEqual(rows[0]["rating"], None)
        rows[0]["recommendations"][0]["movie_id"] = "changed"
        self.assertEqual(raw["recommendations"][0]["movie_id"], "private-movie")

    def test_database_failure_is_logged_and_raised_without_partial_result(self) -> None:
        error = SQLAlchemyError("credentials-private-user-private-movie")
        self.search.side_effect = [SearchPage([self.event], "cursor"), error]
        with self.assertLogs(self.logger, level="INFO") as captured:
            with self.assertRaises(SQLAlchemyError) as raised:
                self.read()
        self.assertIs(raised.exception, error)
        payload = json.loads(captured.records[-1].getMessage())
        self.assertEqual(payload["operation"], "database_read_failed")
        self.assertEqual(payload["records_processed"], 1)
        self.assertFalse(payload["pagination_complete"])
        self.assertEqual(captured.records[-1].levelno, logging.ERROR)
        self.assertNotIn("credentials", "".join(captured.output))

    def test_malformed_pages_and_records_fail_explicitly(self) -> None:
        for page in (None, SearchPage(None, None), SearchPage(["bad-row"], None), SearchPage([], "cursor")):
            with self.subTest(page=page):
                self.search.return_value = page
                with self.assertLogs(self.logger, level="ERROR"):
                    with self.assertRaises(DatabaseReadError):
                        self.read()

    def test_repeated_cursor_is_not_infinite_or_silently_truncated(self) -> None:
        self.search.return_value = SearchPage([self.event], "same-cursor")
        with self.assertLogs(self.logger, level="ERROR"):
            with self.assertRaises(DatabaseReadError):
                self.read()
        self.assertEqual(self.search.call_count, 2)

    def test_invalid_parameters_fail_before_reading(self) -> None:
        for start, end in ((None, self.end), (self.start.replace(tzinfo=None), self.end),
                           (self.start, self.start), (self.end, self.start)):
            with self.subTest(start=start), self.assertRaises(ValueError):
                self.reader.read_events(start=start, end=end)
        for kwargs in ({"event_type": "bad"}, {"time_basis": "bad"}, {"user_id": " "},
                       {"movie_id": 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.read(**kwargs)
        with self.assertRaises(ValueError):
            self.read("read_recommendation_serves", model_version="")
        with self.assertRaises(ValueError):
            self.read("read_llm_usage", provider=True)
        self.search.assert_not_called()

    def test_invalid_constructor_arguments(self) -> None:
        for page_size in (0, 1001, True, 1.5):
            with self.subTest(page_size=page_size), self.assertRaises(ValueError):
                DatabaseReader(page_size=page_size)
        with self.assertRaises(TypeError):
            DatabaseReader(event_search="not-callable")
        with self.assertRaises(TypeError):
            DatabaseReader(logger="not-logger")

    def test_default_search_reuses_public_database_method(self) -> None:
        with patch("src.database.interactions.stream_events.search", return_value=SearchPage([], None)) as search:
            reader = DatabaseReader(logger=self.logger)
            self.assertEqual(reader.read_events(start=self.start, end=self.end), [])
        search.assert_called_once()

    def test_snapshot_windows_are_nonoverlapping(self) -> None:
        snapshots = read_event_snapshots(self.reader, **self.windows(), event_type="rating")
        self.assertEqual(snapshots, {"baseline": [], "recent": []})
        self.assertEqual(self.search.call_count, 2)
        self.search.reset_mock()
        for recent_start in (self.start, self.end - timedelta(seconds=1)):
            with self.subTest(recent_start=recent_start), self.assertRaises(ValueError):
                read_event_snapshots(self.reader, **{**self.windows(), "recent_start": recent_start})
        self.search.assert_not_called()

    def test_drift_results_filtering_mapping_and_logging(self) -> None:
        def search(**kwargs):
            rating = 1 if kwargs["start"] == self.start else 10
            event_type = "watch" if kwargs["start"] == self.start else "account_created"
            rows = [{"rating": rating}] * 100 if kwargs["event_type"] == "rating" else [{"event_type": event_type}] * 100
            return SearchPage(rows, None)
        self.search.side_effect = search
        with self.assertLogs(self.logger, level="INFO") as captured:
            result = detect_database_drift(self.reader, **self.windows())
        for metric in ("rating", "event_type"):
            self.assertEqual(result[metric]["status"], "drift_detected")
            self.assertEqual(result[metric]["drift_score"], 1.0)
            self.assertEqual(result[metric]["baseline_size"], 100)
        self.assertEqual([call.kwargs["event_type"] for call in self.search.call_args_list],
                         ["rating", "rating", None, None])
        payloads = [json.loads(record.getMessage()) for record in captured.records]
        drift_logs = [row for row in payloads if row["issue_type"] == "drift_result"]
        self.assertEqual(len(drift_logs), 2)
        self.assertEqual(drift_logs[0]["drift_score"], 1.0)
        processed = [row for row in payloads if row["issue_type"] == "drift_processing"]
        self.assertEqual([row["records_processed"] for row in processed], [200, 200])

    def test_empty_drift_snapshots_are_insufficient(self) -> None:
        with self.assertLogs(self.logger, level="INFO"):
            results = detect_database_drift(self.reader, **self.windows())
        self.assertTrue(all(result["status"] == "insufficient_data" for result in results.values()))

    def test_malformed_measurements_are_reported_by_drift_not_dropped(self) -> None:
        self.search.return_value = SearchPage([{}] * 100, None)
        with self.assertLogs(self.logger, level="ERROR"):
            results = detect_database_drift(self.reader, **self.windows())
        for result in results.values():
            self.assertEqual(result["status"], "invalid_data")
            self.assertEqual(result["recent_size"], 100)
            self.assertEqual(len(result["errors"]), 200)

    def test_invalid_drift_configuration_does_not_read(self) -> None:
        with self.assertRaises(ValueError):
            detect_database_drift(self.reader, **self.windows(), threshold=float("nan"))
        self.search.assert_not_called()

    def test_read_logs_have_counts_completion_and_no_sensitive_values(self) -> None:
        self.search.return_value = SearchPage([self.event], None)
        handlers = list(self.logger.handlers)
        with self.assertLogs(self.logger, level="INFO") as captured:
            self.read(user_id="private-user", movie_id="private-movie")
        self.assertEqual(self.logger.handlers, handlers)
        payloads = [json.loads(record.getMessage()) for record in captured.records]
        self.assertEqual([row["operation"] for row in payloads],
                         ["read_started", "page_processed", "read_completed"])
        self.assertEqual(payloads[-1]["records_fetched"], 1)
        self.assertEqual(payloads[-1]["records_processed"], 1)
        self.assertTrue(payloads[-1]["pagination_complete"])
        self.assertNotIn("private-user", "".join(captured.output))
        self.assertNotIn("private-movie", "".join(captured.output))

    def test_programming_errors_are_not_swallowed(self) -> None:
        self.search.side_effect = TypeError("bad injected signature")
        with self.assertRaises(TypeError):
            self.read()


if __name__ == "__main__":
    unittest.main()
