"""Reproducible catalog-price diagnostics; never a production A/B evaluation."""

import json
from collections import Counter
from decimal import Decimal

from src.experimentation.cost_reranking import CostAwareConfig, rerank, treatment_version


def compare_candidates(candidates, catalog_costs, *, route, k=20, config=CostAwareConfig()):
    """Compare relevance-only and cost-aware selection from the same valid pool."""
    candidates = tuple(candidates)
    control = rerank(candidates, catalog_costs, k=k, config=CostAwareConfig(
        0, config.candidate_multiplier, config.max_candidates))
    treatment = rerank(candidates, catalog_costs, k=k, config=config)

    def summary(result):
        known = [item.reference_cost for item in result.selected if item.reference_cost is not None]
        known_mean = float(sum(known) / len(known)) if known else None
        return dict(
            recommendations=[item.movie_id for item in result.selected],
            valid_recommendations=len(result.selected), priced_recommendations=len(known),
            average_reference_cost=known_mean if len(known) == len(result.selected) else None,
            known_reference_cost_mean=known_mean,
            mean_raw_relevance=sum(item.raw_score for item in result.selected) / len(result.selected),
            mean_percentile_relevance=sum(item.relevance for item in result.selected) / len(result.selected),
        )

    baseline, changed = summary(control), summary(treatment)
    overlap = len(set(baseline['recommendations']).intersection(changed['recommendations']))
    return dict(route=route, candidate_count=len(treatment.candidates),
                unknown_candidate_costs=treatment.unknown_cost_count, control=baseline, treatment=changed,
                overlap_count=overlap, overlap_fraction=overlap / k, changed_recommendations=k-overlap,
                changed_positions=sum(a != b for a, b in zip(baseline['recommendations'], changed['recommendations'])),
                mean_raw_relevance_difference=changed['mean_raw_relevance']-baseline['mean_raw_relevance'],
                mean_percentile_relevance_difference=changed['mean_percentile_relevance']-baseline['mean_percentile_relevance'])


def synthetic_diagnostic():
    """Fit a tiny synthetic model and exercise all three real candidate paths offline."""
    from src.model.recommender import fit
    from src.service.experiment_routing import ServingRequest
    from src.service.recommendation_candidates import LoadedModelCandidates

    users = [dict(user_id=f'warm-{letter}') for letter in ('a', 'b', 'c')]
    catalog = [dict(movie_id=f'synthetic-{i}', title=f'Synthetic movie {i}',
                    genres='Action' if i % 2 == 0 else 'Drama', overview='synthetic fixture') for i in range(70)]
    events = [dict(user_id=user['user_id'], movie_id=f'synthetic-{(index*7+j)%70}',
                   event_type='rating', rating=1+j%10, timestamp=f'2026-01-{j+1:02}T00:00:00Z')
              for index, user in enumerate(users) for j in range(12)]
    model = fit(events, users, catalog)

    class Profiles:
        def get_profile(self, user_id):
            return dict(positive='action', negative='drama') if user_id == 'cold' else None

        def enqueue(self, user_id):
            pass  # This offline fixture never writes jobs or connects to a database.

    config = CostAwareConfig()
    source = LoadedModelCandidates(model, Profiles())
    prices = {movie['movie_id']: Decimal('10' if i % 3 == 0 else '.1') for i, movie in enumerate(catalog)}
    reports = []
    for user, has_history in (('warm-a', True), ('cold', False), ('unknown', False)):
        request = ServingRequest(user, frozenset(), k=5, has_history=has_history)
        pool = source(request, config.candidate_limit(request.k))
        reports.append(compare_candidates(pool.candidates, prices, route=pool.route, k=request.k, config=config))
    return dict(diagnostic_only=True, data='synthetic', model_version='synthetic-diagnostic-v1',
                policy_version=treatment_version('synthetic-diagnostic-v1', config),
                cost_weight=str(config.cost_weight), route_coverage=dict(Counter(row['route'] for row in reports)),
                cases=reports,
                limitation='Catalog reference prices and model relevance only; no production savings or observed satisfaction.')


if __name__ == '__main__':
    from src.experimentation.cost_sensitivity import main

    main()
