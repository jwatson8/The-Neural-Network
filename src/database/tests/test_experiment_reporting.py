"""Synthetic fixtures only; isolated SQLite reports and reusable PostgreSQL seed."""

import io
import json
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from src.database.interactions import experiment_data, experiment_reports, stream_events, recommendation_serves
from src.database.interactions._shared import RecordNotFoundError
from src.database.models import User, Movie, Experiment, ExperimentAssignment, ExperimentReport, StreamEvent, LicensingCost
from src.experimentation.outcomes import amount, build_outcomes
from src.experimentation.reporting import generate_report, report_from_snapshot, ANALYSIS_VERSION
from src.experimentation.report_types import CoverageEvidence, ReportWindow
from src.experimentation import report_cli, report_worker


def seed_reporting_database(engine):
    """Reusable, explicitly synthetic seed. No production configuration is read."""
    now = datetime.now(UTC)
    start, end = now-timedelta(days=2), now-timedelta(days=1)
    experiment = Experiment(name='synthetic-report-'+uuid4().hex, status='completed', starts_at=start, ends_at=end,
        control_policy='baseline', control_version='trained-v1', treatment_policy='cost-aware', treatment_version='policy-v1',
        config={'data_origin': 'synthetic_test_fixture'})
    with Session(engine) as session:
        session.add(experiment)
        session.add_all([User(user_id=u) for u in ('c0', 'c1', 't0', 't1', 'late')])
        session.add_all([Movie(movie_id=m, title=m, license_cost=Decimal(price)) for m, price in [('cost4', '4'), ('cost2', '2')]])
        session.commit()
        eid = experiment.experiment_id
        assignments = [ExperimentAssignment(experiment_id=eid, user_id=u, variant=v, assigned_at=start)
            for u, v in [('c0', 'control'), ('c1', 'control'), ('t0', 'treatment'), ('t1', 'treatment')]]
        assignments.append(ExperimentAssignment(experiment_id=eid, user_id='late', variant='treatment', assigned_at=end))
        session.add_all(assignments)
        session.commit()
        ids = {a.user_id: a.assignment_id for a in assignments}
    # Real ingestion materialization writes synthetic actual charges: 4 and 2.
    for u, movie in [('c1', 'cost4'), ('t1', 'cost2')]:
        stream_events.create(user_id=u, event_type='watch', movie_id=movie, event_time=start+timedelta(hours=1))
    for u, rating in [('c0', 6), ('c1', 8), ('t0', 5), ('t1', 7)]:
        stream_events.create(user_id=u, event_type='rating', movie_id='cost4', rating=rating, event_time=start+timedelta(hours=2))
    for _ in range(2):
        recommendation_serves.create(user_id='c1', served_at=start+timedelta(minutes=10),
            request_received_at=start+timedelta(minutes=10), serve_duration_ms=1, model_route='collaborative',
            model_version='trained-v1', recommendations=[{'movie_id': 'cost4'}], experiment_assignment_id=ids['c1'])
    # Treatment-assigned fallback remains Treatment in ITT and is not an exposure.
    recommendation_serves.create(user_id='t1', served_at=start+timedelta(minutes=10),
        request_received_at=start+timedelta(minutes=10), serve_duration_ms=1, model_route='popularity_fallback',
        model_version='pop-v1', recommendations=[{'movie_id': 'cost2'}])
    # Prices now differ from actual recorded charges; reporting must not read them.
    with Session(engine) as session:
        for movie in session.exec(select(Movie)).all():
            movie.license_cost = Decimal('999')
            session.add(movie)
        session.commit()
    return dict(experiment_id=eid, window_start=start, window_end=end, as_of=now+timedelta(minutes=1)), ids


@pytest.fixture
def fixture_data():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    @event.listens_for(engine, 'connect')
    def foreign_keys(connection, record):
        connection.execute('PRAGMA foreign_keys=ON')
    SQLModel.metadata.create_all(engine)
    with patch('src.database.database.get_engine', return_value=engine):
        args, ids = seed_reporting_database(engine)
        yield engine, args, ids
    engine.dispose()


def evidence(args):
    return CoverageEvidence(args['window_end'], 'synthetic-fixture-completeness-only')


def snapshot(args):
    return experiment_data.load(**args)


def test_read_mapping_all_assigned_zero_watch_fallback_and_no_catalog_query(fixture_data):
    engine, args, ids = fixture_data
    statements = []
    listener = lambda conn, cursor, statement, parameters, context, many: statements.append(statement)
    event.listen(engine, 'before_cursor_execute', listener)
    try:
        data = snapshot(args)
    finally:
        event.remove(engine, 'before_cursor_execute', listener)
    assert len(statements) == 6
    assert not any('FROM movies' in query or 'CREATE ' in query for query in statements)
    users, quality, _, covered = build_outcomes(data)
    by_user = {u['user_id']: u for u in users}
    assert set(by_user) == {'c0', 'c1', 't0', 't1'}
    assert by_user['c0']['watch_count'] == 0 and by_user['c0']['actual_cost'] is None
    assert by_user['c1']['known_cost'] == Decimal(4)
    assert by_user['c1']['exposure_count'] == 2 and by_user['c1']['charge_count'] == 1
    assert by_user['t1']['variant'] == 'treatment' and by_user['t1']['exposure_count'] == 0
    assert by_user['t1']['unlinked_serve_count'] == 1 and not covered


def test_report_default_incomplete_observed_cost_and_conditional_rating(fixture_data):
    _, args, _ = fixture_data
    result = generate_report(**args, repetitions=100)
    assert result['analysis_version'] == ANALYSIS_VERSION and result['data_origin'] == 'synthetic_test_fixture'
    assert result['status'] == 'provisional'
    assert result['licensing_cost']['control']['observed_known_cost_per_assigned_user'] == '2.0000000000'
    assert result['licensing_cost']['treatment']['actual_cost_per_assigned_user'] is None
    assert result['statistical_tests']['primary_actual_cost']['status'] == 'unidentified_incomplete_cost_measurement'
    assert result['statistical_tests']['observed_known_cost_lower_bound_exploratory']['difference'] == -1
    assert result['user_satisfaction']['control']['mean_user_rating'] == 7
    assert result['statistical_tests']['conditional_rater_mean']['difference'] == -1
    assert not experiment_reports.list_records()
    json.dumps(result, allow_nan=False)
    assert '"user_id"' not in json.dumps(result)  # no raw user identifiers


def test_external_fixture_coverage_enables_actual_cost_and_final(fixture_data):
    _, args, _ = fixture_data
    result = generate_report(**args, coverage=evidence(args), repetitions=100)
    assert result['status'] == 'final'
    assert result['licensing_cost']['control']['confirmed_zero_cost_users'] == 1
    assert result['statistical_tests']['primary_actual_cost']['difference'] == -1
    assert result['statistical_tests']['primary_actual_cost']['relative_difference'] == -.5
    assert result['statistical_tests']['primary_actual_cost']['control_n'] == 2


def test_running_cannot_be_final(fixture_data):
    engine, args, _ = fixture_data
    with Session(engine) as session:
        record = session.get(Experiment, args['experiment_id'])
        record.status = 'running'
        session.add(record)
        session.commit()
    assert generate_report(**args, coverage=evidence(args), repetitions=100)['status'] == 'provisional'


def test_unknown_actual_charge_not_catalog_imputed(fixture_data):
    engine, args, _ = fixture_data
    with Session(engine) as session:
        charge = session.exec(select(LicensingCost).where(LicensingCost.user_id == 't1')).one()
        charge.license_cost = None
        session.add(charge)
        session.commit()
    result = generate_report(**args, coverage=evidence(args), repetitions=100)
    assert result['licensing_cost']['treatment']['unknown_charge_records'] == 1
    assert result['licensing_cost']['treatment']['actual_cost_per_assigned_user'] is None
    assert result['status'] == 'provisional'


def test_missing_charge_is_not_free_and_no_watch_is_distinct(fixture_data):
    engine, args, _ = fixture_data
    with Session(engine) as session:
        charge = session.exec(select(LicensingCost).where(LicensingCost.user_id == 't1')).one()
        session.delete(charge)
        session.commit()
    result = generate_report(**args, coverage=evidence(args), repetitions=100)
    group = result['licensing_cost']['treatment']
    assert group['unexplained_missing_charge_watches'] == 1 and group['no_watch_users'] == 1
    assert group['actual_cost_per_assigned_user'] is None and group['confirmed_zero_cost_users'] == 1


def test_grace_watch_is_classified_not_fabricated_charge(fixture_data):
    _, args, _ = fixture_data
    stream_events.create(user_id='c1', event_type='watch', movie_id='cost4', event_time=args['window_start']+timedelta(hours=1, minutes=1))
    result = generate_report(**args, coverage=evidence(args), repetitions=100)
    assert result['licensing_cost']['control']['uncharged_watches_consistent_with_grace'] == 1
    assert result['licensing_cost']['control']['total_known_recorded_cost'] == '4.0000000000'


def test_half_open_boundary_and_pre_assignment_events(fixture_data):
    engine, args, _ = fixture_data
    start = args['window_start']
    with Session(engine) as session:
        a = session.exec(select(ExperimentAssignment).where(ExperimentAssignment.user_id == 'c0')).one()
        a.assigned_at = start+timedelta(hours=3)
        session.add(a)
        session.commit()
    stream_events.create(user_id='c0', event_type='rating', movie_id='cost4', rating=9, event_time=args['window_end'])
    stream_events.create(user_id='c0', event_type='rating', movie_id='cost4', rating=4, event_time=start+timedelta(hours=3))
    rows, _, _, _ = build_outcomes(snapshot(args))
    row = next(u for u in rows if u['user_id'] == 'c0')
    assert row['rating_count'] == 1 and row['rating_mean'] == 4


def test_rating_means_equal_user_weight_and_participation(fixture_data):
    engine, args, _ = fixture_data
    with Session(engine) as session:
        rating = session.exec(select(StreamEvent).where(StreamEvent.user_id == 't0')).one()
        session.delete(rating)
        session.commit()
    stream_events.create(user_id='c1', event_type='rating', movie_id='cost2', rating=10,
                         event_time=args['window_start']+timedelta(hours=3))
    result = generate_report(**args, repetitions=100)
    assert result['user_satisfaction']['control']['mean_user_rating'] == 7.5  # (6 + (8+10)/2)/2
    assert result['user_satisfaction']['treatment']['observed_rating_participation'] == .5
    assert result['statistical_tests']['rating_participation']['control_n'] == 2
    assert result['statistical_tests']['conditional_rater_mean']['treatment_n'] == 1


def test_duplicate_source_events_charges_and_serves_not_multiplied(fixture_data):
    _, args, _ = fixture_data
    data = snapshot(args)
    duplicate = replace(data, events=data.events+data.events, charges=data.charges+data.charges, serves=data.serves+data.serves)
    users, quality, _, _ = build_outcomes(duplicate)
    row = next(u for u in users if u['user_id'] == 'c1')
    assert row['known_cost'] == 4 and row['rating_count'] == 1 and row['exposure_count'] == 2
    assert quality['duplicate_event_ids'] == 6 and quality['duplicate_charge_sources'] == 2


@pytest.mark.parametrize('value', [None, 'bad', -1, float('nan'), float('inf'), True])
def test_invalid_cost_amount_stays_unknown(value):
    assert amount(value) is None


def test_invalid_rating_and_invalid_source_link_diagnostics(fixture_data):
    _, args, _ = fixture_data
    data = snapshot(args)
    rating = next(e for e in data.events if e.event_type == 'rating')
    invalid = rating.model_copy(update={'rating': 11})
    charge = data.charges[0].model_copy(update={'source_event_id': uuid4()})
    altered = replace(data, events=tuple(invalid if e.event_id == rating.event_id else e for e in data.events), charges=(charge,))
    _, quality, _, _ = build_outcomes(altered, coverage=evidence(args))
    assert quality['invalid_rating_events'] == 1 and quality['invalid_charge_source_links'] == 1


def test_concurrent_experiment_diagnostic(fixture_data):
    engine, args, _ = fixture_data
    with Session(engine) as session:
        other = Experiment(name='overlap', control_policy='c', control_version='1', treatment_policy='t', treatment_version='2')
        session.add(other)
        session.commit()
        session.add(ExperimentAssignment(experiment_id=other.experiment_id, user_id='t1', variant='control', assigned_at=args['window_start']))
        session.commit()
    result = generate_report(**args, coverage=evidence(args), repetitions=100)
    assert result['data_quality']['counters']['possible_interference_users'] == 1
    assert result['status'] == 'provisional'
    assert result['population']['treatment']['assigned_users'] == 2


def test_empty_population_no_fabricated_results(fixture_data):
    _, args, _ = fixture_data
    data = replace(snapshot(args), assignments=(), events=(), charges=(), serves=())
    result = report_from_snapshot(data, repetitions=100)
    assert result['status'] == 'insufficient_data'
    assert result['licensing_cost']['control']['actual_cost_per_assigned_user'] is None
    assert result['statistical_tests']['conditional_rater_mean']['p_value'] is None


def test_all_zero_cost_with_coverage_no_undefined_test(fixture_data):
    _, args, _ = fixture_data
    data = snapshot(args)
    data = replace(data, charges=tuple(c.model_copy(update={'license_cost': Decimal(0)}) for c in data.charges))
    result = report_from_snapshot(data, coverage=evidence(args), repetitions=100)
    assert result['licensing_cost']['control']['actual_cost_per_assigned_user'] == '0'
    assert result['statistical_tests']['primary_actual_cost']['p_value'] is None
    assert result['statistical_tests']['primary_actual_cost']['relative_difference'] is None


def test_validation_before_database_access():
    with patch('src.database.database.get_engine') as engine:
        with pytest.raises(ValueError):
            experiment_data.load(experiment_id='bad', window_start=datetime.now(UTC), window_end=datetime.now(UTC))
        with pytest.raises(ValueError):
            ReportWindow(datetime(2026, 1, 1), datetime.now(UTC))
        engine.assert_not_called()


def test_invalid_windows_missing_experiment_and_limits(fixture_data):
    _, args, _ = fixture_data
    with pytest.raises(RecordNotFoundError):
        snapshot(dict(args, experiment_id=uuid4()))
    with pytest.raises(ValueError):
        snapshot(dict(args, window_end=args['window_start']))
    with pytest.raises(ValueError):
        snapshot(dict(args, window_end=args['as_of']+timedelta(days=1)))
    with pytest.raises(ValueError, match='row limit'):
        experiment_data.load(**args, row_limit=1)
    with pytest.raises(ValueError):
        generate_report(**args, coverage=CoverageEvidence(args['as_of']+timedelta(days=1), 'bad'), repetitions=100)


def test_report_persistence_idempotent_and_new_source_revision(fixture_data):
    _, args, _ = fixture_data
    first = generate_report(**args, persist=True, repetitions=100)
    second = generate_report(**dict(args, as_of=args['as_of']+timedelta(minutes=1)), persist=True, repetitions=100)
    assert first['report_id'] == second['report_id'] and not second['persistence_created']
    assert second['generated_at'] == first['generated_at']
    stream_events.create(user_id='t0', event_type='rating', movie_id='cost2', rating=8,
                         event_time=args['window_start']+timedelta(hours=4))
    revision = generate_report(**args, persist=True, repetitions=100)
    assert revision['report_id'] != first['report_id']
    assert len(experiment_reports.list_records()) == 2


def test_persistence_failure_rolls_back(fixture_data):
    engine, args, _ = fixture_data
    original = Session.get
    def fail_report_read(session, entity, *positional, **keywords):
        if entity is ExperimentReport:
            raise RuntimeError('simulated post-insert failure')
        return original(session, entity, *positional, **keywords)
    with patch.object(Session, 'get', fail_report_read), pytest.raises(RuntimeError):
        generate_report(**args, persist=True, repetitions=100)
    assert not experiment_reports.list_records()


def cli_arguments(args):
    return ['--experiment-id', str(args['experiment_id']), '--start', args['window_start'].isoformat(),
            '--end', args['window_end'].isoformat(), '--repetitions', '100']


def test_cli_preview_json_and_explicit_persistence(fixture_data):
    _, args, _ = fixture_data
    output = io.StringIO()
    with redirect_stdout(output):
        assert report_cli.main(cli_arguments(args)+['--json', '--dry-run']) == 0
    assert json.loads(output.getvalue())['analysis_version'] == ANALYSIS_VERSION
    assert not experiment_reports.list_records()
    with redirect_stdout(io.StringIO()):
        assert report_cli.main(cli_arguments(args)+['--persist']) == 0
    assert len(experiment_reports.list_records()) == 1


@pytest.mark.parametrize('argv', [[], ['--experiment-id', 'bad'], ['--experiment-id', str(uuid4()), '--start', '2026-01-01', '--end', '2026-01-02Z']])
def test_cli_invalid_arguments(argv):
    with pytest.raises(SystemExit) as error:
        report_cli.main(argv)
    assert error.value.code == 2


def test_cli_errors_never_expose_credentials(fixture_data):
    _, args, _ = fixture_data
    stderr = io.StringIO()
    with patch.object(report_cli, 'generate_report', side_effect=RuntimeError('postgresql://secret-password')), redirect_stderr(stderr):
        assert report_cli.main(cli_arguments(args)) == 1
    assert 'secret-password' not in stderr.getvalue()


def test_worker_disabled_no_database_and_one_shot_explicit_persistence(fixture_data):
    _, args, _ = fixture_data
    with patch('src.database.database.get_engine') as engine, redirect_stdout(io.StringIO()):
        assert report_worker.main([]) == 0
        engine.assert_not_called()
    with redirect_stdout(io.StringIO()):
        assert report_worker.main(['--enabled']+cli_arguments(args)+['--persist']) == 0
        assert report_worker.main(['--enabled']+cli_arguments(args)+['--persist']) == 0
    assert len(experiment_reports.list_records()) == 1


def test_unfulfilled_or_other_assignment_exposures_not_counted(fixture_data):
    _, args, ids = fixture_data
    data = snapshot(args)
    altered = replace(data, serves=tuple(s.model_copy(update={'experiment_assignment_id': ids['t0']}) for s in data.serves))
    users, quality, _, _ = build_outcomes(altered)
    assert sum(u['exposure_count'] for u in users) == 0
    assert quality['non_target_or_invalid_exposures'] == 3
    empty = replace(data, serves=tuple(s.model_copy(update={'recommendations': []}) for s in data.serves))
    assert sum(u['exposure_count'] for u in build_outcomes(empty)[0]) == 0


def test_conflicting_duplicate_source_not_trusted(fixture_data):
    _, args, _ = fixture_data
    data = snapshot(args)
    conflict = data.charges[0].model_copy(update={'license_cost': Decimal('500')})
    users, quality, _, _ = build_outcomes(replace(data, charges=data.charges+(conflict,)), coverage=evidence(args))
    assert quality['conflicting_charge_sources'] == 1
    assert next(u for u in users if u['user_id'] == conflict.user_id)['actual_cost'] is None


def test_event_window_and_experiment_boundaries_clip_observations(fixture_data):
    _, args, _ = fixture_data
    data = experiment_data.load(**dict(args, window_start=args['window_start']-timedelta(days=5)))
    assert data.effective_start == args['window_start']
    no_overlap = experiment_data.load(**dict(args, window_start=args['window_end'], window_end=args['as_of']))
    assert not no_overlap.assignments
    assert report_from_snapshot(no_overlap, repetitions=100)['status'] == 'insufficient_data'


def test_prior_assignment_charge_excluded_but_grace_lookback_kept(fixture_data):
    engine, args, _ = fixture_data
    with Session(engine) as session:
        assignment = session.exec(select(ExperimentAssignment).where(ExperimentAssignment.user_id == 'c1')).one()
        assignment.assigned_at = args['window_start']+timedelta(hours=1, minutes=30)
        session.add(assignment)
        session.commit()
    stream_events.create(user_id='c1', event_type='watch', movie_id='cost4', event_time=args['window_start']+timedelta(hours=2))
    users, _, _, _ = build_outcomes(snapshot(args), coverage=evidence(args))
    row = next(u for u in users if u['user_id'] == 'c1')
    assert row['known_cost'] == 0 and row['watch_count'] == 1
    assert row['grace_watch_count'] == 1 and row['actual_cost'] == 0


def test_report_cli_invalid_window_and_evidence_pair(fixture_data):
    _, args, _ = fixture_data
    with redirect_stderr(io.StringIO()):
        assert report_cli.main(cli_arguments(dict(args, window_end=args['window_start']))) == 2
        with pytest.raises(SystemExit) as error:
            report_cli.main(cli_arguments(args)+['--coverage-reference', 'unpaired'])
    assert error.value.code == 2


def test_json_export_and_worker_failure_exit(fixture_data, tmp_path):
    _, args, _ = fixture_data
    destination = tmp_path/'synthetic-report.json'
    with redirect_stdout(io.StringIO()):
        assert report_cli.main(cli_arguments(args)+['--output', str(destination)]) == 0
    assert json.loads(destination.read_text())['data_origin'] == 'synthetic_test_fixture'
    with patch.object(report_cli, 'generate_report', side_effect=RuntimeError('synthetic failure')), redirect_stderr(io.StringIO()):
        assert report_worker.main(['--enabled']+cli_arguments(args)) == 1
