"""Audit and gate automatic promotion. Exit 10 means retain without error."""
import argparse
from datetime import date
import json
from pathlib import Path

from src.model_updates import audit
from src.model_updates.policy import rejection_reasons


def decide(report, artifacts, day):
    directory = Path(artifacts) / 'daily' / day.isoformat()
    if (Path(artifacts) / 'promotion-pending.json').exists():
        raise ValueError('Interrupted promotion requires recovery before proceeding')
    completed = directory / 'promotion.json'
    if completed.exists():
        outcome = json.loads(completed.read_text())['status']
        audit.record(report, 'deployment', outcome)
        return False  # Never re-promote a rolled-back candidate on a retry.
    rejected = rejection_reasons(report.get('comparison', {}), report.get('drift', {}),
                                 report.get('min_users', 100))
    eligible = (report.get('status') == 'awaiting_approval'
                and report.get('eligible_for_manual_promotion') is True and not rejected)
    audit.record(report, 'comparison', 'eligible' if eligible else 'retain_champion')
    if report.get('status') == 'failed':
        raise ValueError('Daily evaluation failed; see saved report')
    return eligible


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('decide', 'record'))
    parser.add_argument('day', type=date.fromisoformat)
    parser.add_argument('--artifacts', type=Path, default=Path('/artifacts'))
    args = parser.parse_args()
    directory = args.artifacts / 'daily' / args.day.isoformat()
    try:
        report = json.loads((directory / 'report.json').read_text())
        if args.action == 'decide':
            eligible = decide(report, args.artifacts, args.day)
            print(json.dumps({'day': str(args.day), 'automatic_decision': 'promote' if eligible else 'retain'}))
            raise SystemExit(0 if eligible else 10)
        outcome = json.loads((directory / 'promotion.json').read_text())['status']
        audit.record(report, 'deployment', outcome)
        print(json.dumps({'day': str(args.day), 'outcome': outcome, 'audit': 'model_logs'}))
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error_type': type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
