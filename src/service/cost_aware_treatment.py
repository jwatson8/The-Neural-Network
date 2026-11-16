"""Treatment adapter over a scored candidate source, not a model host."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from src.database.interactions import movies
from src.experimentation.cost_reranking import (
    POLICY, CostAwareConfig, RerankingUnavailable, rerank, treatment_version,
)
from src.experimentation.decisions import ExperimentDecision
from src.service.experiment_routing import ServingRequest, ServingResult
from src.service.recommendation_candidates import CandidateSource


@dataclass(frozen=True)
class CostAwareTreatmentServer:
    candidate_source: CandidateSource
    model_version: str
    config: CostAwareConfig = CostAwareConfig()
    catalog_lookup: Callable[[tuple[str, ...]], Mapping[str, object]] | None = None

    @property
    def policy_version(self) -> str:
        return treatment_version(self.model_version, self.config)

    def __call__(self, request: ServingRequest, decision: ExperimentDecision) -> ServingResult:
        if (not decision.assigned or decision.variant != 'treatment' or decision.policy != POLICY
                or decision.version != self.policy_version or not isinstance(self.model_version, str)
                or self.model_version.strip().lower() in ('', 'unknown')):
            raise RerankingUnavailable('policy/configuration/model pin does not match the assignment')
        pool = self.candidate_source(request, self.config.candidate_limit(request.k))
        if pool.route not in ('collaborative_svd', 'llm_content', 'popularity_fallback'):
            raise RerankingUnavailable('unsupported underlying candidate route')
        if len(pool.candidates) > self.config.candidate_limit(request.k):
            raise RerankingUnavailable('candidate source exceeded its limit')
        lookup = self.catalog_lookup if self.catalog_lookup is not None else movies.get_license_costs
        prices = lookup(tuple(candidate.movie_id for candidate in pool.candidates))
        result = rerank(pool.candidates, prices, k=request.k, config=self.config,
                        exclude_movie_ids=request.exclude_movie_ids)
        return ServingResult(
            recommendations=tuple(item.movie_id for item in result.selected),
            model_version=self.model_version, model_route=f'cost_aware:{pool.route}',
            policy=POLICY, fulfilled=True, policy_version=self.policy_version,
            underlying_route=pool.route,
        )
