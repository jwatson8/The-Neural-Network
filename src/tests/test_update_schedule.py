from datetime import datetime, timedelta, timezone

import pytest

from deployment.scheduled_update import due


def test_first_run_waits_for_anchor_and_interval_survives_restart():
    anchor = datetime(2026, 11, 20, 6, tzinfo=timezone.utc)
    assert not due(anchor - timedelta(seconds=1), None, anchor.isoformat(), 24)
    assert due(anchor, None, anchor.isoformat(), 24)
    state = {'started_at': anchor.isoformat(), 'status': 'completed'}
    assert not due(anchor + timedelta(hours=23, minutes=59), state, '', 24)
    assert due(anchor + timedelta(hours=24), state, '', 24)
    # The second run falls within the submission's final 72 hours.
    deadline = datetime(2026, 11, 23, 4, 59, tzinfo=timezone.utc)
    assert deadline - timedelta(hours=72) < anchor < anchor + timedelta(hours=24) < deadline


def test_failures_do_not_trigger_an_hourly_training_storm():
    now = datetime.now(timezone.utc)
    state = {'started_at': now.isoformat(), 'status': 'failed'}
    assert not due(now + timedelta(hours=1), state, '', 24)
    for hours in (0, 23, 49):
        with pytest.raises(ValueError):
            due(now, None, '', hours)
