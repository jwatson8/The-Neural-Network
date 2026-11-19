"""Publish approved model activation instructions and reconcile the champion alias."""
import argparse
from datetime import date
import fcntl
import os
from pathlib import Path
import shutil
from uuid import uuid4

import joblib

from src.model_updates.daily import check_model, digest, write_json
from src.service.model_registry import (
    ACTIVATION_FILE, ACTIVATION_LOCK, artifact_path, model_version, read_record, registration_path, trusted_file,
)


def replace(source, destination):
    temp = destination.with_suffix('.promoting')
    shutil.copyfile(source, temp)
    temp.replace(destination)


def register(root, source, version, sha256, provenance):
    """Called by the trusted writer under the deployment and daily locks."""
    target = artifact_path(root, sha256)
    binding = registration_path(root, version)
    if not source.is_relative_to(root) or source.resolve() != source or not source.is_file():
        raise ValueError('Unexpected registration source')
    if digest(source) != sha256:
        raise ValueError('Artifact checksum mismatch')
    target.parent.mkdir(exist_ok=True)
    if target.parent.resolve() != target.parent:
        raise ValueError('Unexpected model directory')
    if binding.exists():
        record = read_record(binding, root)
        if record['sha256'] != sha256 or record['version'] != version:
            raise ValueError('Model version already bound to different bytes')
    if not target.exists():
        replace(source, target)
    if target.resolve() != target or digest(target) != sha256:
        raise ValueError('Registered artifact checksum mismatch')
    if not binding.exists():
        write_json(binding, dict(version=version, sha256=sha256, provenance=provenance))


def approved_candidate(root, day):
    directory = root / 'daily' / day.isoformat()
    report = read_record(directory / 'report.json', root)
    if not report.get('eligible_for_manual_promotion') or report['status'] != 'awaiting_approval':
        raise ValueError('Report is not eligible for promotion')
    candidate = directory / 'candidate.joblib'
    if candidate.resolve() != candidate or digest(candidate) != report['candidate_sha256']:
        raise ValueError('Candidate checksum does not match report')
    with trusted_file(candidate, root) as stream:
        model = joblib.load(stream)
    check_model(model)
    if model['update_metadata']['version'] != report['model_version']:
        raise ValueError('Candidate version mismatch')
    return candidate, report


def activation(version, sha256, base_sha256):
    return dict(activation_id=uuid4().hex, version=version, sha256=sha256, base_sha256=base_sha256)


def register_champion(root, fallback_version):
    live = root / 'model.joblib'
    sha256 = digest(live)
    with trusted_file(live, root) as stream:
        version = model_version(joblib.load(stream), fallback_version)
    sidecar = live.with_suffix('.json')
    provenance = read_record(sidecar, root) if sidecar.exists() else {}
    if provenance.get('model_sha256') != sha256:
        provenance = {}
    register(root, live, version, sha256, provenance)
    return version, sha256


def publish_activation(root, request):
    fd = os.open(root / ACTIVATION_LOCK, os.O_CREAT | os.O_NOFOLLOW | os.O_RDONLY, 0o644)
    with os.fdopen(fd, 'rb') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        write_json(root / ACTIVATION_FILE, request)


def stage(artifacts, day, fallback_version='m0-svd-v1'):
    root = Path(artifacts).resolve()
    pending = root / 'promotion-pending.json'
    directory = root / 'daily' / day.isoformat()
    if pending.exists():
        raise ValueError('Resolve pending promotion or rollback first')
    if (directory / 'promotion.json').exists():
        raise ValueError('Candidate already has a final promotion outcome')
    candidate, report = approved_candidate(root, day)
    live = root / 'model.joblib'
    previous_sha256 = digest(live)
    if previous_sha256 != report['champion_sha256']:
        raise ValueError('Production model changed since evaluation; rerun comparison')
    manifest = root / ACTIVATION_FILE
    if manifest.exists() and read_record(manifest, root)['sha256'] != previous_sha256:
        raise ValueError('Serving instruction differs from the champion; recover before staging')
    previous_version, _ = register_champion(root, fallback_version)
    register(root, candidate, report['model_version'], report['candidate_sha256'], report)
    replace(live, directory / 'previous.joblib')
    request = activation(report['model_version'], report['candidate_sha256'], previous_sha256)
    transaction = dict(day=day.isoformat(), previous_sha256=previous_sha256,
                       previous_version=previous_version, candidate_sha256=request['sha256'],
                       activation=request)
    # Journal before publication. Recovery can roll back even if publication never happened.
    write_json(pending, transaction)
    publish_activation(root, request)
    return request


def request_rollback(artifacts, day):
    root = Path(artifacts).resolve()
    pending = root / 'promotion-pending.json'
    completed = root / 'daily' / day.isoformat() / 'promotion.json'
    if pending.exists():
        transaction = read_record(pending, root)
    else:
        transaction = read_record(completed, root)
        if transaction.pop('status') not in ('promoted', 'rolled_back'):
            raise ValueError('No promoted model to roll back')
    if transaction['day'] != day.isoformat() or 'activation' not in transaction:
        raise ValueError('Wrong pending date or legacy transaction requires legacy recovery')
    live = digest(root / 'model.joblib')
    if live not in (transaction['previous_sha256'], transaction['candidate_sha256']):
        raise ValueError('Unexpected model change; refusing rollback')
    manifest = root / ACTIVATION_FILE
    current = read_record(manifest, root) if manifest.exists() else None
    if current not in (transaction['activation'], transaction.get('rollback')):
        # A crash before the forward instruction was published left the preceding
        # committed instruction in place. Allow recovery only for this pending date.
        if not pending.exists() or (current and current['sha256'] != transaction['previous_sha256']):
            raise ValueError('A newer activation superseded this transaction')
    previous_sha256 = transaction['previous_sha256']
    if digest(artifact_path(root, previous_sha256)) != previous_sha256:
        raise ValueError('Rollback checksum mismatch')
    if 'rollback' not in transaction:
        transaction['rollback'] = activation(transaction['previous_version'], previous_sha256,
                                             transaction['candidate_sha256'])
    if current != transaction['rollback']:
        write_json(pending, transaction)
        publish_activation(root, transaction['rollback'])
    return transaction['rollback']


def finish(artifacts, day, acknowledgement, expected_sha256, *, rollback=False):
    root = Path(artifacts).resolve()
    pending = root / 'promotion-pending.json'
    completed = root / 'daily' / day.isoformat() / 'promotion.json'
    transaction = read_record(pending if pending.exists() else completed, root)
    request = transaction['rollback' if rollback else 'activation']
    status = 'rolled_back' if rollback else 'promoted'
    if (transaction['day'] != day.isoformat() or request['activation_id'] != acknowledgement
            or request['sha256'] != expected_sha256
            or read_record(root / ACTIVATION_FILE, root) != request
            or (not rollback and 'rollback' in transaction)):
        raise ValueError('Activation acknowledgement does not match the pending transaction')
    if not pending.exists():
        if transaction['status'] != status:
            raise ValueError('Promotion already finalized with a different outcome')
        return
    live = root / 'model.joblib'
    if digest(live) not in (transaction['previous_sha256'], transaction['candidate_sha256']):
        raise ValueError('Unexpected model change; refusing to replace the champion')
    source = artifact_path(root, request['sha256'])
    if digest(source) != request['sha256']:
        raise ValueError('Activated artifact checksum mismatch')
    if digest(live) != request['sha256']:
        replace(source, live)
    write_json(completed, {'status': status, **transaction})
    pending.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('stage', 'finish', 'rollback', 'finish-rollback', 'register', 'register-champion'))
    parser.add_argument('day', type=date.fromisoformat)
    parser.add_argument('acknowledgement', nargs='?')
    parser.add_argument('expected_sha256', nargs='?')
    parser.add_argument('--artifacts', type=Path, default=Path('/artifacts'))
    parser.add_argument('--bootstrap-version', default=os.getenv('MODEL_VERSION', 'm0-svd-v1'))
    args = parser.parse_args()
    if args.action == 'stage':
        request = stage(args.artifacts, args.day, args.bootstrap_version)
    elif args.action == 'rollback':
        request = request_rollback(args.artifacts, args.day)
    elif args.action == 'register-champion':
        register_champion(args.artifacts.resolve(), args.bootstrap_version)
        return
    elif args.action == 'register':
        root = args.artifacts.resolve()
        candidate, report = approved_candidate(root, args.day)
        register(root, candidate, report['model_version'], report['candidate_sha256'], report)
        return
    else:
        finish(args.artifacts, args.day, args.acknowledgement, args.expected_sha256, rollback=args.action == 'finish-rollback')
        return
    print(request['activation_id'], request['sha256'])


if __name__ == '__main__':
    main()
