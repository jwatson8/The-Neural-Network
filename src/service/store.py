"""Service adapter over the PostgreSQL interaction layer."""

import json
from pathlib import Path

from pydantic import ValidationError

from src.database.database import database_is_ready
from src.database.interactions import (
    cold_start_jobs,
    cold_start_profiles,
    course_requests,
    movies,
    stream_events,
    users,
)
from src.database.schemas import CourseRequestCreate, StreamEventCreate
from src.data_quality.ingestion import persist_stream_event, validate_stream_events
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
        return cold_start_jobs.enqueue(user_id=user_id)

    def claim_next(self, lease_seconds=360):
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

    def record_movie_event(self, event, topic, partition, offset):
        from uuid import NAMESPACE_URL, uuid5

        event_id = uuid5(NAMESPACE_URL, f"kafka:{topic}:{partition}:{offset}")
        context = LogContext(event_id=event_id)
        try:
            validated_event = StreamEventCreate.model_validate(
                {**event, "event_id": event_id}
            )
        except ValidationError as error:
            errors = error.errors(
                include_url=False, include_context=False, include_input=False
            )
            log_validation_failure("kafka", errors, context=context)
            return None, errors
        movie_id = validated_event.movie_id
        if movies.get(movie_id) is None:
            errors = [{
                "loc": ("movie_id",),
                "type": "value_error",
                "msg": "Unknown movie identifier",
            }]
            log_validation_failure("kafka", errors, context=context)
            return None, errors
        users.create_missing_many([{"user_id": validated_event.user_id}])
        return persist_stream_event(validated_event.model_dump(), context=context)

    def record_course_request(self, topic, partition_id, offset_id, line):
        return course_requests.create(
            topic=topic,
            partition_id=partition_id,
            offset_id=offset_id,
            raw_line=line,
        )

    def record_batch(self, stream_records, request_records):
        """Persist one decoded Kafka poll using bounded bulk operations."""
        counts = {"account": 0, "watch": 0, "rating": 0, "recommendation": 0}
        errors = []
        rejected_count = 0
        valid_events, validation_failures = validate_stream_events(
            record["event"] for record in stream_records
        )
        for failure in validation_failures:
            errors.extend(failure)
        rejected_count += len(validation_failures)
        kind_by_event_type = {
            "account_created": "account", "watch": "watch", "rating": "rating",
        }
        validated_stream = [
            (kind_by_event_type[event.event_type], event) for event in valid_events
        ]

        account_user_ids = {
            event.user_id
            for kind, event in validated_stream
            if kind == "account"
        }
        movie_ids = {
            event.movie_id
            for kind, event in validated_stream
            if kind in ("watch", "rating") and event.movie_id is not None
        }
        known_movies = movies.get_existing_ids(movie_ids)
        accepted_events = []
        for kind, event in validated_stream:
            if kind in ("watch", "rating") and event.movie_id not in known_movies:
                errors.append({
                    "loc": ("movie_id",),
                    "type": "value_error",
                    "msg": "Unknown movie identifier",
                })
                rejected_count += 1
                continue
            accepted_events.append(event)
            counts[kind] += 1

        observed_user_ids = {event.user_id for event in accepted_events}
        if observed_user_ids:
            users.create_missing_many(
                [{"user_id": user_id} for user_id in sorted(observed_user_ids)]
            )

        inserted = stream_events.create_many(accepted_events)

        valid_requests = []
        for record in request_records:
            try:
                valid_requests.append(CourseRequestCreate.model_validate(record))
            except ValidationError as error:
                errors.extend(error.errors(
                    include_url=False, include_context=False, include_input=False
                ))
                rejected_count += 1
        course_requests.create_many(valid_requests)
        counts["recommendation"] = len(valid_requests)

        if account_user_ids:
            existing_profiles = cold_start_profiles.get_existing_user_ids(
                account_user_ids
            )
            cold_start_jobs.enqueue_many(
                {"user_id": user_id}
                for user_id in sorted(account_user_ids - existing_profiles)
            )

        return {
            "counts": counts,
            "inserted": inserted,
            "duplicates": max(0, len(accepted_events) - inserted),
            "errors": errors,
            "rejected_count": rejected_count,
        }

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
