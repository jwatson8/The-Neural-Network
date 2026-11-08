import time
import unittest
from datetime import UTC, datetime, timezone
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from deployment.traffic_report import summarize
from src.database.interactions import (
    cold_start_profiles,
    course_requests,
    llm_usage,
    recommendation_serves,
    stream_events,
    users,
)
from src.service.cold_start import recommend_cold_start
from src.service.course_stream import handle_line
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
