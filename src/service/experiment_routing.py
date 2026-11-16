"""Small serving boundary for experiment decisions; no model hosting implementation."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from src.data_quality.boundary import validate_recommendation_response
from src.experimentation.decisions import ExperimentDecision

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServingRequest:
    user_id: str
    exclude_movie_ids: frozenset[str]
    k: int = 20
    has_history: bool = False


@dataclass(frozen=True)
class ServingResult:
    """Actual inference metadata, supplied by a trusted serving adapter.

    fulfilled attests that the requested policy/version actually ran. The router
    also checks metadata and output; it never substitutes requested metadata for
    actual values. fallback_path describes any deviation from requested serving.
    policy_version optionally pins a recommendation policy/configuration separately
    from the actual model_version. underlying_route retains its candidate method.
    """

    recommendations: tuple[str, ...]
    model_version: str
    model_route: str
    policy: str
    fallback_path: str | None = None
    fulfilled: bool = False
    policy_version: str | None = None
    underlying_route: str | None = None


class TreatmentServer(Protocol):
    def __call__(self, request: ServingRequest, decision: ExperimentDecision) -> ServingResult:
        """Serve the requested policy/version and report the actual execution."""
        ...


def _matches(result: ServingResult, decision: ExperimentDecision) -> bool:
    return (
        result.fulfilled is True
        and result.policy == decision.policy
        and (result.policy_version if result.policy_version is not None else result.model_version) == decision.version
        and result.model_version.strip().lower() not in ("", "unknown")
    )


def _valid_treatment(result: object, request: ServingRequest, decision: ExperimentDecision) -> bool:
    if not isinstance(result, ServingResult):
        return False
    if not all(isinstance(value, str) and value.strip() and len(value) <= maximum
               for value, maximum in ((result.model_version, 255), (result.model_route, 100), (result.policy, 255))):
        return False
    if result.policy_version is not None and (
        not isinstance(result.policy_version, str) or not result.policy_version.strip()
        or len(result.policy_version) > 255
    ):
        return False
    if result.underlying_route is not None and (
        not isinstance(result.underlying_route, str) or not result.underlying_route.strip()
        or len(result.underlying_route) > 100
    ):
        return False
    if not isinstance(result.recommendations, tuple) or not all(isinstance(item, str) for item in result.recommendations):
        return False
    if any("," in item for item in result.recommendations):
        return False
    _, errors = validate_recommendation_response(",".join(result.recommendations))
    return (
        not errors and len(result.recommendations) <= request.k
        and not request.exclude_movie_ids.intersection(result.recommendations)
        and result.fallback_path is None and _matches(result, decision)
    )


@dataclass(frozen=True)
class RecommendationRouter:
    treatment: TreatmentServer | None = None

    def serve(
        self, request: ServingRequest, decision: ExperimentDecision | None,
        control: Callable[[], ServingResult],
    ) -> ServingResult:
        """Use legacy control, or a fulfilled Treatment; otherwise fall back explicitly.

        Control inference errors propagate unchanged. Treatment errors or invalid
        results use normal serving and never constitute an experiment exposure.
        The callback is invoked at most once, and no audit record is written here.
        """
        if decision is None or not decision.assigned:
            return replace(control(), fulfilled=False)
        if decision.variant == "control":
            result = control()
            fulfilled = _matches(result, decision)
            fallback = result.fallback_path
            if not fulfilled:
                fallback = ":".join(filter(None, ("unfulfilled_control", fallback)))
            return replace(result, fulfilled=fulfilled, fallback_path=fallback)
        reason = "unsupported_treatment"
        if decision.variant == "treatment" and self.treatment is not None:
            try:
                result = self.treatment(request, decision)
                if _valid_treatment(result, request, decision):
                    return result
                reason = "unfulfilled_treatment"
            except Exception as error:
                reason = "treatment_error"
                LOGGER.warning("experiment_treatment_failed error_type=%s", type(error).__name__)
        result = control()
        fallback = ":".join(filter(None, (reason, result.fallback_path)))
        return replace(result, fulfilled=False, fallback_path=fallback)
