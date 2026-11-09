"""Reproduce a global chronological holdout on the fixed course dataset, offline."""
import argparse
from datetime import UTC, datetime
from pathlib import Path

from threadpoolctl import threadpool_limits

from src.model.recommender import fit, load_data
from src.model_updates.daily import digest, write_json
from src.model_updates.evaluation import compare
from src.model_updates.snapshot import normalize_events, utc


def split(events):
    interactions = [e for e in normalize_events(events) if e['event_type'] != 'account_created']
    if len(interactions) < 2:
        raise ValueError('Need at least two interactions')
    cutoff = utc(interactions[min(len(interactions) - 1, int(len(interactions) * .8))]['timestamp'])
    train = [e for e in interactions if utc(e['timestamp']) < cutoff]
    test = [e for e in interactions if utc(e['timestamp']) >= cutoff]
    if not train or not test:
        raise ValueError('Need distinct timestamps for a chronological split')
    return train, test, cutoff


def evaluate(data):
    events, users, movies = load_data(data)
    train, test, cutoff = split(events)
    with threadpool_limits(limits=1):
        model = fit(train, users, movies, components=32)
        popularity = dict(model, ui={}, seen={})
        # No timestamped pre-cutoff LLM profiles exist in this fixed dataset.
        # Low-history users therefore use the production popularity fallback.
        result = compare(model, popularity, train, test, [], min_users=100)
    return dict(
        measured_at_utc=datetime.now(UTC).isoformat(),
        dataset_sha256={p.name: digest(p) for p in sorted(data.glob('*.csv.gz'))},
        implementation_sha256={str(p.relative_to(Path('src'))): digest(p) for p in
                               sorted(Path('src').rglob('*.py'))},
        split=dict(cutoff=cutoff.isoformat(), train_events=len(train), test_events=len(test),
                   train_fraction=len(train)/(len(train)+len(test)),
                   policy='Global event-time 80th percentile; equal boundary timestamps all in test.'),
        groups={key: dict(users=value['users'], model=value['champion'], popularity=value['candidate'])
                for key, value in result['groups'].items()},
        excluded_positive_pairs=result['excluded_positive_pairs'],
        settings=dict(k=20, positive_rating_at_least=7, min_svd_distinct_movies=10,
                      factors=32, seed=42, numerical_threads=1),
        limitations=['Fixed metadata snapshot includes the full provided catalog and user IDs.',
                     'No dated historical LLM profiles; cold cohort evaluates popularity fallback only.',
                     'No hyperparameter tuning or separate validation set; test scores were not used to select settings.',
                     'Observed positives are sparse and exposure-biased; offline scores are not causal online effects.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=Path('planning/Milestone1/modelTesting/data'))
    parser.add_argument('--output', type=Path, default=Path('planning/Milestone 2/offline_results.json'))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = evaluate(args.data)
    write_json(args.output, result)
    print(result['split'])
    print(result['groups'])


if __name__ == '__main__':
    main()
