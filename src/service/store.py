"""Service adapter over the PostgreSQL interaction layer."""

import json
from pathlib import Path

from pydantic import ValidationError

from src.database.database import database_is_ready
from src.database.interactions import (
    cold_start_jobs,
    cold_start_profiles,
    course_requests,
    users,
)
from src.data_quality.ingestion import persist_stream_event
from src.data_quality.quality_logging import LogContext, log_validation_failure


class ProfileStore:
    def healthy(self):
        if not database_is_ready():
            raise RuntimeError("PostgreSQL is unavailable")
        return True

    def get_profile(self, user_id):
        profile = cold_start_profiles.get(user_id)
        if profile is None:
            return None
        return {"positive": profile.positive, "negative": profile.negative}

    def enqueue(self, user_id):
        users.create_missing_many([{"user_id": user_id}])
        return cold_start_jobs.enqueue(user_id=user_id)

    def claim_next(self, lease_seconds=60):
        job = cold_start_jobs.claim(lease_seconds=lease_seconds)
        if job is None:
            return None
        return {"user_id": job.user_id, "attempts": job.attempts}

    def save_profile(self, user_id, positive, negative, llm_request_id=None):
        users.create_missing_many([{"user_id": user_id}])
        return cold_start_profiles.complete_generation(
            user_id=user_id,
            positive=positive,
            negative=negative,
            llm_request_id=llm_request_id,
        )

    def retry_later(self, user_id, attempts, error="profile generation failed"):
        return cold_start_jobs.retry(user_id=user_id, error=error)

    def cancel_job(self, user_id):
        return cold_start_jobs.complete(user_id)

    def record_account_created(self, user_id, event_time, topic, partition, offset):
        from uuid import NAMESPACE_URL, uuid5

        event_id = uuid5(NAMESPACE_URL, f"kafka:{topic}:{partition}:{offset}")
        context = LogContext(event_id=event_id)
        try:
            users.create_missing_many([{"user_id": user_id}])
        except ValidationError as error:
            errors = error.errors(
                include_url=False, include_context=False, include_input=False
            )
            log_validation_failure("kafka", errors, context=context)
            return None, errors
        return persist_stream_event({
            "event_id": event_id,
            "user_id": user_id,
            "event_type": "account_created",
            "event_time": event_time,
        }, context=context)

    def record_course_request(self, topic, partition_id, offset_id, line):
        return course_requests.create(
            topic=topic,
            partition_id=partition_id,
            offset_id=offset_id,
            raw_line=line,
        )

    def seed_profiles(self, path):
        seed_path = Path(path)
        if not seed_path.is_file():
            return
        profiles = json.loads(seed_path.read_text(encoding="utf-8")).get("profiles", {})
        valid_profiles = {
            str(user_id): profile
            for user_id, profile in profiles.items()
            if isinstance(profile.get("positive"), str)
            and isinstance(profile.get("negative"), str)
        }
        users.create_missing_many(
            [{"user_id": user_id} for user_id in valid_profiles]
        )
        for user_id, profile in valid_profiles.items():
            if cold_start_profiles.get(user_id) is None:
                cold_start_profiles.replace(
                    user_id=user_id,
                    positive=profile["positive"],
                    negative=profile["negative"],
                )
