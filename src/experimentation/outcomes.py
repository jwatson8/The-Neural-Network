"""Assignment-based per-user outcomes; exposure never changes randomized variant."""

import math
from collections import Counter
from decimal import Decimal, InvalidOperation
from datetime import timedelta

from src.experimentation.report_types import stored_utc


def amount(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and number >= 0 else None


def build_outcomes(snapshot, *, coverage=None):
    """Keep all assigned users and separately identify known, uncertain and zero costs."""
    diagnostics = Counter()
    users, assignment_ids = {}, {}
    for assignment in snapshot.assignments:
        if assignment.assignment_id in assignment_ids:
            diagnostics['duplicate_assignments'] += 1
            continue
        if assignment.user_id in users or assignment.variant not in ('control', 'treatment'):
            raise ValueError('ambiguous experiment assignment population')
        start = max(snapshot.effective_start, stored_utc(assignment.assigned_at))
        if start >= snapshot.effective_end:
            continue
        assignment_ids[assignment.assignment_id] = assignment
        users[assignment.user_id] = dict(
            user_id=assignment.user_id, assignment_id=assignment.assignment_id, variant=assignment.variant,
            start=start, end=snapshot.effective_end, watch_count=0, rating_count=0, ratings=[],
            exposure_count=0, unlinked_serve_count=0, models=set(), known_cost=Decimal(0),
            charge_count=0, known_charge_count=0, unknown_charge_count=0, missing_expected_charges=0,
            grace_watch_count=0, watches_without_charge=0, integrity_issues=0,
        )
    events = {}
    for event in snapshot.events:
        if event.event_id in events:
            diagnostics['duplicate_event_ids'] += 1
            continue
        events[event.event_id] = event
    charges = {}
    for charge in snapshot.charges:
        if charge.source_event_id in charges:
            previous = charges[charge.source_event_id]
            diagnostics['duplicate_charge_sources'] += 1
            if (previous.user_id, previous.movie_id, previous.license_cost, previous.charged_at) != (
                    charge.user_id, charge.movie_id, charge.license_cost, charge.charged_at):
                diagnostics['conflicting_charge_sources'] += 1
                if charge.user_id in users:
                    users[charge.user_id]['integrity_issues'] += 1
                if previous.user_id in users:
                    users[previous.user_id]['integrity_issues'] += 1
            continue
        charges[charge.source_event_id] = charge
    valid_charge_events = set()
    for source_id, charge in charges.items():
        user = users.get(charge.user_id)
        if user is None or not user['start'] <= stored_utc(charge.charged_at) < user['end']:
            continue
        user['charge_count'] += 1
        source = events.get(source_id)
        if (source is None or source.event_type != 'watch' or source.user_id != charge.user_id
                or source.movie_id != charge.movie_id or stored_utc(source.event_time) != stored_utc(charge.charged_at)):
            user['unknown_charge_count'] += 1
            user['integrity_issues'] += 1
            diagnostics['invalid_charge_source_links'] += 1
            continue
        valid_charge_events.add(source_id)
        value = amount(charge.license_cost)
        if value is None:
            user['unknown_charge_count'] += 1
            diagnostics['unknown_charge_amounts'] += 1
        else:
            user['known_cost'] += value
            user['known_charge_count'] += 1
    previous_watches, rating_payloads = {}, set()
    for event in sorted(events.values(), key=lambda item: (stored_utc(item.event_time), str(item.event_id))):
        user = users.get(event.user_id)
        if user is None:
            continue
        when = stored_utc(event.event_time)
        observed = user['start'] <= when < user['end']
        if event.event_type == 'watch':
            key = (event.user_id, event.movie_id)
            previous = previous_watches.get(key)
            grace = previous is not None and when < stored_utc(previous.event_time)+timedelta(hours=24)
            if observed:
                user['watch_count'] += 1
                if previous is not None and stored_utc(event.stored_at) < stored_utc(previous.stored_at):
                    user['integrity_issues'] += 1
                    diagnostics['out_of_order_watch_pairs'] += 1
                if event.event_id not in valid_charge_events:
                    user['watches_without_charge'] += 1
                    if grace:
                        user['grace_watch_count'] += 1
                    else:
                        user['missing_expected_charges'] += 1
                elif grace:
                    user['integrity_issues'] += 1
                    diagnostics['charges_inside_expected_grace'] += 1
            previous_watches[key] = event
        elif event.event_type == 'rating' and observed:
            try:
                rating = float(event.rating)
            except (TypeError, ValueError, OverflowError):
                rating = float('nan')
            if not math.isfinite(rating) or not 1 <= rating <= 10 or isinstance(event.rating, bool):
                diagnostics['invalid_rating_events'] += 1
                user['integrity_issues'] += 1
                continue
            payload = (event.user_id, event.movie_id, when, rating)
            if payload in rating_payloads:
                diagnostics['possible_replayed_rating_payloads'] += 1
            rating_payloads.add(payload)
            user['ratings'].append(rating)
            user['rating_count'] += 1
        if observed and stored_utc(event.stored_at) >= snapshot.window.end:
            diagnostics['events_arrived_after_window_end'] += 1
    seen_serves = set()
    for serve in snapshot.serves:
        if serve.request_id in seen_serves:
            diagnostics['duplicate_serve_ids'] += 1
            continue
        seen_serves.add(serve.request_id)
        user = users.get(serve.user_id)
        if user is None or not user['start'] <= stored_utc(serve.served_at) < user['end']:
            continue
        if serve.experiment_assignment_id is None:
            user['unlinked_serve_count'] += 1
        elif (serve.experiment_assignment_id == user['assignment_id'] and bool(serve.recommendations) and serve.model_route.strip()
              and serve.model_version.strip().lower() not in ('', 'unknown')):
            user['exposure_count'] += 1
            user['models'].add((serve.model_route, serve.model_version))
        else:
            diagnostics['non_target_or_invalid_exposures'] += 1
    interference = set()
    other_experiments = set()
    for assignment, experiment in snapshot.overlapping_assignments:
        user = users.get(assignment.user_id)
        if user is None:
            continue
        start = max(user['start'], stored_utc(assignment.assigned_at),
                    stored_utc(experiment.starts_at) if experiment.starts_at else user['start'])
        end = min(user['end'], stored_utc(experiment.ends_at)) if experiment.ends_at else user['end']
        if start < end:
            interference.add(user['user_id'])
            other_experiments.add(str(experiment.experiment_id))
    coverage_verified = coverage is not None and coverage.complete_through >= snapshot.effective_end
    for user in users.values():
        user['rating_mean'] = sum(user['ratings'])/len(user['ratings']) if user['ratings'] else None
        user['actual_cost'] = (user['known_cost'] if coverage_verified and not user['unknown_charge_count']
                               and not user['missing_expected_charges'] and not user['integrity_issues'] else None)
        user['confirmed_zero_cost'] = user['actual_cost'] == 0 if user['actual_cost'] is not None else False
    diagnostics.update(assigned_users=len(users), possible_interference_users=len(interference),
                       users_with_unexplained_missing_charges=sum(user['missing_expected_charges'] > 0 for user in users.values()))
    return list(users.values()), dict(diagnostics), sorted(other_experiments), coverage_verified
