"""Stage an approved refit atomically; shell wrapper restarts API or rolls back."""
import argparse
from datetime import date
import json
from pathlib import Path
import shutil

import joblib

from src.model_updates.daily import check_model, digest, write_json


def replace(source, destination):
    temp = destination.with_suffix('.promoting')
    shutil.copyfile(source, temp)
    temp.replace(destination)


def stage(artifacts, day):
    artifacts = Path(artifacts)
    directory = artifacts / 'daily' / day.isoformat()
    report = json.loads((directory / 'report.json').read_text())
    live = artifacts / 'model.joblib'
    if not report.get('eligible_for_manual_promotion') or report['status'] != 'awaiting_approval':
        raise ValueError('Report is not eligible for promotion')
    if (artifacts / 'promotion-pending.json').exists():
        raise ValueError('Resolve pending promotion or rollback first')
    if digest(live) != report['champion_sha256']:
        raise ValueError('Production model changed since evaluation; rerun comparison')
    candidate = directory / 'candidate.joblib'
    if digest(candidate) != report['candidate_sha256']:
        raise ValueError('Candidate checksum does not match report')
    model = joblib.load(candidate)
    check_model(model)
    if model['update_metadata']['version'] != report['model_version']:
        raise ValueError('Candidate version mismatch')
    replace(live, directory / 'previous.joblib')
    write_json(artifacts / 'promotion-pending.json', {'day': day.isoformat(),
               'previous_sha256': digest(live), 'candidate_sha256': digest(candidate)})
    replace(candidate, live)


def finish(artifacts, day, rollback=False):
    artifacts = Path(artifacts)
    pending = artifacts / 'promotion-pending.json'
    directory = artifacts / 'daily' / day.isoformat()
    if pending.exists():
        transaction = json.loads(pending.read_text())
    elif rollback:
        transaction = json.loads((directory / 'promotion.json').read_text())
        if transaction['status'] != 'promoted':
            raise ValueError('No promoted model to roll back')
        transaction.pop('status')
    else:
        raise ValueError('No pending promotion')
    if transaction['day'] != day.isoformat():
        raise ValueError('Pending promotion date mismatch')
    live = artifacts / 'model.joblib'
    if digest(live) not in (transaction['candidate_sha256'], transaction['previous_sha256']):
        raise ValueError('Unexpected model change; refusing to overwrite it')
    if rollback:
        backup = directory / 'previous.joblib'
        if digest(backup) != transaction['previous_sha256']:
            raise ValueError('Rollback checksum mismatch')
        replace(backup, live)
    elif digest(live) != transaction['candidate_sha256']:
        raise ValueError('Candidate is not installed')
    write_json(directory / 'promotion.json', {'status': 'rolled_back' if rollback else 'promoted', **transaction})
    pending.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('stage', 'finish', 'rollback'))
    parser.add_argument('day', type=date.fromisoformat)
    parser.add_argument('--artifacts', type=Path, default=Path('/artifacts'))
    args = parser.parse_args()
    if args.action == 'stage':
        stage(args.artifacts, args.day)
    else:
        finish(args.artifacts, args.day, rollback=args.action == 'rollback')


if __name__ == '__main__':
    main()
