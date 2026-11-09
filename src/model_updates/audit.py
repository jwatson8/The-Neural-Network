"""Persist aggregate daily evidence in Nathan's append-only model_logs table."""
from uuid import NAMESPACE_URL, uuid5

from src.database.interactions import model_logs
from src.model_updates.policy import POLICY
from src.model_updates.snapshot import utc


def monitoring_context(snapshot):
    charges = snapshot.get('licensing_costs', [])
    buckets = sorted(snapshot.get('monitoring_runs', []), key=lambda r: r['window_start'])
    overlap = any(utc(a['window_end']) > utc(b['window_start']) for a, b in zip(buckets, buckets[1:]))
    attributed = sum(b['attributed_rating_count'] for b in buckets)
    positive = sum(b['positive_rating_count'] for b in buckets)
    priced = [float(c['license_cost']) for c in charges if c['license_cost'] is not None]
    return {
        'licensing_records': len(charges), 'unpriced_records': len(charges) - len(priced),
        'known_license_cost_usd': sum(priced) if priced else None,
        'monitoring_buckets': len(buckets), 'overlapping_buckets': overlap,
        'attributed_ratings': attributed if not overlap else None,
        'positive_rating_fraction': positive / attributed if attributed and not overlap else None,
        'note': 'Observed production context only, not candidate scores. Empty tables mean missing evidence; buckets may not cover the full day.',
    }


def record(report, phase, outcome):
    """Deterministic IDs make retries append-once; no raw user records are copied."""
    if phase not in ('comparison', 'deployment'):
        raise ValueError('Unknown audit phase')
    identity = ':'.join(('daily-update', report['day'], report.get('champion_sha256', ''),
                         report.get('candidate_sha256', ''), phase, outcome))
    log_id = uuid5(NAMESPACE_URL, identity)
    payload = {key: report[key] for key in (
        'day', 'status', 'test_start', 'test_end', 'champion_sha256', 'candidate_sha256',
        'snapshot_sha256', 'source_sha256', 'counts', 'comparison', 'drift', 'reasons',
        'reason', 'note', 'error_type', 'candidate_training_seconds', 'candidate_bytes',
        'monitoring_context', 'production') if key in report}
    payload.update(policy=POLICY, outcome=outcome,
                   score_scope='Evaluation candidate; deployed artifact is a separate refit through the test day.')
    existing = model_logs.get(log_id)
    if existing is not None:
        if existing.test_result != payload:
            raise ValueError('Existing audit evidence differs; refusing overwrite')
        return existing
    return model_logs.create_test(model_name=report.get('model_version', 'daily-' + report['day']),
                                  test_type='daily_' + phase + '_v1', test_result=payload, log_id=log_id)
