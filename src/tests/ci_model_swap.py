"""Build a clearly synthetic swap fixture ONLY for the isolated Docker CI job.

This tests restart/checksum/rollback mechanics, not candidate model quality.
"""
import os
from pathlib import Path
import joblib
from src.model_updates.daily import digest, write_json


def main():
    if os.getenv('CI_DAILY_SMOKE') != '1':
        raise RuntimeError('This fixture is restricted to the isolated CI smoke deployment')
    artifacts = Path('/artifacts')
    folder = artifacts / 'daily' / '2000-01-01'
    folder.mkdir(parents=True, exist_ok=False)
    model = joblib.load(artifacts / 'model.joblib')
    model['update_metadata'] = dict(version='daily-2000-01-01', training_end_exclusive='2000-01-02T00:00:00Z')
    joblib.dump(model, folder / 'candidate.joblib')
    write_json(folder / 'report.json', dict(day='2000-01-01', status='awaiting_approval',
               eligible_for_manual_promotion=True, model_version='daily-2000-01-01',
               champion_sha256=digest(artifacts / 'model.joblib'),
               candidate_sha256=digest(folder / 'candidate.joblib'),
               comparison=dict(groups={'all': {'users': 100}}, paired_delta_lower_95=.1,
                               candidate_p95_ms=1),
               drift={k: {'status': 'no_drift'} for k in ('events', 'ratings')},
               note='Synthetic CI swap fixture, not a measured model improvement'))


if __name__ == '__main__':
    main()
