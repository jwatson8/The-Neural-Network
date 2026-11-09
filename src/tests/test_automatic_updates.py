"""Automatic promotion must retain on weak evidence and audit before mutation."""
import copy
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from src.model_updates import audit
from src.model_updates.automatic import decide
from src.model_updates.policy import rejection_reasons


def eligible_report():
    return dict(day='2026-01-03', status='awaiting_approval', eligible_for_manual_promotion=True,
                champion_sha256='a', candidate_sha256='b', min_users=100,
                comparison=dict(groups={'all': {'users': 100}}, paired_delta_lower_95=.01,
                                candidate_p95_ms=5),
                drift={k: {'status': 'no_drift'} for k in ('events', 'ratings')})


class AutomaticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.day = date(2026, 1, 3)
        self.directory = self.root / 'daily' / str(self.day)
        self.directory.mkdir(parents=True)

    def test_eligible_requires_durable_audit(self):
        with patch.object(audit, 'record') as record:
            self.assertTrue(decide(eligible_report(), self.root, self.day))
            self.assertEqual(record.call_args.args[1:], ('comparison', 'eligible'))
        with patch.object(audit, 'record', side_effect=RuntimeError('database unavailable')):
            with self.assertRaises(RuntimeError):
                decide(eligible_report(), self.root, self.day)

    def test_every_gate_fails_closed_including_nonfinite_and_missing(self):
        report = eligible_report()
        changes = [({}, report['drift']), (report['comparison'], {}),
                   ({**report['comparison'], 'paired_delta_lower_95': float('nan')}, report['drift']),
                   ({**report['comparison'], 'paired_delta_lower_95': 0}, report['drift']),
                   ({**report['comparison'], 'candidate_p95_ms': 600}, report['drift']),
                   ({**report['comparison'], 'candidate_p95_ms': float('nan')}, report['drift']),
                   ({**report['comparison'], 'groups': {'all': {'users': 99}}}, report['drift'])]
        for comparison, drift in changes:
            self.assertTrue(rejection_reasons(comparison, drift, min_users=2))

    def test_report_flag_alone_never_authorizes_promotion(self):
        report = eligible_report()
        report['comparison'] = {}
        with patch.object(audit, 'record'):
            self.assertFalse(decide(report, self.root, self.day))

    def test_success_and_rollback_are_not_repeated_and_pending_blocks(self):
        with patch.object(audit, 'record'):
            for outcome in ('promoted', 'rolled_back'):
                (self.directory / 'promotion.json').write_text(json.dumps({'status': outcome}))
                self.assertFalse(decide(eligible_report(), self.root, self.day))
            (self.root / 'promotion-pending.json').write_text('{}')
            with self.assertRaises(ValueError):
                decide(eligible_report(), self.root, self.day)

    def test_audit_retry_is_idempotent_and_changed_evidence_rejected(self):
        with patch.object(audit.model_logs, 'get', return_value=None), \
             patch.object(audit.model_logs, 'create_test') as create:
            audit.record(eligible_report(), 'comparison', 'eligible')
            args = create.call_args.kwargs
        with patch.object(audit.model_logs, 'get', return_value=SimpleNamespace(test_result=args['test_result'])), \
             patch.object(audit.model_logs, 'create_test') as create:
            audit.record(eligible_report(), 'comparison', 'eligible')
            create.assert_not_called()
            changed = copy.deepcopy(eligible_report())
            changed['comparison']['candidate_p95_ms'] = 20
            with self.assertRaises(ValueError):
                audit.record(changed, 'comparison', 'eligible')

    def test_missing_monitoring_not_zero_cost_or_candidate_accuracy(self):
        context = audit.monitoring_context({})
        self.assertIsNone(context['known_license_cost_usd'])
        self.assertIsNone(context['positive_rating_fraction'])
        bucket = dict(window_start='2026-01-03T00:00:00Z', window_end='2026-01-03T00:05:00Z',
                      attributed_rating_count=10, positive_rating_count=7)
        context = audit.monitoring_context({'monitoring_runs': [bucket, bucket]})
        self.assertTrue(context['overlapping_buckets'])
        self.assertIsNone(context['positive_rating_fraction'])
