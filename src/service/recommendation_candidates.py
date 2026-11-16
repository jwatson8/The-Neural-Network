"""Bounded, scored candidates from the same inference paths as Control."""

import logging
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from src.data_quality.boundary import validate_recommendation_item
from src.experimentation.cost_reranking import ScoredCandidate
from src.model.routing import uses_svd
from src.service.cold_start import cold_start_scores
from src.service.experiment_routing import ServingRequest
from src.service.model_adapter import recommendation_scores

LOGGER = logging.getLogger(__name__)
MAX_CANDIDATES = 200


@dataclass(frozen=True)
class CandidatePool:
    candidates: tuple[ScoredCandidate, ...]
    route: str


class CandidateSource(Protocol):
    def __call__(self, request: ServingRequest, limit: int) -> CandidatePool:
        ...


@dataclass(frozen=True)
class LoadedModelCandidates:
    model: dict
    profile_store: object

    def __call__(self, request: ServingRequest, limit: int) -> CandidatePool:
        if not 1 <= limit <= MAX_CANDIDATES:
            raise ValueError('candidate limit must be between 1 and 200')
        if not self.model:
            raise ValueError('model unavailable')
        if request.has_history and uses_svd(self.model, request.user_id):
            scores, route = recommendation_scores(self.model, request.user_id)
        else:
            result = cold_start_scores(self.model, self.profile_store, request.user_id, allow_svd=False)
            if result is None:
                scores, route = recommendation_scores(self.model, request.user_id, allow_svd=False)
                try:
                    self.profile_store.enqueue(request.user_id)
                except Exception as error:
                    LOGGER.warning('candidate_profile_enqueue_failed error_type=%s', type(error).__name__)
            else:
                scores, route = result
        scores = np.asarray(scores, dtype=float).copy()
        if scores.ndim != 1 or len(scores) != len(self.model['movies']):
            raise ValueError('model scores do not match the catalog')
        seen = self.model['seen'].get(request.user_id, ())
        if seen:
            scores[list(seen)] = -np.inf
        picks, used = [], set()
        for index in np.argsort(-scores, kind='stable'):
            movie_id = str(self.model['movies'][int(index)]['movie_id'])
            if (not np.isfinite(scores[index]) or movie_id in used
                    or movie_id in request.exclude_movie_ids):
                continue
            item, errors = validate_recommendation_item({'movie_id': movie_id})
            if errors:
                continue
            picks.append(ScoredCandidate(item.movie_id, float(scores[index])))
            used.add(movie_id)
            if len(picks) == limit:
                break
        return CandidatePool(tuple(picks), route)
