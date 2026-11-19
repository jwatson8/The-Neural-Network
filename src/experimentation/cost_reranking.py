"""Deterministic cost/relevance ranking using catalog reference prices only."""

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from numbers import Real

from src.data_quality.boundary import validate_recommendation_item

POLICY = 'cost_aware_rerank'
ALGORITHM = 'cost-aware-rank-v1'


class RerankingUnavailable(ValueError):
    """The full requested Treatment cannot safely be produced."""


@dataclass(frozen=True)
class CostAwareConfig:
    cost_weight: Decimal = Decimal('0.15')
    candidate_multiplier: int = 5
    max_candidates: int = 200

    def __post_init__(self):
        try:
            if isinstance(self.cost_weight, bool):
                raise ValueError('boolean cost weight')
            weight = Decimal(str(self.cost_weight))
        except (InvalidOperation, ValueError, TypeError) as error:
            raise ValueError('cost_weight must be finite and between zero and one') from error
        if not weight.is_finite() or not 0 <= weight <= 1:
            raise ValueError('cost_weight must be finite and between zero and one')
        if type(self.candidate_multiplier) is not int or not 1 <= self.candidate_multiplier <= 10:
            raise ValueError('candidate_multiplier must be an integer from 1 to 10')
        if type(self.max_candidates) is not int or not 20 <= self.max_candidates <= 200:
            raise ValueError('max_candidates must be an integer from 20 to 200')
        object.__setattr__(self, 'cost_weight', weight)

    def candidate_limit(self, k: int) -> int:
        if type(k) is not int or not 1 <= k <= 20:
            raise ValueError('k must be between 1 and 20')
        return min(self.max_candidates, k * self.candidate_multiplier)


def treatment_version(model_version: str, config: CostAwareConfig) -> str:
    """Identify the algorithm/configuration and independently supplied model pin."""
    manifest = dict(algorithm=ALGORITHM, model_version=model_version,
                    weight=format(config.cost_weight.normalize(), 'f'),
                    candidate_multiplier=config.candidate_multiplier, max_candidates=config.max_candidates)
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:16]
    return f'{ALGORITHM}-{digest}'


@dataclass(frozen=True)
class ScoredCandidate:
    movie_id: str
    score: float


@dataclass(frozen=True)
class RankedCandidate:
    movie_id: str
    raw_score: float
    relevance: float
    normalized_cost: float
    adjusted_score: float
    reference_cost: Decimal | None


@dataclass(frozen=True)
class RerankingResult:
    selected: tuple[RankedCandidate, ...]
    candidates: tuple[RankedCandidate, ...]
    unknown_cost_count: int


def reference_price(value: object) -> Decimal | None:
    """Unknown/invalid prices are never interpreted as free catalog licenses."""
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return price if price.is_finite() and price >= 0 else None


def normalized_price(price: Decimal | None, scale: Decimal) -> float:
    """Bounded p/(p+median_positive_price), computed without large-price overflow."""
    if price is None:
        return 1.0
    if price == 0:
        return 0.0
    if price >= scale:
        return float(1 / (1 + scale / price))
    ratio = price / scale
    return float(ratio / (1 + ratio))


def rerank(
    candidates: Iterable[ScoredCandidate], catalog_costs: Mapping[str, object], *,
    k: int = 20, config: CostAwareConfig = CostAwareConfig(),
    exclude_movie_ids: Iterable[str] = (),
) -> RerankingResult:
    """Select K valid distinct candidates with percentile relevance minus cost penalty.

    Input is bounded to 200 entries. Tied raw scores share their average rank.
    Stable input order breaks final ties. The source must preserve model catalog
    order for score ties. Missing mapping keys are not valid catalog movies.
    """
    config.candidate_limit(k)
    excluded, clean = set(exclude_movie_ids), []
    for index, candidate in enumerate(candidates):
        if index >= 200:
            raise ValueError('at most 200 candidates may be reranked')
        item, errors = validate_recommendation_item({'movie_id': candidate.movie_id})
        if errors or candidate.movie_id in excluded or candidate.movie_id not in catalog_costs:
            continue
        if isinstance(candidate.score, bool) or not isinstance(candidate.score, Real):
            continue
        try:
            score = float(candidate.score)
        except (OverflowError, ValueError):
            continue
        if not math.isfinite(score):
            continue
        clean.append((item.movie_id, score, index))
    clean.sort(key=lambda row: (-row[1], row[2]))
    unique, used = [], set()
    for row in clean:
        if row[0] not in used:
            unique.append(row)
            used.add(row[0])
    if len(unique) < k:
        raise RerankingUnavailable('insufficient valid catalog candidates')
    prices = {movie_id: reference_price(catalog_costs[movie_id]) for movie_id, _, _ in unique}
    known = [price for price in prices.values() if price is not None]
    if not known:
        raise RerankingUnavailable('no known catalog reference prices')
    positive = sorted(price for price in known if price > 0)
    # Lower median avoids averaging huge Decimal values; robust to minority outliers.
    scale = positive[(len(positive) - 1) // 2] if positive else Decimal('1')
    ranked, start, count = [], 0, len(unique)
    while start < count:
        end = start + 1
        while end < count and unique[end][1] == unique[start][1]:
            end += 1
        relevance = 1.0 if count == 1 else 1 - ((start + end - 1) / 2) / (count - 1)
        for movie_id, score, original_index in unique[start:end]:
            cost = normalized_price(prices[movie_id], scale)
            item = RankedCandidate(movie_id, score, relevance, cost,
                                   relevance - float(config.cost_weight) * cost, prices[movie_id])
            ranked.append((item, original_index))
        start = end
    selected = sorted(ranked, key=lambda row: (-row[0].adjusted_score, -row[0].relevance, row[1]))[:k]
    return RerankingResult(tuple(row[0] for row in selected), tuple(row[0] for row in ranked),
                           sum(price is None for price in prices.values()))
