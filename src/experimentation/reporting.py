"""User-level experiment reports with explicit completeness and attribution limits."""

import hashlib
import json
import math
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from src.database.interactions import experiment_reports
from src.database.interactions import experiment_data
from src.database.schemas import ExperimentReportCreate
from src.experimentation.outcomes import build_outcomes
from src.experimentation.report_types import CoverageEvidence, stored_utc
from src.experimentation.statistics import bootstrap_difference, participation, welch

ANALYSIS_VERSION = 'assignment-user-outcomes-v1'


def _json(value):
    if isinstance(value, datetime):
        return stored_utc(value).isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


def _fingerprint(snapshot, coverage, seed, repetitions, status):
    def rows(values):
        return sorted([_json(value.model_dump()) for value in values], key=lambda item: json.dumps(item, sort_keys=True))
    data = dict(experiment=_json(snapshot.experiment.model_dump()), window=_json([snapshot.window.start, snapshot.window.end]),
                assignments=rows(snapshot.assignments), events=rows(snapshot.events), charges=rows(snapshot.charges),
                serves=rows(snapshot.serves), overlaps=sorted([rows(pair) for pair in snapshot.overlapping_assignments], key=lambda pair: json.dumps(pair, sort_keys=True)),
                coverage=_json(dict(through=coverage.complete_through, reference=coverage.reference)) if coverage else None,
                seed=seed, repetitions=repetitions, status=status, analysis_version=ANALYSIS_VERSION)
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def report_from_snapshot(snapshot, *, coverage: CoverageEvidence | None = None, seed=42, repetitions=2000):
    """Pure aggregation/inference over persisted source rows; does not write reports."""
    if coverage is not None and coverage.complete_through > snapshot.as_of:
        raise ValueError('coverage watermark cannot exceed snapshot time')
    users, quality, other_experiments, covered = build_outcomes(snapshot, coverage=coverage)
    groups = {variant: [user for user in users if user['variant'] == variant] for variant in ('control', 'treatment')}
    cost, ratings, engagement, populations, exposures = {}, {}, {}, {}, {}
    for variant, rows in groups.items():
        count = len(rows)
        known = sum((row['known_cost'] for row in rows), Decimal(0))
        complete = bool(rows) and all(row['actual_cost'] is not None for row in rows)
        rating_means = [row['rating_mean'] for row in rows if row['rating_mean'] is not None]
        hours = [(row['end']-row['start']).total_seconds()/3600 for row in rows]
        populations[variant] = dict(assigned_users=count, observation_hours_mean=sum(hours)/count if count else None,
                                   observation_hours_min=min(hours) if hours else None, observation_hours_max=max(hours) if hours else None)
        exposures[variant] = dict(exposed_users=sum(row['exposure_count'] > 0 for row in rows),
                                 successful_serves=sum(row['exposure_count'] for row in rows),
                                 unlinked_normal_or_fallback_serves=sum(row['unlinked_serve_count'] for row in rows),
                                 actual_models=[dict(route=route, version=version) for route, version in sorted(set().union(*(row['models'] for row in rows)))])
        cost[variant] = dict(assigned_users=count, exposed_users=exposures[variant]['exposed_users'],
            watch_events=sum(row['watch_count'] for row in rows), recorded_charge_records=sum(row['charge_count'] for row in rows),
            known_charge_records=sum(row['known_charge_count'] for row in rows), unknown_charge_records=sum(row['unknown_charge_count'] for row in rows),
            watch_events_without_charge=sum(row['watches_without_charge'] for row in rows),
            uncharged_watches_consistent_with_grace=sum(row['grace_watch_count'] for row in rows),
            unexplained_missing_charge_watches=sum(row['missing_expected_charges'] for row in rows),
            total_known_recorded_cost=str(known), observed_known_cost_per_assigned_user=str(known/count) if count else None,
            observed_zero_known_cost_users=sum(row['known_cost'] == 0 for row in rows),
            confirmed_zero_cost_users=sum(row['confirmed_zero_cost'] for row in rows),
            no_watch_users=sum(row['watch_count'] == 0 for row in rows),
            users_with_complete_actual_cost=sum(row['actual_cost'] is not None for row in rows),
            users_with_unidentified_actual_cost=sum(row['actual_cost'] is None for row in rows), complete_actual_cost=complete,
            actual_cost_per_assigned_user=str(known/count) if complete else None)
        ratings[variant] = dict(assigned_users=count, users_with_ratings=len(rating_means),
            users_without_ratings=count-len(rating_means), valid_rating_events=sum(row['rating_count'] for row in rows),
            observed_rating_participation=len(rating_means)/count if count else None,
            mean_user_rating=sum(rating_means)/len(rating_means) if rating_means else None)
        engagement[variant] = dict(assigned_users=count, users_with_watch=sum(row['watch_count'] > 0 for row in rows),
            watch_events=sum(row['watch_count'] for row in rows),
            observed_watch_participation=sum(row['watch_count'] > 0 for row in rows)/count if count else None,
            watch_events_per_assigned_user=sum(row['watch_count'] for row in rows)/count if count else None)
    c, t = groups['control'], groups['treatment']
    full_cost = cost['control']['complete_actual_cost'] and cost['treatment']['complete_actual_cost']
    tests = dict(
        primary_actual_cost=welch([row['actual_cost'] for row in c], [row['actual_cost'] for row in t]) if full_cost else
            dict(status='unidentified_incomplete_cost_measurement', method='No primary actual-cost test without all-user completeness'),
        observed_known_cost_lower_bound_exploratory=welch([row['known_cost'] for row in c], [row['known_cost'] for row in t]),
        observed_known_cost_bootstrap_exploratory=bootstrap_difference([row['known_cost'] for row in c], [row['known_cost'] for row in t], seed=seed, repetitions=repetitions),
        conditional_rater_mean=welch([row['rating_mean'] for row in c], [row['rating_mean'] for row in t]),
        rating_participation=participation([row['rating_count'] > 0 for row in c], [row['rating_count'] > 0 for row in t]),
        watch_participation=participation([row['watch_count'] > 0 for row in c], [row['watch_count'] > 0 for row in t]),
        watch_count_exploratory=welch([row['watch_count'] for row in c], [row['watch_count'] for row in t]),
    )
    if full_cost:
        tests['primary_actual_cost_bootstrap_robustness'] = bootstrap_difference(
            [row['actual_cost'] for row in c], [row['actual_cost'] for row in t], seed=seed, repetitions=repetitions)
    experiment = snapshot.experiment
    meaningful_events = sum(row['watch_count']+row['rating_count']+row['charge_count'] for row in users)
    closed = (experiment.status == 'completed' and experiment.ends_at is not None
              and stored_utc(experiment.ends_at) <= snapshot.as_of)
    clean = not any(quality.get(key, 0) for key in (
        'invalid_charge_source_links', 'conflicting_charge_sources', 'invalid_rating_events',
        'out_of_order_watch_pairs', 'charges_inside_expected_grace', 'possible_interference_users', 'possible_replayed_rating_payloads'))
    if min(len(c), len(t)) < 2 or meaningful_events == 0:
        status = 'insufficient_data'
    elif (full_cost and closed and clean and experiment.starts_at is not None
          and tests['primary_actual_cost']['status'] == 'ok' and tests['conditional_rater_mean']['status'] == 'ok'):
        status = 'final'
    else:
        status = 'provisional'
    warnings = []
    if not covered:
        warnings.append('No verified complete ingestion/billing evidence: known cost is an observed ledger lower-bound, not complete operating cost.')
    if not full_cost:
        warnings.append('Primary all-assigned-user actual-cost effect is unidentified; unknown users/amounts are not zero-imputed or dropped.')
    if not closed:
        warnings.append('Experiment is not completed with a known elapsed end; report cannot be final.')
    if not clean:
        warnings.append('Integrity, replay, billing-order or concurrent-experiment diagnostics require review.')
    if experiment.starts_at is None:
        warnings.append('Experiment start is unspecified; observation starts use assignment and requested window only.')
    result = dict(
        analysis_version=ANALYSIS_VERSION, generated_at=snapshot.as_of.isoformat(), status=status,
        data_origin='synthetic_test_fixture' if experiment.config.get('data_origin') == 'synthetic_test_fixture' else 'persisted_database_unclassified',
        experiment_metadata=dict(experiment_id=str(experiment.experiment_id), name=experiment.name, status=experiment.status,
            starts_at=_json(experiment.starts_at), ends_at=_json(experiment.ends_at),
            control_policy=experiment.control_policy, control_version=experiment.control_version,
            treatment_policy=experiment.treatment_policy, treatment_version=experiment.treatment_version),
        analysis_window=dict(start=snapshot.window.start.isoformat(), end=snapshot.window.end.isoformat(),
            effective_start=snapshot.effective_start.isoformat(), effective_end=snapshot.effective_end.isoformat(),
            source_as_of=snapshot.as_of.isoformat(), boundary='[start,end)', observation_start='max(assignment, window start, experiment start)'),
        population=populations, exposure_diagnostics=exposures, licensing_cost=cost, user_satisfaction=ratings, engagement=engagement,
        statistical_tests=tests, data_quality=dict(counters=quality, source_counts=snapshot.source_counts,
            other_experiment_ids=other_experiments, coverage_verified=covered,
            completeness_evidence=_json(dict(complete_through=coverage.complete_through, reference=coverage.reference,
                                            external_operator_attestation=True)) if coverage else None, warnings=warnings),
        methodology=dict(randomization_unit='user', primary='Actual licensing charge sum per assigned user during their observation interval.',
            conditional_satisfaction='Equal-weight mean of user rating means among observed raters; not all-user causal satisfaction.',
            confidence_level=.95, bootstrap_seed=seed, bootstrap_repetitions=repetitions,
            monetary_units='Recorded licensing_costs units; currency is not encoded. LLM costs excluded.',
            cost_lower_bound='Sum of known, linked nonnegative ledger charges; not imputation of unknown amounts.',
            zero_cost='Confirmed only with external complete ingestion/ordered billing evidence and no unresolved user anomalies.',
            watch_unit='Distinct watch event UUID; often a minute request, not an independent viewing session.',
            rating_rule='All valid 1..10 post-assignment rating events, deduplicated by UUID; user means get equal weight.',
            multiple_testing='Cost is primary. Ratings/participation/engagement and bootstrap are secondary/exploratory; no multiplicity adjustment or repeated-look stopping rule is implied.'),
        limitations=[
            'Kafka outcomes have no serve/assignment ID. Assignment-based outcomes include organic watches; no direct recommendation-to-watch causality is claimed.',
            'Rater mean conditions on a post-randomization choice; missing ratings are not zero and selection may differ by variant.',
            'No rows is not proof of no activity without completeness evidence. External watermarks are attestations, not inferred from these tables.',
            'Charge grace classification assumes ordered ingestion of the existing 24-hour inactivity rule; out-of-order events can affect materialization.',
            'Assignments do not store eligibility snapshots or lifecycle history; policy/ratio changes and concurrent experiments can compromise interpretation.',
            'Observation durations vary with enrollment; means are for these intervals, not equal fixed follow-up. Review duration imbalance.',
            'Serve FK is application-attested server completion, not client acknowledgment; null links cannot distinguish fallback from every other normal serve.',
            'Nonsignificance is not equivalence. Small samples, skew, serial monitoring and multiple tests require a preregistered live analysis plan.',
        ],
    )
    result = _json(result)
    result['source_fingerprint'] = _fingerprint(snapshot, coverage, seed, repetitions, status)
    json.dumps(result, allow_nan=False)
    return result


def generate_report(*, experiment_id, window_start, window_end, persist=False, coverage=None,
                    seed=42, repetitions=2000, as_of=None):
    """Load real persisted source data; preview by default, append idempotent revisions explicitly."""
    if type(seed) is not int or seed < 0 or type(repetitions) is not int or not 100 <= repetitions <= 5000:
        raise ValueError('seed must be nonnegative and repetitions must be 100..5000')
    snapshot = experiment_data.load(experiment_id=experiment_id, window_start=window_start,
                                    window_end=window_end, as_of=as_of)
    result = report_from_snapshot(snapshot, coverage=coverage, seed=seed, repetitions=repetitions)
    if persist:
        record, created = experiment_reports.save_revision(ExperimentReportCreate(
            experiment_id=experiment_id, generated_at=snapshot.as_of, window_start=snapshot.window.start,
            window_end=snapshot.window.end, status=result['status'], results=result, analysis_version=ANALYSIS_VERSION))
        # Retries return the immutable persisted revision, including its original generation time.
        result = dict(record.results, report_id=str(record.report_id), persistence_created=created)
    return result
