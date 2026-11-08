"""Fixed temporal evaluation and observed production metrics, not counterfactual claims."""
from collections import defaultdict
from datetime import timedelta
import math
import time

import numpy as np

from src.service.model_adapter import recommend
from src.service.cold_start import recommend_cold_start
from src.model_updates.snapshot import utc


class FrozenProfiles:
    def __init__(self, profiles):
        self.profiles = {p['user_id']: p for p in profiles}

    def get_profile(self, user_id):
        return self.profiles.get(user_id)


def rank(model, user, profiles, excluded=()):
    # Same score formulas as the serving paths; mask identical history BEFORE top-k.
    profile = profiles.get_profile(user)
    if model['seen'].get(user):
        values = model['user_factors'][model['ui'][user]] @ model['movie_factors']
    elif profile and (profile['positive'].strip() or profile['negative'].strip()):
        query = model['vectorizer'].transform([profile['positive'], profile['negative']])
        matches = (model['content'] @ query.T).toarray()
        values = matches[:, 0] - .7 * matches[:, 1] + .02 * model['popularity']
    else:
        values = model['popularity'].copy()
    values = np.asarray(values).copy()
    excluded = set(excluded)
    for i, movie in enumerate(model['movies']):
        if movie['movie_id'] in excluded:
            values[i] = -np.inf
    return [model['movies'][int(i)]['movie_id'] for i in np.argsort(-values, kind='stable')
            if np.isfinite(values[i])][:20]


def scores(ranking, relevant):
    hits = [1 if movie in relevant else 0 for movie in ranking[:20]]
    ideal = sum(1 / math.log2(i + 2) for i in range(min(20, len(relevant))))
    dcg = sum(hit / math.log2(i + 2) for i, hit in enumerate(hits))
    return {'ndcg_at_20': dcg / ideal if ideal else 0.,
            'recall_at_20': sum(hits) / len(relevant) if relevant else 0.}


def compare(champion, candidate, history, heldout, profiles, min_users=100):
    if min_users < 2:
        raise ValueError('min_users must be at least 2')
    catalog = {m['movie_id'] for m in champion['movies']}
    if catalog != {m['movie_id'] for m in candidate['movies']}:
        raise ValueError('Comparison catalogs must match')
    seen = defaultdict(set)
    for event in history:
        if event['movie_id']:
            seen[event['user_id']].add(event['movie_id'])
    # Last rating of each pair in the held-out window defines relevance.
    latest = {(e['user_id'], e['movie_id']): e for e in heldout if e['event_type'] == 'rating'}
    relevant = defaultdict(set)
    excluded = 0
    for (user, movie), event in latest.items():
        if event['rating'] >= 7:
            if movie in catalog and movie not in seen[user]:
                relevant[user].add(movie)
            else:
                excluded += 1
    frozen = FrozenProfiles(profiles)
    rows, elapsed = [], []
    for user in sorted(relevant):
        metrics = {}
        for name, model in (('champion', champion), ('candidate', candidate)):
            before = time.perf_counter()
            # Identical external history exclusion for both models, including stale champion.
            ranking = rank(model, user, frozen, seen[user])
            metrics[name] = scores(ranking, relevant[user])
            if name == 'candidate':
                elapsed.append((time.perf_counter() - before) * 1000)
        rows.append(dict(user_id=user, cohort='warm' if champion['seen'].get(user) else 'cold', **metrics))
    delta = np.array([r['candidate']['ndcg_at_20'] - r['champion']['ndcg_at_20'] for r in rows])
    lower = None
    if len(rows) >= min_users:
        rng = np.random.default_rng(42)
        means = [float(rng.choice(delta, len(delta), replace=True).mean()) for _ in range(1000)]
        lower = float(np.quantile(means, .05))
    groups = {}
    for cohort in ('all', 'warm', 'cold'):
        selected = [r for r in rows if cohort == 'all' or r['cohort'] == cohort]
        groups[cohort] = {'users': len(selected)}
        for name in ('champion', 'candidate'):
            groups[cohort][name] = {metric: float(np.mean([r[name][metric] for r in selected])) if selected else None
                                   for metric in ('ndcg_at_20', 'recall_at_20')}
    return dict(groups=groups, excluded_positive_pairs=excluded, paired_delta_lower_95=lower,
                candidate_p95_ms=float(np.percentile(elapsed, 95)) if elapsed else None,
                min_users=min_users, bootstrap_samples=1000, bootstrap_seed=42)


def production_report(snapshot, start, end):
    """Observed served-request conversion; never infer failed requests absent from the DB."""
    serves = sorted(snapshot['serves'], key=lambda r: utc(r['served_at']))
    by_user = defaultdict(list)
    for row in serves:
        by_user[row['user_id']].append(row)
    converted = set()
    movies = {m['movie_id']: m for m in snapshot['movies']}
    # Minute zero is an explicit view-start proxy; missing minute indexes aren't guessed.
    starts = {(e['user_id'], e['movie_id'], utc(e['event_time'])) for e in snapshot['events']
              if e['event_type'] == 'watch' and e.get('minute_index') == 0}
    known_cost, missing_cost, views = 0., 0, 0
    for user, movie, when in sorted(starts):
        if start <= when < end:
            views += 1
            cost = movies.get(movie, {}).get('license_cost')
            if cost is None:
                missing_cost += 1
            else:
                known_cost += float(cost)
        eligible = [s for s in by_user[user] if utc(s['served_at']) <= when <= utc(s['served_at']) + timedelta(hours=1)]
        if eligible:
            serve = eligible[-1]
            if movie in {m['movie_id'] for m in serve['recommendations']}:
                converted.add(str(serve['request_id']))
    durations = [float(s['serve_duration_ms']) for s in serves]
    usage = snapshot['usage']
    return dict(recorded_serves=len(serves), converted_requests=len(converted),
                recommendation_to_watch_rate=len(converted) / len(serves) if serves else None,
                recorded_personalized_fraction=sum(s['model_route'] in ('collaborative_svd', 'llm_content') for s in serves) / len(serves) if serves else None,
                response_p95_ms=float(np.percentile(durations, 95)) if durations else None,
                failure_rate=None, failure_rate_note='Serve table does not contain failed requests; do not treat missing failures as zero.',
                view_starts=views, known_license_cost_usd=known_cost, views_missing_license_cost=missing_cost,
                llm_records=len(usage), input_tokens=sum(u['input_tokens'] for u in usage),
                output_tokens=sum(u['output_tokens'] for u in usage),
                known_llm_cost_usd=sum(float(u['estimated_cost_usd']) for u in usage if u.get('estimated_cost_usd') is not None),
                llm_records_missing_cost=sum(u.get('estimated_cost_usd') is None for u in usage),
                telemetry_note='Empty tables mean unavailable evidence, not proven zero activity. View proxy requires minute_index=0.')
