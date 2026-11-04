from decimal import Decimal
from math import isfinite
from collections.abc import Mapping
from typing import Any, TypeVar, TypedDict

from pydantic import ValidationError

from src.database.schemas import (
    MovieCreate, RecommendationItem, StreamEventCreate, UserCreate,
)


ResponseModel = TypeVar("ResponseModel", UserCreate, MovieCreate)


class RecommendationRequest(TypedDict):
    user_id: str


def validate_recommendation_request(
    raw_request: object,
    query_params: Mapping[str, object] | None = None,
) -> tuple[RecommendationRequest | None, list[dict]]:
    """Validate decoded Flask route values and optional query parameters.

    Only user_id is supported; k is internal and fixed at 20 by the route.
    Keep identity spelling intact rather than silently changing lookup keys.
    """
    if not isinstance(raw_request, dict):
        return None, _response_error((), "Request must be an object of route values")
    errors = []
    user_id = raw_request.get("user_id")
    if not isinstance(user_id, str) or not user_id.strip() or "/" in user_id:
        errors.extend(_response_error(("user_id",), "user_id must be a nonblank string path segment"))
    for key in raw_request.keys() - {"user_id"}:
        errors.extend(_response_error((key,), "Unsupported request parameter"))
    if query_params is not None:
        if not isinstance(query_params, Mapping):
            errors.extend(_response_error(("query_params",), "Query parameters must be a mapping"))
        else:
            for key in query_params:
                errors.extend(_response_error(("query_params", key), "Unsupported query parameter"))
    if errors:
        return None, errors
    return RecommendationRequest(user_id=user_id), []


def validate_recommendation_item(
    raw_item: object,
) -> tuple[RecommendationItem | None, list[dict]]:
    """Validate a canonical internal item, not a public HTTP response.

    Optional model_score uses Nathan's schema with an additional finite-number
    check. This schema belongs to persistence; scores have no rating-like bound.
    """
    if not isinstance(raw_item, dict):
        return None, _response_error((), "Recommendation item must be an object")
    score = raw_item.get("model_score")
    if isinstance(score, bool):
        return None, _response_error(("model_score",), "Model score cannot be a boolean")
    try:
        item = RecommendationItem.model_validate(raw_item)
    except ValidationError as exc:
        return None, exc.errors(
            include_url=False, include_context=False, include_input=False
        )
    errors = []
    if not item.movie_id.strip() or any(char in item.movie_id for char in ",\r\n"):
        errors.extend(_response_error(("movie_id",), "Movie ID must be nonblank and contain no comma or line break"))
    if item.model_score is not None and not isfinite(item.model_score):
        errors.extend(_response_error(("model_score",), "Model score must be finite"))
    return (None, errors) if errors else (item, [])


def validate_recommendation_response(
    raw_response: object,
) -> tuple[list[RecommendationItem] | None, list[dict]]:
    """Parse a successful public response: 1–20 distinct comma-separated IDs.

    Pass decoded text, not a Response object or JSON. Ordering and ID spelling
    are preserved. HTTP status/content type and catalog membership are caller
    checks. Public responses do not carry scores or telemetry fields.
    """
    if not isinstance(raw_response, str):
        return None, _response_error((), "Recommendation response must be plain text")
    movie_ids = raw_response.split(",")
    if not raw_response or len(movie_ids) > 20:
        return None, _response_error(("recommendations",), "Response must contain 1 to 20 movie IDs")
    items = []
    errors = []
    seen = set()
    for index, movie_id in enumerate(movie_ids):
        item, item_errors = validate_recommendation_item({"movie_id": movie_id})
        errors.extend(
            {**error, "loc": ("recommendations", index, *error["loc"])}
            for error in item_errors
        )
        if movie_id in seen:
            errors.extend(_response_error(("recommendations", index, "movie_id"), "Duplicate movie ID"))
        seen.add(movie_id)
        if item is not None:
            items.append(item)
    return (None, errors) if errors else (items, [])


def _response_error(location: tuple, message: str) -> list[dict]:
    return [{"loc": location, "type": "value_error", "msg": message}]


def _validate_response(
    raw_response: object,
    schema: type[ResponseModel],
    identifier: str,
    numeric_fields: tuple[str, ...],
) -> tuple[ResponseModel | None, list[dict]]:
    if not isinstance(raw_response, dict):
        return None, _response_error((), "Response must be a JSON object")
    values = dict(raw_response)
    if isinstance(values.get(identifier), str):
        values[identifier] = values[identifier].strip()

    # Pydantic accepts booleans as numbers and permits non-finite floats by
    # default. Neither represents a usable API measurement or count.
    errors = []
    for field in numeric_fields:
        value = values.get(field)
        if isinstance(value, bool):
            errors.extend(_response_error((field,), "Numeric fields cannot be booleans"))
    if errors:
        return None, errors
    try:
        validated = schema.model_validate(values)
    except ValidationError as exc:
        return None, exc.errors(
            include_url=False, include_context=False, include_input=False
        )
    for field in numeric_fields:
        value = getattr(validated, field)
        if (isinstance(value, float) and not isfinite(value)) or (
            isinstance(value, Decimal) and not value.is_finite()
        ):
            errors.extend(_response_error((field,), "Numeric fields must be finite"))
    return (None, errors) if errors else (validated, [])


def validate_user_response(raw_response: object) -> tuple[UserCreate | None, list[dict]]:
    """Validate a database-shaped user object or the verified ``user`` wrapper.

    The wrapper and default preference names come from
    service/profile_worker.py:fetch_descriptions. The live API's identifier,
    demographic fields and configured preference aliases are not documented;
    callers must map those to UserCreate names before calling this function.
    Preference-only responses need a verified request user_id supplied by the
    caller. This function does not read USER_LIKES_FIELD/USER_DISLIKES_FIELD or
    infer identities; see data_quality/README.md for the integration mapping.
    Unknown fields, including wrapper metadata, are rejected rather than lost.
    """
    if isinstance(raw_response, dict) and "user" in raw_response:
        extras = raw_response.keys() - {"user"}
        if extras:
            return None, [
                {"loc": (key,), "type": "extra_forbidden", "msg": "Extra inputs are not permitted"}
                for key in sorted(extras, key=str)
            ]
        user, errors = _validate_response(
            raw_response["user"], UserCreate, "user_id", ("age",)
        )
        return user, [{**error, "loc": ("user", *error["loc"])} for error in errors]
    return _validate_response(raw_response, UserCreate, "user_id", ("age",))


def validate_movie_response(raw_response: object) -> tuple[MovieCreate | None, list[dict]]:
    """Validate only the verified MovieCreate persistence representation.

    No external Movie API response contract is present in the repository.
    This does not infer aliases, wrappers, genre lists or external ID types.
    Numeric coercions and bounds follow Nathan's schema; optional nulls remain
    null. Callers must explicitly map any different live API representation.
    """
    movie, errors = _validate_response(
        raw_response,
        MovieCreate,
        "movie_id",
        ("runtime", "popularity", "vote_average", "vote_count", "license_cost"),
    )
    if movie is not None and not movie.title.strip():
        return None, _response_error(("title",), "Movie title cannot be blank")
    return movie, errors


def validate_stream_event(
    raw_event: dict[str, Any],
) -> tuple[StreamEventCreate | None, list[dict]]:
    """
    Validate a Kafka stream event.

    Returns:
        (validated_event, errors)
    """
    try:
        event = StreamEventCreate.model_validate(raw_event)
        return event, []

    except ValidationError as exc:
        return None, exc.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )
