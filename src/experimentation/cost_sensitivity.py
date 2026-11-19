"""Offline lambda sensitivity analysis over frozen candidates; never a DB workload."""

import argparse
import hashlib
import json
import math
import platform
from importlib.metadata import version
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from numbers import Real
from types import MappingProxyType
from typing import Mapping

from src.data_quality.boundary import validate_recommendation_item
from src.experimentation.cost_reranking import (
    CostAwareConfig, RerankingUnavailable, ScoredCandidate, reference_price, rerank,
)

LAMBDA_GRID = ('0.00', '0.05', '0.10', '0.15', '0.20', '0.30', '0.50', '0.75', '1.00')
ROUTES = ('collaborative_svd', 'llm_content', 'popularity_fallback')
ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    route: str
    candidates: tuple[ScoredCandidate, ...]
    catalog_costs: Mapping[str, object]
    control_ids: tuple[str, ...]
    exclusions: frozenset[str] = frozenset()
    control_scores: Mapping[str, float] = field(default_factory=dict)


def _finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _mean(values):
    values = list(values)
    if not values:
        return None
    return _finite(math.fsum(value / len(values) for value in values))


def _rate(numerator, denominator):
    return numerator / denominator if denominator else None


def _valid_id(movie_id, prices, exclusions):
    _, errors = validate_recommendation_item({'movie_id': movie_id})
    return not errors and movie_id in prices and movie_id not in exclusions


def _slate(ids, prices, scores, percentiles, exclusions):
    valid = [movie_id for movie_id in ids if _valid_id(movie_id, prices, exclusions)]
    costs = [reference_price(prices.get(movie_id)) for movie_id in ids]
    priced = [price for price in costs if price is not None]
    complete = bool(ids) and len(priced) == len(ids) and len(valid) == len(ids) == len(set(ids))
    total = sum(priced, Decimal(0)) if complete else None
    raw = [_finite(scores.get(movie_id)) for movie_id in ids]
    ranks = [percentiles.get(movie_id) for movie_id in ids]
    return dict(
        recommendations=list(ids), recommendation_count=len(ids), valid_recommendations=len(set(valid)),
        priced_recommendations=len(priced), missing_prices=len(ids)-len(priced),
        total_reference_cost=_finite(total),
        mean_reference_cost=_finite(total / len(ids)) if total is not None else None,
        known_reference_cost_mean=_finite(sum(priced) / len(priced)) if priced else None,
        mean_raw_relevance=_mean(raw) if raw and all(value is not None for value in raw) else None,
        mean_relevance_percentile=_mean(ranks) if ranks and all(value is not None for value in ranks) else None,
    )


def _cost_total(ids, prices):
    values = [reference_price(prices.get(movie_id)) for movie_id in ids]
    return sum(values, Decimal(0)) if values and all(value is not None for value in values) else None


def _fingerprint(candidates, prices, exclusions):
    payload = dict(candidates=[(item.movie_id, repr(item.score), str(prices.get(item.movie_id)),
                               item.movie_id in prices) for item in candidates], exclusions=sorted(exclusions))
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def evaluate_case(case: EvaluationCase, *, k=20):
    """Run the production reranker, once per lambda, on one immutable input snapshot."""
    limit = CostAwareConfig().candidate_limit(k)
    candidates = tuple(case.candidates)
    if len(candidates) > limit:
        raise ValueError('case candidate pool exceeds min(5*K, 200)')
    prices = MappingProxyType(dict(case.catalog_costs))
    exclusions = frozenset(case.exclusions)
    scores = {item.movie_id: item.score for item in candidates}
    scores.update(case.control_scores)
    fingerprint = _fingerprint(candidates, prices, exclusions)
    zero, zero_error = None, None
    try:
        zero = rerank(candidates, prices, k=k, config=CostAwareConfig(0), exclude_movie_ids=exclusions)
    except (RerankingUnavailable, ValueError) as error:
        zero_error = str(error)
    percentiles = {item.movie_id: item.relevance for item in zero.candidates} if zero else {}
    control = _slate(case.control_ids, prices, scores, percentiles, exclusions)
    baseline_valid = (len(case.control_ids) == k and control['valid_recommendations'] == k)
    zero_ids = tuple(item.movie_id for item in zero.selected) if zero else None
    rows = []
    for weight in LAMBDA_GRID:
        result, error = zero, zero_error
        if Decimal(weight) != 0:
            try:
                result = rerank(candidates, prices, k=k, config=CostAwareConfig(weight), exclude_movie_ids=exclusions)
                error = None
            except (RerankingUnavailable, ValueError) as exception:
                result, error = None, str(exception)
        row = dict(lambda_value=weight, fulfilled=result is not None, failure=error,
                   input_fingerprint=fingerprint, treatment=None, comparison=None)
        if result is not None:
            selected = tuple(item.movie_id for item in result.selected)
            treatment = _slate(selected, prices, scores, percentiles, exclusions)
            row['treatment'] = treatment
            if baseline_valid:
                overlap = len(set(selected).intersection(case.control_ids))
                baseline_positions = {movie_id: index for index, movie_id in enumerate(case.control_ids)}
                common_positions = [baseline_positions[movie_id] for movie_id in selected if movie_id in baseline_positions]
                inversions = sum(left > right for index, left in enumerate(common_positions) for right in common_positions[index+1:])
                common_pairs = len(common_positions)*(len(common_positions)-1)//2
                reordered = sum(movie_id in baseline_positions and baseline_positions[movie_id] != index
                                for index, movie_id in enumerate(selected))
                control_total, treatment_total = _cost_total(case.control_ids, prices), _cost_total(selected, prices)
                difference = treatment_total-control_total if control_total is not None and treatment_total is not None else None
                raw_loss = (_finite(control['mean_raw_relevance']-treatment['mean_raw_relevance'])
                            if control['mean_raw_relevance'] is not None and treatment['mean_raw_relevance'] is not None else None)
                rank_loss = (_finite(control['mean_relevance_percentile']-treatment['mean_relevance_percentile'])
                             if control['mean_relevance_percentile'] is not None and treatment['mean_relevance_percentile'] is not None else None)
                row['comparison'] = dict(
                    overlap=overlap, replaced_movies=k-overlap,
                    changed_positions=sum(a != b for a, b in zip(case.control_ids, selected)),
                    reordered_common_movies=reordered, common_order_inversions=inversions,
                    comparable_common_pairs=common_pairs, common_order_inversion_rate=_rate(inversions, common_pairs),
                    raw_relevance_loss=raw_loss, percentile_relevance_loss=rank_loss,
                    lower_predicted_relevance=raw_loss > 1e-12 if raw_loss is not None else None,
                    cost_difference=_finite(difference),
                    cost_difference_percent=_finite(100*difference/control_total) if difference is not None and control_total else None,
                    no_cost_savings=difference >= 0 if difference is not None else None,
                )
        rows.append(row)
    valid_candidates = {item.movie_id for item in candidates if _valid_id(item.movie_id, prices, exclusions)
                        and isinstance(item.score, Real) and not isinstance(item.score, bool) and _finite(item.score) is not None}
    missing_keys = {item.movie_id for item in candidates if item.movie_id not in prices}
    unknown = sum(reference_price(prices[movie_id]) is None for movie_id in valid_candidates)
    return dict(
        case_id=case.case_id, route=case.route, candidate_count=len(candidates),
        valid_candidate_count=len(valid_candidates), valid_priced_candidates=len(valid_candidates)-unknown,
        unknown_candidate_prices=unknown, catalog_membership_failures=len(missing_keys),
        exclusion_count=len(exclusions), input_fingerprint=fingerprint,
        ranking_fingerprint=hashlib.sha256(json.dumps([(item.movie_id, repr(item.score)) for item in candidates]).encode()).hexdigest(),
        control=control, control_valid=baseline_valid,
        lambda_zero=_slate(zero_ids, prices, scores, percentiles, exclusions) if zero_ids is not None else None,
        lambda_zero_same_items=set(zero_ids) == set(case.control_ids) if zero_ids is not None else None,
        lambda_zero_same_order=zero_ids == case.control_ids if zero_ids is not None else None,
        lambda_zero_failure=zero_error, rows=rows,
    )


def aggregate_route(cases, *, k):
    """Equal weight per attempted user; paired metrics name their smaller denominators."""
    output = []
    for index, weight in enumerate(LAMBDA_GRID):
        attempts = [case['rows'][index] for case in cases]
        fulfilled = [(case, row) for case, row in zip(cases, attempts) if row['fulfilled']]
        comparable = [(case, row) for case, row in fulfilled if row['comparison'] is not None]
        priced = [(case, row) for case, row in comparable
                  if case['control']['total_reference_cost'] is not None
                  and row['treatment']['total_reference_cost'] is not None
                  and row['comparison']['cost_difference'] is not None]
        raw = [row['comparison']['raw_relevance_loss'] for _, row in comparable
               if row['comparison']['raw_relevance_loss'] is not None]
        ranks = [row['comparison']['percentile_relevance_loss'] for _, row in comparable
                 if row['comparison']['percentile_relevance_loss'] is not None]
        control_cost = _mean(case['control']['total_reference_cost'] for case, _ in priced)
        treatment_cost = _mean(row['treatment']['total_reference_cost'] for _, row in priced)
        difference = _mean(row['comparison']['cost_difference'] for _, row in priced)
        valid_outputs = sum(row['treatment']['valid_recommendations'] for _, row in fulfilled)
        replaced = sum(row['comparison']['replaced_movies'] for _, row in comparable)
        lower_relevance_users = sum(row['comparison']['lower_predicted_relevance'] is True for _, row in comparable)
        missing_recommended = sum(row['treatment']['missing_prices'] for _, row in fulfilled)
        output.append(dict(
            lambda_value=weight, attempted_users=len(cases), fulfilled_users=len(fulfilled),
            fulfillment_rate=_rate(len(fulfilled), len(cases)), comparison_users=len(comparable),
            cost_paired_users=len(priced), raw_relevance_users=len(raw), percentile_relevance_users=len(ranks),
            valid_recommendations=valid_outputs, valid_output_slot_rate=_rate(valid_outputs, len(cases)*k),
            control_mean_cost_per_movie=control_cost/k if control_cost is not None else None,
            treatment_mean_cost_per_movie=treatment_cost/k if treatment_cost is not None else None,
            control_mean_slate_cost=control_cost, treatment_mean_slate_cost=treatment_cost,
            mean_slate_cost_difference=difference,
            mean_movie_cost_difference=difference/k if difference is not None else None,
            cost_difference_percent=_finite(100*difference/control_cost) if difference is not None and control_cost else None,
            mean_top_k_overlap=_mean(row['comparison']['overlap'] for _, row in comparable),
            mean_top_k_overlap_fraction=_mean(row['comparison']['overlap']/k for _, row in comparable),
            replaced_movies=replaced, replaced_movie_rate=_rate(replaced, len(comparable)*k),
            users_with_replacement=sum(row['comparison']['replaced_movies'] > 0 for _, row in comparable),
            users_with_replacement_rate=_rate(sum(row['comparison']['replaced_movies'] > 0 for _, row in comparable), len(comparable)),
            mean_changed_positions=_mean(row['comparison']['changed_positions'] for _, row in comparable),
            users_with_order_change=sum(row['comparison']['changed_positions'] > 0 for _, row in comparable),
            users_with_order_change_rate=_rate(sum(row['comparison']['changed_positions'] > 0 for _, row in comparable), len(comparable)),
            mean_reordered_common_movies=_mean(row['comparison']['reordered_common_movies'] for _, row in comparable),
            mean_common_order_inversions=_mean(row['comparison']['common_order_inversions'] for _, row in comparable),
            common_order_comparable_users=sum(row['comparison']['comparable_common_pairs'] > 0 for _, row in comparable),
            users_with_relative_order_change=sum(row['comparison']['common_order_inversions'] > 0 for _, row in comparable),
            relative_order_change_user_rate=_rate(sum(row['comparison']['common_order_inversions'] > 0 for _, row in comparable),
                                                  sum(row['comparison']['comparable_common_pairs'] > 0 for _, row in comparable)),
            reorder_only_users=sum(row['comparison']['replaced_movies'] == 0 and row['comparison']['changed_positions'] > 0 for _, row in comparable),
            control_mean_raw_relevance=_mean(case['control']['mean_raw_relevance'] for case, row in comparable
                                             if row['comparison']['raw_relevance_loss'] is not None),
            treatment_mean_raw_relevance=_mean(row['treatment']['mean_raw_relevance'] for _, row in comparable
                                               if row['comparison']['raw_relevance_loss'] is not None),
            mean_raw_relevance_loss=_mean(raw), mean_percentile_relevance_loss=_mean(ranks),
            control_mean_relevance_percentile=_mean(case['control']['mean_relevance_percentile'] for case, row in comparable
                                                   if row['comparison']['percentile_relevance_loss'] is not None),
            treatment_mean_relevance_percentile=_mean(row['treatment']['mean_relevance_percentile'] for _, row in comparable
                                                     if row['comparison']['percentile_relevance_loss'] is not None),
            lower_predicted_relevance_users=lower_relevance_users,
            lower_predicted_relevance_user_rate=_rate(lower_relevance_users, len(raw)),
            missing_recommended_prices=missing_recommended,
            missing_recommended_price_rate=_rate(missing_recommended, len(fulfilled)*k),
            no_cost_savings_users=sum(row['comparison']['no_cost_savings'] is True for _, row in priced),
            no_cost_savings_rate=_rate(sum(row['comparison']['no_cost_savings'] is True for _, row in priced), len(priced)),
            insufficient_pool_users=sum(row['failure'] == 'insufficient valid catalog candidates' for row in attempts),
            no_known_price_users=sum(row['failure'] == 'no known catalog reference prices' for row in attempts),
            failure_counts=dict(Counter(row['failure'] for row in attempts if not row['fulfilled'])),
        ))
    return output


def analyze(cases, *, k=20, dataset=None):
    evaluated = [evaluate_case(case, k=k) for case in cases]
    groups = defaultdict(list)
    for case in evaluated:
        groups[case['route']].append(case)
    routes = {}
    for route, items in sorted(groups.items()):
        candidate_count = sum(case['valid_candidate_count'] for case in items)
        unknown = sum(case['unknown_candidate_prices'] for case in items)
        zero_available = sum(case['lambda_zero'] is not None for case in items)
        routes[route] = dict(
            evaluated_users=len(items),
            eligible_users=(dataset or {}).get('eligible_users_per_route', {}).get(route),
            distinct_candidate_rankings=len({case['ranking_fingerprint'] for case in items}),
            candidate_observations=sum(case['candidate_count'] for case in items),
            candidate_counts=[case['candidate_count'] for case in items],
            valid_priced_candidates=sum(case['valid_priced_candidates'] for case in items),
            unknown_candidate_prices=unknown, missing_candidate_price_rate=_rate(unknown, candidate_count),
            catalog_membership_failures=sum(case['catalog_membership_failures'] for case in items),
            catalog_membership_failure_rate=_rate(sum(case['catalog_membership_failures'] for case in items),
                                                  sum(case['candidate_count'] for case in items)),
            lambda_zero_available_users=zero_available,
            lambda_zero_item_mismatch_users=sum(case['lambda_zero_same_items'] is False for case in items),
            lambda_zero_order_mismatch_users=sum(case['lambda_zero_same_order'] is False for case in items),
            rows=aggregate_route(items, k=k),
        )
    report = dict(
        diagnostic_only=True, dataset=dataset or {}, k=k, lambda_grid=list(LAMBDA_GRID),
        runtime=dict(python=platform.python_version(), numpy=version('numpy'), sklearn=version('scikit-learn')),
        candidate_configuration=dict(multiplier=5, cap=200, requested_pool_size=CostAwareConfig().candidate_limit(k)),
        weighting='Equal weight per evaluated user within each route; no pooled cross-route relevance or overall aggregate.',
        denominators='Fulfillment: attempted users. Overlap/replacement/order: fulfilled users with K valid Control IDs. '
                     'Cost: fully priced paired slates. Relevance: compatible complete score pairs. Missing-price slots: fulfilled users*K.',
        default_lambda_unchanged='0.15', routes=routes, cases=evaluated,
        implementation_sha256={name: hashlib.sha256((ROOT/'src/experimentation'/name).read_bytes()).hexdigest()
                               for name in ('cost_reranking.py', 'cost_sensitivity.py')},
        limitations=['Catalog reference prices are not observed licensing charges.',
                     'Historical in-sample model scores are not held-out satisfaction evidence.',
                     'A lambda recommendation requires team review and representative quality evidence.'],
    )
    # Fail explicitly if a future metric accidentally emits JSON NaN or Infinity.
    json.dumps(report, allow_nan=False)
    return report


class _MemoryProfiles:
    def __init__(self, profiles):
        self.profiles = profiles

    def get_profile(self, user_id):
        return self.profiles.get(user_id)

    def enqueue(self, user_id):
        pass  # Offline: no DB jobs, external descriptions or LLM requests.


def _build_cases(model, profiles, histories, selected, prices, *, seed, k):
    from src.service.cold_start import recommend_cold_start
    from src.service.experiment_routing import ServingRequest
    from src.service.model_adapter import recommend
    from src.service.recommendation_candidates import LoadedModelCandidates

    store = _MemoryProfiles(profiles)
    source = LoadedModelCandidates(model, store)
    cases = []
    for user_id in selected:
        excluded = frozenset(histories.get(user_id, ()))
        request = ServingRequest(user_id, excluded, k=k, has_history=bool(excluded))
        pool = source(request, CostAwareConfig().candidate_limit(k))
        if request.has_history and pool.route == 'collaborative_svd':
            ids, control_route = recommend(model, user_id, k=k, exclude_movie_ids=excluded)
        else:
            control_result = recommend_cold_start(model, store, user_id, k=k, exclude_movie_ids=excluded, allow_svd=False)
            ids, control_route = control_result if control_result is not None else recommend(
                model, user_id, k=k, exclude_movie_ids=excluded, allow_svd=False)
        if control_route != pool.route:
            raise ValueError('Control and candidate generator chose different routes')
        opaque_id = hashlib.sha256(f'{seed}:{user_id}'.encode()).hexdigest()[:16]
        cases.append(EvaluationCase(opaque_id, pool.route, pool.candidates, prices, tuple(ids), excluded,
                                    {item.movie_id: item.score for item in pool.candidates}))
    return cases


def recorded_cases(*, sample_size=100, seed=42, k=20):
    """Use only checked-in historical M0 artifacts and course reference metadata."""
    import joblib
    import numpy as np
    from src.model.recommender import load_data, description_hash
    from src.model.routing import uses_svd
    from src.service.model_adapter import validate_model

    directory = ROOT / 'planning/Milestone1/modelTesting/data'
    manifest = json.loads((ROOT / 'src/model/data_manifest.json').read_text())
    hashes = {}
    for name, entry in manifest.items():
        digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        if digest != entry['sha256']:
            raise ValueError(f'Recorded dataset checksum mismatch: {name}')
        hashes[name] = digest
    model_path = ROOT / 'planning/Milestone1/modelTesting/full_models/jace_model.joblib'
    model = joblib.load(model_path)  # Trusted checked-in artifact, never a remote/production path.
    validate_model(model)
    events, users, catalog = load_data(directory)
    if {movie['movie_id'] for movie in model['movies']} != {movie['movie_id'] for movie in catalog}:
        raise ValueError('Recorded model and catalog IDs differ')
    profile_path = ROOT / 'src/model/llm_profiles.json'
    saved = json.loads(profile_path.read_text())
    by_user = {user['user_id']: user for user in users}
    profiles = saved['profiles']
    if any(user not in by_user or profile['input_sha256'] != description_hash(by_user[user])
           for user, profile in profiles.items()):
        raise ValueError('Recorded profiles do not match recorded user descriptions')
    histories = defaultdict(set)
    for event in events:
        if event['event_type'] in ('watch', 'rating'):
            histories[event['user_id']].add(event['movie_id'])
    eligible = defaultdict(list)
    for user in sorted(by_user):
        if histories[user] and uses_svd(model, user):
            route = 'collaborative_svd'
        elif user in profiles and (profiles[user]['positive'].strip() or profiles[user]['negative'].strip()):
            route = 'llm_content'
        else:
            route = 'popularity_fallback'
        eligible[route].append(user)
    rng = np.random.default_rng(seed)
    selected = []
    for route in ROUTES:
        population = eligible[route]
        selected.extend(sorted(rng.choice(population, size=min(sample_size, len(population)), replace=False).tolist()))
    prices = {movie['movie_id']: movie.get('license_cost') for movie in catalog}
    cases = _build_cases(model, profiles, histories, selected, prices, seed=seed, k=k)
    metadata = dict(
        source='recorded-m0', seed=seed, sample_limit_per_route=sample_size,
        model_path=str(model_path.relative_to(ROOT)), model_artifact_sha256=hashlib.sha256(model_path.read_bytes()).hexdigest(),
        embedded_model_version=model.get('update_metadata', {}).get('version'), data_sha256=hashes,
        profile_sha256=hashlib.sha256(profile_path.read_bytes()).hexdigest(),
        catalog_movies=len(catalog), catalog_valid_prices=sum(reference_price(value) is not None for value in prices.values()),
        historical_users=len(users), historical_events=len(events), saved_profiles=len(profiles),
        eligible_users_per_route={route: len(eligible[route]) for route in ROUTES},
        evaluated_users_per_route=dict(Counter(case.route for case in cases)), total_evaluated_users=len(cases),
        relevance_source='Actual scores from the checked-in trained M0 snapshot; historical in-sample, not held-out labels.',
        price_source='Recorded public-course movies.csv.gz reference license_cost; not production charges or invoices.',
        profile_source='50 saved assistant-authored search summaries of supplied descriptions, with matching input hashes.',
        history_source='Recorded course watch/rating history; no production database or synthetic price substitution.',
        representativeness='Historical course snapshot; popularity population is only two users. Not representative of current production.',
    )
    return cases, metadata


def synthetic_cases(*, sample_size=100, seed=42, k=20):
    """Generate varied users, real model-computed synthetic scores, and labeled synthetic prices."""
    import numpy as np
    from src.model.recommender import fit

    rng = np.random.default_rng(seed)
    genres = ('action', 'comedy', 'drama', 'horror', 'science fiction', 'romance', 'mystery', 'adventure')
    count = max(140, CostAwareConfig().candidate_limit(k)+2*k+30)
    catalog = [dict(movie_id=f'synthetic-{i}', title=f'Synthetic movie {i}',
                    genres=genres[i % len(genres)], overview=f'{genres[(i*3+1) % len(genres)]} journey') for i in range(count)]
    users = [dict(user_id=f'warm-{i}') for i in range(max(3, sample_size))]
    events, histories = [], defaultdict(set)
    for user in users:
        for j, index in enumerate(rng.choice(count, size=25, replace=False).tolist()):
            movie_id = catalog[index]['movie_id']
            events.append(dict(user_id=user['user_id'], movie_id=movie_id, event_type='rating',
                               rating=int(rng.integers(1, 11)), timestamp=f'2026-01-{j+1:02}T00:00:00Z'))
            histories[user['user_id']].add(movie_id)
    model = fit(events, users, catalog)
    profiles = {}
    selected = [user['user_id'] for user in users[:sample_size]]
    for index in range(sample_size):
        cold, popular = f'cold-{index}', f'popularity-{index}'
        profiles[cold] = dict(positive=genres[index % len(genres)], negative=genres[(index+3) % len(genres)])
        for user in (cold, popular):
            histories[user].update(catalog[int(i)]['movie_id'] for i in rng.choice(count, size=10, replace=False))
        selected.extend((cold, popular))
    prices = {}
    for movie in catalog:
        draw = float(rng.random())
        prices[movie['movie_id']] = (None if draw < .02 else '0' if draw < .04
                                    else str(round(float(rng.lognormal(-3, 1.2)), 10)))
    cases = _build_cases(model, profiles, histories, selected, prices, seed=seed, k=k)
    metadata = dict(source='synthetic', seed=seed, sample_limit_per_route=sample_size,
        catalog_movies=count, historical_users=len(users), historical_events=len(events),
        eligible_users_per_route={route: sample_size for route in ROUTES},
        evaluated_users_per_route=dict(Counter(case.route for case in cases)), total_evaluated_users=len(cases),
        catalog_valid_prices=sum(reference_price(value) is not None for value in prices.values()),
        relevance_source='Real inference calculations from an in-memory model fitted to synthetic ratings/documents.',
        price_source='Seeded synthetic lognormal reference prices, with synthetic zero/unknown prices.',
        profile_source='Synthetic genre preferences; synthetic per-user historical exclusions diversify candidate sets.',
        representativeness='Synthetic stress sample only; not observed users, prices, ratings or production costs.')
    return cases, metadata


def run_sensitivity(*, dataset='recorded', sample_size=100, seed=42, k=20):
    if type(sample_size) is not int or not 1 <= sample_size <= 1000:
        raise ValueError('sample_size must be an integer from 1 to 1000 per route')
    if type(seed) is not int or not 0 <= seed <= 2**32-1:
        raise ValueError('seed must be an integer from zero to 2**32-1')
    CostAwareConfig().candidate_limit(k)
    if dataset not in ('recorded', 'synthetic'):
        raise ValueError('dataset must be recorded or synthetic')
    loader = recorded_cases if dataset == 'recorded' else synthetic_cases
    cases, metadata = loader(sample_size=sample_size, seed=seed, k=k)
    return analyze(cases, k=k, dataset=metadata)


def summary_table(report):
    def number(value, digits=4):
        return 'NA' if value is None else f'{value:.{digits}f}'

    lines = [f"Dataset: {report['dataset'].get('source', 'custom')} | K={report['k']} | default lambda unchanged: 0.15",
             'Route                  lambda users fulfill% ref/movie deltaCost% replace% usersChanged% rankLoss noSaving%']
    for route, group in report['routes'].items():
        for row in group['rows']:
            lines.append(f"{route:22} {row['lambda_value']:>5} {row['attempted_users']:5} "
                f"{number(100*row['fulfillment_rate'] if row['fulfillment_rate'] is not None else None, 1):>8} "
                f"{number(row['treatment_mean_cost_per_movie']):>9} {number(row['cost_difference_percent'], 2):>10} "
                f"{number(100*row['replaced_movie_rate'] if row['replaced_movie_rate'] is not None else None, 2):>8} "
                f"{number(100*row['users_with_replacement_rate'] if row['users_with_replacement_rate'] is not None else None, 1):>13} "
                f"{number(row['mean_percentile_relevance_loss']):>8} "
                f"{number(100*row['no_cost_savings_rate'] if row['no_cost_savings_rate'] is not None else None, 1):>9}")
    lines.append('Reference metadata only; no observed production savings or satisfaction conclusions. See JSON for denominators/data quality.')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', choices=('recorded', 'synthetic'), default='recorded')
    parser.add_argument('--sample-size', type=int, default=100, help='Maximum evaluated users per route (1..1000).')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--k', type=int, default=20)
    parser.add_argument('--format', choices=('table', 'json'), default='table')
    parser.add_argument('--json', dest='json_path', help='Explicit JSON output path, or - for JSON-only stdout.')
    args = parser.parse_args(argv)
    try:
        report = run_sensitivity(dataset=args.dataset, sample_size=args.sample_size, seed=args.seed, k=args.k)
    except (ValueError, FileNotFoundError) as error:
        parser.error(str(error))
    encoded = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + '\n'
    if args.json_path and args.json_path != '-':
        Path(args.json_path).write_text(encoded)
    print(encoded if args.format == 'json' or args.json_path == '-' else summary_table(report), end='')
    if args.format != 'json' and args.json_path != '-':
        print()


if __name__ == '__main__':
    main()
