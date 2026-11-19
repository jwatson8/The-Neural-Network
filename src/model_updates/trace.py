"""Resolve a recorded prediction to immutable model, code, and retained inputs."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
from uuid import UUID

from src.database.interactions import recommendation_serves
from src.service.model_registry import artifact_path, read_record, registration_path


def trace(root, request_id):
    root = Path(root).resolve()
    serve = recommendation_serves.get(request_id)
    if serve is None:
        raise ValueError('Prediction not found')
    record = read_record(registration_path(root, serve.model_version), root)
    model = artifact_path(root, record['sha256'])
    with model.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != record['sha256']:
            raise ValueError('Registered model checksum mismatch')
    provenance = record['provenance']
    snapshot = provenance.get('training_data_snapshot', {})
    verified = []
    for entry in snapshot.get('original_files', {}).values():
        path = (root / entry['path']).resolve()
        if not path.is_relative_to(root):
            raise ValueError('Unexpected retained-data path')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != entry['sha256']:
                raise ValueError('Original training data checksum mismatch')
        verified.append(entry['path'])
    if snapshot.get('database_path'):
        path = (root / snapshot['database_path']).resolve()
        if not path.is_relative_to(root):
            raise ValueError('Unexpected snapshot path')
        with gzip.open(path, 'rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != snapshot['database_sha256']:
                raise ValueError('Database snapshot checksum mismatch')
        verified.append(snapshot['database_path'])
    return dict(request_id=str(request_id), served_at=serve.served_at.isoformat(),
                model_version_id=serve.model_version, model_sha256=record['sha256'],
                git_commit_sha=provenance.get('git_commit_sha'),
                verified_training_files=verified, provenance=provenance)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('request_id', type=UUID)
    parser.add_argument('--artifacts', type=Path, default=Path('/inputs'))
    args = parser.parse_args()
    print(json.dumps(trace(args.artifacts, args.request_id), indent=2))


if __name__ == '__main__':
    main()
