"""Reproducible meeting diagnostics for the unchanged Milestone 0 model.

Run: python benchmark_m1.py. Writes meeting/model_metrics.json.
No live traffic, HTTP, LLM generation, or VM performance is measured here.
"""
import hashlib
import json
import os
import platform
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone

import joblib
import numpy as np
import scipy
import sklearn
from threadpoolctl import threadpool_info, threadpool_limits

from recommender import ROOT, fit, load_data, load_profiles, recommend


def split_pairs(events):
    interactions = sorted((e for e in events if e['event_type'] != 'account_created'),
                          key=lambda e: e['timestamp'])
    latest = defaultdict(dict)
    for e in interactions:
        latest[e['user_id']][e['movie_id']] = e['timestamp']
    held = set()
    for u, pairs in latest.items():
        ordered = sorted(pairs, key=lambda m: (pairs[m], m))
        n = max(1, int(0.2 * len(ordered)))
        held.update((u, m) for m in ordered[-n:])
    train = [e for e in interactions if (e['user_id'], e['movie_id']) not in held]
    test = [e for e in interactions if (e['user_id'], e['movie_id']) in held]
    assert not ({(e['user_id'], e['movie_id']) for e in train} & held)
    return train, test


def ranking_summary(predictions, truth, catalog_size, k):
    precision, recall, hit, ndcg = [], [], [], []
    covered = set()
    for u, relevant in truth.items():
        picks = predictions[u][:k]
        covered.update(picks)
        gains = np.array([float(m in relevant) for m in picks])
        found = int(gains.sum())
        precision.append(found / k)
        recall.append(found / len(relevant))
        hit.append(float(found > 0))
        dcg = float(np.sum(gains / np.log2(np.arange(2, len(picks) + 2))))
        ideal = float(np.sum(1 / np.log2(np.arange(2, min(k, len(relevant)) + 2))))
        ndcg.append(dcg / ideal)
    return {'k': k, 'users': len(truth), 'precision': float(np.mean(precision)),
            'recall': float(np.mean(recall)), 'hit_rate': float(np.mean(hit)),
            'ndcg': float(np.mean(ndcg)), 'catalog_coverage': len(covered) / catalog_size,
            'distinct_recommended_movies': len(covered)}


def errors(predictions, actual):
    residual = predictions - actual
    return {'rmse': float(np.sqrt(np.mean(residual ** 2))),
            'mae': float(np.mean(np.abs(residual))),
            'binary_accuracy_rating_at_least_7': float(np.mean((predictions >= 7) == (actual >= 7))),
            'predicted_positive_fraction': float(np.mean(predictions >= 7))}


def latency(model, ids, profiles, count=1000):
    for i in range(50):
        recommend(model, ids[i % len(ids)], profiles, 20)
    measurements = []
    start = time.perf_counter()
    for i in range(count):
        before = time.perf_counter()
        recommend(model, ids[i % len(ids)], profiles, 20)
        measurements.append(1000 * (time.perf_counter() - before))
    elapsed = time.perf_counter() - start
    return {'requests': count, 'warmup_requests': 50, 'p50_ms': float(np.median(measurements)),
            'p95_ms': float(np.percentile(measurements, 95)),
            'p99_ms': float(np.percentile(measurements, 99)), 'max_ms': max(measurements),
            'serial_calls_per_second': count / elapsed}


def main():
    # Verify metric behavior on an independently interpretable ranked list.
    sample = ranking_summary({'u': ['a', 'x']}, {'u': {'a', 'b'}}, 4, 2)
    assert sample['precision'] == 0.5 and sample['recall'] == 0.5
    assert np.isclose(sample['ndcg'], 1 / (1 + 1 / np.log2(3)))
    out = ROOT / 'meeting'
    out.mkdir(exist_ok=True)
    events, users, movies = load_data(ROOT / 'data')
    train, test = split_pairs(events)
    with threadpool_limits(limits=1):
        model = fit(train, users, movies)
        latest = {}
        for e in sorted(test, key=lambda e: e['timestamp']):
            if e['event_type'] == 'rating':
                latest[e['user_id'], e['movie_id']] = float(e['rating'])
        truth = defaultdict(set)
        actual, estimated = [], []
        for (u, m), rating in latest.items():
            if not model['seen'][u]:
                continue
            actual.append(rating)
            raw = float(model['user_factors'][model['ui'][u]] @ model['movie_factors'][:, model['mi'][m]])
            estimated.append(np.clip(5.5 + 4.5 * raw, 1, 10))
            if rating >= 7:
                truth[u].add(m)
        predictions, popular = {}, {}
        for u in truth:
            predictions[u] = [r['movie_id'] for r in recommend(model, u, {}, 20)['recommendations']]
            assert not ({model['mi'][m] for m in predictions[u]} & model['seen'][u])
            scores = model['popularity'].copy()
            scores[list(model['seen'][u])] = -np.inf
            popular[u] = [movies[i]['movie_id'] for i in np.argsort(-scores, kind='stable')
                          if np.isfinite(scores[i])][:20]
        ranking = {str(k): {'svd': ranking_summary(predictions, truth, len(movies), k),
                            'popularity': ranking_summary(popular, truth, len(movies), k)}
                   for k in (10, 20)}
        actual, estimated = np.asarray(actual), np.asarray(estimated)
        train_ratings = [float(e['rating']) for e in train if e['event_type'] == 'rating']
        mean_rating = float(np.mean(train_ratings))
        rating_errors = {'test_ratings': len(actual), 'positive_fraction': float(np.mean(actual >= 7)),
                         'conversion': 'clip(5.5 + 4.5 * SVD score, 1, 10); no calibration fitted',
                         'svd': errors(estimated, actual),
                         'training_mean_baseline_value': mean_rating,
                         'training_mean_baseline': errors(np.full(len(actual), mean_rating), actual)}
        profiles = load_profiles(ROOT / 'llm_profiles.json')
        runs = []
        artifact = ROOT / 'artifacts/m1_benchmark.joblib'
        artifact.parent.mkdir(exist_ok=True)
        for _ in range(3):
            t0 = time.perf_counter()
            data = load_data(ROOT / 'data')
            t1 = time.perf_counter()
            full = fit(*data)
            t2 = time.perf_counter()
            joblib.dump(full, artifact, compress=0)
            t3 = time.perf_counter()
            runs.append({'load_validate_seconds': t1-t0, 'fit_seconds': t2-t1,
                         'serialize_seconds': t3-t2, 'total_seconds': t3-t0})
        warm_ids = sorted(u for u, seen in full['seen'].items() if seen)
        cold_ids = sorted(profiles)
        serving = {'warm': latency(full, warm_ids, profiles),
                   'cold_cached_profile': latency(full, cold_ids, profiles),
                   'scope': 'In-process recommend(k=20), single serial client, loaded model; '
                            'excludes HTTP, network, metadata API, queueing, and LLM generation.'}
        pool = threadpool_info()
    try:
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        commit = 'unavailable'
    size = artifact.stat().st_size
    result = {'measured_at_utc': datetime.now(timezone.utc).isoformat(), 'base_commit': commit,
              'source_sha256': {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
                                for p in ['recommender.py', 'benchmark_m1.py', 'llm_profiles.json']},
              'environment': {'platform': platform.platform(), 'python': platform.python_version(),
                              'processor': platform.processor(), 'logical_cpus': os.cpu_count(),
                              'numpy': np.__version__, 'scipy': scipy.__version__,
                              'sklearn': sklearn.__version__, 'threadpools_during_measurement': pool},
              'split': {'method': 'Last max(1, floor(0.2*n)) movie pairs per user, ordered by latest '
                                  'pair timestamp then movie ID; both watch and rating held out together.',
                        'train_events': len(train), 'test_events': len(test),
                        'train_pairs': len({(e['user_id'], e['movie_id']) for e in train}),
                        'test_pairs': len(latest), 'train_fraction': len(train)/(len(train)+len(test)),
                        'ranking_users': len(truth), 'positive_test_movies': sum(map(len, truth.values())),
                        'limitation': 'Cross-user calendar-time leakage possible; exploratory per-user split. '
                                      'No validation set or hyperparameter search; not a global-time simulation.'},
              'ranking': ranking, 'rating_diagnostics': rating_errors,
              'training': {'runs': runs, 'median_total_seconds': float(np.median([r['total_seconds'] for r in runs])),
                           'median_fit_seconds': float(np.median([r['fit_seconds'] for r in runs])),
                           'scope': 'Full 54704-event dataset, including CSV load, validation, preparation, '
                                    'SVD and TF-IDF fitting, and uncompressed model serialization; '
                                    'excludes download and LLM profile generation; OS cache not cleared.'},
              'inference': serving,
              'size': {'serving_artifact_bytes': size, 'serving_artifact_mib': size/(1024**2),
                       'profiles_bytes': (ROOT / 'llm_profiles.json').stat().st_size,
                       'svd_factor_bytes': full['user_factors'].nbytes + full['movie_factors'].nbytes,
                       'scope': 'Full uncompressed joblib serving bundle plus separately reported profiles. '
                                'No RAM measurement; excludes Python packages, container, and optional local LLM weights.'}}
    (out / 'model_metrics.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
