import os
import time
import unittest
from datetime import UTC, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from deployment.traffic_report import iter_course_lines, summarize
from src.database.interactions import (
    cold_start_profiles,
    course_requests,
    data_quality_logs,
    licensing_costs,
    llm_usage,
    movies,
    recommendation_serves,
    stream_events,
    user_movie_history,
    users,
)
from src.service.cold_start import recommend_cold_start
from src.service.course_stream import handle_line, main
from src.service.profile_worker import PreferenceProfile, process_one
from src.service.store import ProfileStore
from src.tests.test_service import make_model


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.engine_patch = patch(
            "src.database.database.get_engine", return_value=self.engine
        )
        self.engine_patch.start()
        self.addCleanup(self.engine_patch.stop)
        self.addCleanup(self.engine.dispose)
        self.store = ProfileStore()

    def test_account_event_queues_once_and_profile_survives_reopen(self):
        line = "2026-10-11T12:00:00Z,501,GET /create_account"
        for _ in range(2):
            self.assertEqual(handle_line(self.store, "topic", 0, 1, line), "account")
        account_events = stream_events.search(
            user_id="501", event_type="account_created", limit=10
        ).records
        self.assertEqual(len(account_events), 1)
        with patch(
            "src.service.profile_worker.fetch_descriptions",
            return_value=("space", "horror"),
        ), patch(
            "src.service.profile_worker.generate_profile",
            return_value=(PreferenceProfile(positive="space", negative="horror"), None),
        ) as generate:
            self.assertTrue(process_one(self.store, object(), "test-model"))
            self.assertFalse(process_one(self.store, object(), "test-model"))
            generate.assert_called_once()
        reopened = ProfileStore()
        self.assertEqual(reopened.get_profile("501")["positive"], "space")
        profile = cold_start_profiles.get("501")
        usage = llm_usage.get(profile.llm_request_id)
        self.assertEqual(usage.status, "succeeded")
        handle_line(reopened, "topic", 0, 1, line)
        self.assertIsNone(reopened.claim_next())

    def test_watch_and_rating_rows_are_persisted_and_derive_history(self):
        movies.create(movie_id="live-movie", title="Live movie")
        watch = handle_line(
            self.store, "topic", 0, 10,
            "2026-10-11T12:00:00Z,501,watch,live-movie,",
        )
        replay = handle_line(
            self.store, "topic", 0, 10,
            "2026-10-11T12:00:00Z,501,watch,live-movie,",
        )
        rating = handle_line(
            self.store, "topic", 0, 11,
            "2026-10-11T12:30:00Z,501,rating,live-movie,8",
        )

        self.assertEqual((watch, replay, rating), ("watch", "watch", "rating"))
        events = stream_events.search(user_id="501", limit=10).records
        self.assertEqual({event.event_type for event in events}, {"watch", "rating"})
        history = user_movie_history.get("501", "live-movie")
        self.assertEqual(history.latest_rating, 8)
        self.assertIsNotNone(history.last_recorded_watch_at)
        self.assertEqual(len(licensing_costs.search(user_id="501").records), 1)

    def test_consumer_counts_logs_and_commits_watch_and_rating_events(self):
        records = [
            SimpleNamespace(
                topic="topic",
                partition=0,
                offset=10,
                value=b"2026-10-11T12:00:00Z,501,watch,live-movie,",
            ),
            SimpleNamespace(
                topic="topic",
                partition=0,
                offset=11,
                value=b"2026-10-11T12:30:00Z,501,rating,live-movie,8",
            ),
        ]
        consumer = SimpleNamespace(
            poll=Mock(side_effect=[{("topic", 0): records}, RuntimeError("stop")]),
            commit=Mock(),
            close=Mock(),
        )
        with patch.dict(
            os.environ,
            {
                "KAFKA_BOOTSTRAP_SERVERS": "localhost:9092",
                "KAFKA_TOPIC": "topic",
                "KAFKA_GROUP_ID": "test-group",
            },
        ), patch("src.service.course_stream.KafkaConsumer", return_value=consumer), patch(
            "src.service.course_stream.ProfileStore"
        ) as store_factory, patch("src.service.course_stream.logger.info") as log_info:
            store_factory.return_value.record_movie_event.return_value = (True, [])
            with self.assertRaisesRegex(RuntimeError, "stop"):
                main()

        consumer.commit.assert_called_once_with()
        consumer.close.assert_called_once_with()
        activity_logs = [call.args for call in log_info.call_args_list]
        event_log = next(
            args for args in activity_logs if args[0].startswith("course_events")
        )
        self.assertIn("watches=%d ratings=%d", event_log[0])
        self.assertEqual(event_log[3:5], (1, 1))

    def test_unknown_movie_stream_event_is_logged_and_not_persisted(self):
        result = handle_line(
            self.store, "topic", 0, 12,
            "2026-10-11T12:00:00Z,501,watch,missing-movie,",
        )

        self.assertEqual(result, "invalid")
        self.assertEqual(stream_events.list_records(), [])
        logs = data_quality_logs.search(source="kafka", limit=10).records
        self.assertEqual(len(logs), 1)
        self.assertNotIn("missing-movie", str(logs[0].error_details))

    def test_invalid_account_event_is_logged_and_not_queued(self):
        result = handle_line(
            self.store, "topic", 0, 3, "2026-10-11T12:00:00Z,,GET /create_account"
        )

        self.assertEqual(result, "invalid")
        self.assertIsNone(self.store.claim_next())
        records = data_quality_logs.list_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].source, "kafka")
        self.assertNotIn("2026-10-11", str(records[0].error_details))

    def test_invalid_event_time_is_logged_without_raw_line(self):
        result = handle_line(
            self.store, "topic", 0, 4, "not-a-date,501,GET /create_account"
        )

        self.assertEqual(result, "invalid")
        records = data_quality_logs.list_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].source, "kafka")
        self.assertNotIn("not-a-date", str(records[0].error_details))

    def test_naive_account_timestamp_uses_configured_course_timezone(self):
        with patch.dict(os.environ, {"COURSE_LOG_TIMEZONE": "America/New_York"}):
            result = handle_line(
                self.store, "topic", 0, 7,
                "2026-10-11T12:00:00,501,GET /create_account",
            )

        self.assertEqual(result, "account")
        event = stream_events.search(
            user_id="501", event_type="account_created", limit=1
        ).records[0]
        self.assertEqual(
            event.event_time.replace(tzinfo=UTC),
            datetime(2026, 10, 11, 16, 0, tzinfo=UTC),
        )

    def test_course_report_lines_are_yielded_pagewise(self):
        lines = ["first", "second", "third"]

        def read_page(*, offset, limit):
            return [
                SimpleNamespace(raw_line=line)
                for line in lines[offset : offset + limit]
            ]

        with patch(
            "deployment.traffic_report.course_requests.list_records",
            side_effect=read_page,
        ) as read_records:
            self.assertEqual(list(iter_course_lines(page_size=2)), lines)

        self.assertEqual(
            [call.kwargs for call in read_records.call_args_list],
            [{"offset": 0, "limit": 2}, {"offset": 2, "limit": 2}],
        )

    def test_event_database_failure_propagates_for_consumer_retry(self):
        line = "2026-10-11T12:00:00Z,501,GET /create_account"
        with patch(
            "src.database.interactions.stream_events.create_many",
            side_effect=SQLAlchemyError("database unavailable"),
        ):
            with self.assertRaises(SQLAlchemyError):
                handle_line(self.store, "topic", 0, 5, line)

    def test_lookup_failure_schedules_retry_without_profile(self):
        self.store.enqueue("502")
        with patch(
            "src.service.profile_worker.fetch_descriptions", side_effect=TimeoutError
        ):
            self.assertTrue(process_one(self.store, object(), "test-model"))
        self.assertIsNone(self.store.get_profile("502"))
        self.assertIsNone(self.store.claim_next())

    def test_llm_failure_is_audited_and_job_is_retried(self):
        self.store.enqueue("504")
        with patch(
            "src.service.profile_worker.fetch_descriptions",
            return_value=("space", "horror"),
        ), patch(
            "src.service.profile_worker.generate_profile", side_effect=TimeoutError
        ):
            self.assertTrue(process_one(self.store, object(), "test-model"))
        records = llm_usage.search(user_id="504", status="failed", limit=10).records
        self.assertEqual(len(records), 1)
        self.assertIsNone(self.store.get_profile("504"))

    def test_empty_preferences_do_not_call_llm_or_count_as_personalized(self):
        self.store.enqueue("503")
        with patch(
            "src.service.profile_worker.fetch_descriptions", return_value=("", "")
        ), patch("src.service.profile_worker.generate_profile") as generate:
            process_one(self.store, object(), "test-model")
            generate.assert_not_called()
        _, route = recommend_cold_start(make_model(), self.store, "503")
        self.assertEqual(route, "popularity_fallback")

    def test_report_denominator_includes_failure_and_deduplicates_offsets(self):
        now = time.time()
        served_at = datetime.fromtimestamp(now, UTC)
        timestamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
        users.create(user_id="501")
        recommendation_serves.create(
            user_id="501",
            served_at=served_at,
            request_received_at=served_at,
            serve_duration_ms=4,
            model_route="llm_content",
            model_version="test",
            recommendations=[{"movie_id": "movie-a"}, {"movie_id": "movie-b"}],
        )
        lines = [
            f"{timestamp},501,recommendation request team-vm:8082, status 200, result: movie-a,movie-b, 4ms",
            f"{timestamp},502,recommendation request team-vm:8082, status 500, result: error, 600ms",
            f"{timestamp},503,recommendation request team-vm:8082, status 200, result: movie-c, 4ms",
            f"{timestamp},501,recommendation request other-vm:8082, status 200, result: movie-a,movie-b, 4ms",
        ]
        for _ in range(2):
            for offset, line in enumerate(lines):
                handle_line(self.store, "topic", 0, offset, line)
        captured = course_requests.list_records(limit=1000)
        audits = recommendation_serves.search(
            start=datetime.fromtimestamp(now - 10, UTC),
            end=datetime.fromtimestamp(now + 10, UTC),
        ).records
        report = summarize(
            [record.raw_line for record in captured],
            audits,
            "team-vm:8082",
            now - 10,
            now + 10,
            "UTC",
        )
        self.assertEqual(report["all_requests"], 3)
        self.assertEqual(report["successful_status_200"], 2)
        self.assertEqual(report["matched_personalized_successes"], 1)
        self.assertAlmostEqual(
            report["matched_personalized_fraction_of_ALL_requests"], 1 / 3
        )


if __name__ == "__main__":
    unittest.main()
