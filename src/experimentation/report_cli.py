"""Explicit report preview/persistence; never initializes databases or experiments."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

from src.experimentation.report_types import CoverageEvidence, utc
from src.experimentation.reporting import generate_report


def timestamp(value):
    try:
        return utc(datetime.fromisoformat(value.replace('Z', '+00:00')))
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError('use an ISO-8601 timestamp including timezone') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment-id', type=UUID, required=True)
    parser.add_argument('--start', type=timestamp, required=True)
    parser.add_argument('--end', type=timestamp, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--persist', action='store_true', help='Append an immutable report revision')
    mode.add_argument('--dry-run', action='store_true', help='Preview only (default)')
    parser.add_argument('--json', action='store_true', help='Print structured JSON instead of summary')
    parser.add_argument('--output', type=Path, help='Export JSON to an explicitly selected local path')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--repetitions', type=int, default=2000)
    parser.add_argument('--coverage-through', type=timestamp)
    parser.add_argument('--coverage-reference', help='Nonsecret external completeness/ordered-billing evidence ID')
    args = parser.parse_args(argv)
    if bool(args.coverage_through) != bool(args.coverage_reference):
        parser.error('coverage-through and coverage-reference must be supplied together')
    try:
        coverage = CoverageEvidence(args.coverage_through, args.coverage_reference) if args.coverage_through else None
        result = generate_report(experiment_id=args.experiment_id, window_start=args.start, window_end=args.end,
                                 persist=args.persist, coverage=coverage, seed=args.seed, repetitions=args.repetitions)
        encoded = json.dumps(result, indent=2, allow_nan=False)
        if args.output:
            args.output.write_text(encoded+'\n', encoding='utf-8')
        if args.json:
            print(encoded)
        else:
            print(f"Experiment {args.experiment_id}: {result['status']} ({result['analysis_version']})")
            for variant in ('control', 'treatment'):
                cost = result['licensing_cost'][variant]
                rating = result['user_satisfaction'][variant]
                print(f"{variant}: assigned={cost['assigned_users']}; known ledger total={cost['total_known_recorded_cost']}; "
                      f"complete cost/user={cost['actual_cost_per_assigned_user']}; raters={rating['users_with_ratings']}")
            for warning in result['data_quality']['warnings']:
                print('WARNING: '+warning)
        return 0
    except ValueError:
        print('Invalid report inputs or source row limit exceeded; check UUID, window, evidence and resampling parameters.', file=sys.stderr)
        return 2
    except Exception as error:
        # DB exceptions can contain credential-bearing connection URLs/parameters.
        print(f'Report generation failed ({type(error).__name__}); no result claimed. Inspect authorized local logs.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
