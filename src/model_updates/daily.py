"""Run once: freeze data, compare on yesterday, and prepare a manually promotable refit."""
import argparse
from datetime import UTC, date, datetime, timedelta
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from zoneinfo import ZoneInfo

import joblib
import numpy as np

from src.model.recommender import fit, load_data
from src.model.prepare import DEFAULT_DATA
from src.model_updates.snapshot import capture, normalize_events, utc, window
from src.model_updates.evaluation import compare, production_report
from src.service.model_adapter import validate_model, recommend
from src.data_quality.drift import detect_event_type_drift, detect_rating_drift
from src.data_quality.quality_logging import log_drift_result
from src.model_updates.policy import rejection_reasons, POLICY
from src.model_updates.audit import monitoring_context


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, value):
    path = Path(path)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n')
    temp.replace(path)


def model_cutoff(model, model_path, original, data_dir=DEFAULT_DATA):
    """Require provenance, including a narrowly verified legacy M0 migration path."""
    metadata = model.get('update_metadata')
    if metadata:
        return utc(metadata['training_end_exclusive'])
    sidecar = Path(model_path).with_suffix('.json')
    if sidecar.is_file():
        meta = json.loads(sidecar.read_text())
        expected = json.loads(Path(__file__).parents[1].joinpath('model/data_manifest.json').read_text())
        if (meta.get('model_sha256') == digest(model_path)
                and meta.get('data_sha256') == {name: entry['sha256'] for name, entry in expected.items()}
                and all(digest(Path(data_dir) / name) == entry['sha256'] for name, entry in expected.items())):
            return max(utc(e['timestamp']) for e in original) + timedelta(microseconds=1)
    raise ValueError('Champion training provenance missing. Supply its matching model.json from training; do not guess a cutoff.')


def check_model(model):
    validate_model(model)
    for name in ('user_factors', 'movie_factors', 'popularity'):
        if not np.isfinite(model[name]).all():
            raise ValueError(f'Nonfinite model values: {name}')
    for user in list(model['ui'])[:20] + ['daily-update-unknown']:
        ids, _ = recommend(model, user)
        if len(ids) > 20 or len(set(ids)) != len(ids):
            raise ValueError('Invalid recommendation output')
        if any(not m or any(c in m for c in ',\r\n') for m in ids):
            raise ValueError('Movie ID cannot be served as single-line CSV')


def merge_catalog(original, fresh, key):
    rows = {r[key]: dict(r) for r in original}
    rows.update({r[key]: dict(r) for r in fresh})
    result = []
    for identifier in sorted(rows):
        row = rows[identifier]
        for field in ('title', 'genres', 'overview', 'self_description_likes', 'self_description_dislikes'):
            row[field] = row.get(field) or ''
        result.append(row)
    return result


def run(day, artifacts, data_dir=DEFAULT_DATA, snapshot=None, min_users=100):
    artifacts = Path(artifacts)
    run_dir = artifacts / 'daily' / day.isoformat()
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / 'report.json'
    if report_path.exists():
        return json.loads(report_path.read_text())  # Date-keyed idempotence, no silent overwrites.
    run_dir.mkdir(exist_ok=True)
    report = dict(day=day.isoformat(), status='failed', eligible_for_manual_promotion=False)
    try:
        report['stage'] = 'verify_champion_provenance'
        start, end = window(day)
        if datetime.now(UTC) < end + timedelta(hours=1):
            raise ValueError('Wait for the full day and 60-minute attribution tail')
        report.update(test_start=start.isoformat(), test_end=end.isoformat(),
                      min_users=min_users, created_at=datetime.now(UTC).isoformat())
        model_path = artifacts / 'model.joblib'
        # Serialize model reads against code deployment/promotion on the host via the wrapper lock.
        shutil.copyfile(model_path, run_dir / 'champion.joblib')
        report['champion_sha256'] = digest(run_dir / 'champion.joblib')
        champion = joblib.load(run_dir / 'champion.joblib')
        original_events, original_users, original_movies = load_data(Path(data_dir))
        cutoff = model_cutoff(champion, model_path, original_events, data_dir)
        report['champion_training_end_exclusive'] = cutoff.isoformat()
        if cutoff > start:
            raise ValueError('Champion was trained on held-out day; comparison would leak labels')
        if snapshot is None:
            report['stage'] = 'capture_database_snapshot'
            from src.database.database import get_engine
            snapshot = capture(get_engine(), start, end)
        payload = json.dumps(snapshot, sort_keys=True, allow_nan=False).encode()
        report['stage'] = 'validate_snapshot'
        with gzip.open(run_dir / 'snapshot.json.gz', 'wb') as stream:
            stream.write(payload)
        report['snapshot_sha256'] = hashlib.sha256(payload).hexdigest()
        report['source_sha256'] = hashlib.sha256(b''.join(
            p.read_bytes() for p in sorted(Path(__file__).parents[1].rglob('*.py')))).hexdigest()
        report['original_data_sha256'] = {p.name: digest(p) for p in sorted(Path(data_dir).glob('*.csv.gz'))}
        events = normalize_events(original_events + snapshot['events'])
        users = merge_catalog(original_users, snapshot['users'], 'user_id')
        movies = merge_catalog(original_movies, snapshot['movies'], 'movie_id')
        user_ids, movie_ids = {u['user_id'] for u in users}, {m['movie_id'] for m in movies}
        if any(e['user_id'] not in user_ids or (e['movie_id'] and e['movie_id'] not in movie_ids) for e in events):
            raise ValueError('Interactions reference users/movies missing from metadata')
        available = [e for e in snapshot['events']
                     if utc(e['stored_at']) < start]
        history = [e for e in normalize_events(original_events + available) if utc(e['timestamp']) < start]
        heldout = [e for e in events if start <= utc(e['timestamp']) < end]
        fresh = [e for e in normalize_events(snapshot['events']) if start <= utc(e['timestamp']) < end]
        report['production'] = production_report(snapshot, start, end)
        report['monitoring_context'] = monitoring_context(snapshot)
        report['policy'] = POLICY
        report['counts'] = dict(history=len(history), heldout=len(heldout), fresh_day_events=len(fresh))
        if not fresh:
            report.update(status='insufficient_data', reason='No fresh PostgreSQL events for this day; check ingestion.')
        else:
            report['stage'] = 'train_and_compare'
            baseline = [e for e in history if start - timedelta(days=7) <= utc(e['timestamp'])]
            report['drift'] = {
                'events': detect_event_type_drift(baseline, heldout),
                'ratings': detect_rating_drift([e for e in baseline if e['event_type'] == 'rating'],
                                              [e for e in heldout if e['event_type'] == 'rating'])}
            for drift_result in report['drift'].values():
                log_drift_result(drift_result)
            catalog = {m['movie_id'] for m in champion['movies']}
            eval_events = [e for e in history if not e['movie_id'] or e['movie_id'] in catalog]
            # Frozen champion catalog avoids introducing future metadata into the evaluation.
            started = time.perf_counter()
            known_users = {e['user_id'] for e in history} | set(champion['ui'])
            candidate = fit(eval_events, [u for u in users if u['user_id'] in known_users], champion['movies'], components=32)
            report['candidate_training_seconds'] = time.perf_counter() - started
            check_model(candidate)
            joblib.dump(candidate, run_dir / 'evaluation.joblib')
            profiles = [p for p in snapshot['profiles'] if utc(p['updated_at']) < start]
            report['comparison'] = compare(champion, candidate, history, heldout,
                                            profiles, min_users=min_users)
            result = report['comparison']
            reasons = rejection_reasons(result, report['drift'], min_users)
            report.update(status='retain_champion' if reasons else 'awaiting_approval', reasons=reasons)
            if not reasons:
                report['stage'] = 'refit_and_validate'
                final = fit([e for e in events if utc(e['timestamp']) < end], users, movies, components=32)
                final['update_metadata'] = dict(version='daily-' + day.isoformat(),
                                                training_end_exclusive=end.isoformat(),
                                                snapshot_sha256=report['snapshot_sha256'])
                check_model(final)
                joblib.dump(final, run_dir / 'candidate.joblib')
                check_model(joblib.load(run_dir / 'candidate.joblib'))
                report.update(eligible_for_manual_promotion=True,
                              candidate_sha256=digest(run_dir / 'candidate.joblib'),
                              candidate_bytes=(run_dir / 'candidate.joblib').stat().st_size,
                              model_version=final['update_metadata']['version'])
    except Exception as error:
        # No raw input payloads, database credentials, or exception messages in report/logs.
        report.update(status='failed', error_type=type(error).__name__,
                      reason='Daily run failed validation, data access or training; production model unchanged.')
        write_json(report_path, report)
        raise
    report['stage'] = 'complete'
    write_json(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day', type=date.fromisoformat,
                        default=datetime.now(ZoneInfo('America/New_York')).date() - timedelta(days=1))
    parser.add_argument('--artifacts', type=Path, default=Path('/artifacts'))
    parser.add_argument('--data', type=Path, default=Path('/app/data'))
    parser.add_argument('--min-users', type=int, default=100)
    args = parser.parse_args()
    try:
        result = run(args.day, args.artifacts, args.data, min_users=args.min_users)
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error_type': type(error).__name__,
                          'report': str(args.artifacts / 'daily' / str(args.day) / 'report.json')}))
        raise SystemExit(1) from None
    print(json.dumps({'day': result['day'], 'status': result['status'],
                      'eligible_for_manual_promotion': result['eligible_for_manual_promotion']}))


if __name__ == '__main__':
    main()
