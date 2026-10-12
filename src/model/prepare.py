"""Verify the course snapshot and build a fresh serving artifact."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
from datetime import datetime, timezone
from urllib.request import urlopen
import joblib
from src.model.recommender import fit, load_data

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA = ROOT / 'planning/Milestone1/modelTesting/data'


def prepare(data_dir, model_path, download=False):
    manifest = json.loads(Path(__file__).with_name('data_manifest.json').read_text())
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    for name, entry in manifest.items():
        path = data_dir / name
        if not path.exists() and download:
            with urlopen(entry['url'], timeout=120) as response:
                contents = response.read()
            if hashlib.sha256(contents).hexdigest() != entry['sha256']:
                raise ValueError(f'Download checksum mismatch: {name}')
            path.write_bytes(contents)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError(f'Missing or changed dataset: {path}. --download fetches missing files.')
    model = fit(*load_data(data_dir))
    model_path = Path(model_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    temp = model_path.with_suffix('.tmp')
    joblib.dump(model, temp)
    temp.replace(model_path)
    metadata = dict(trained_at_utc=datetime.now(timezone.utc).isoformat(),
                    python=platform.python_version(), stats=model['stats'],
                    model_sha256=hashlib.sha256(model_path.read_bytes()).hexdigest(),
                    training_source_sha256=hashlib.sha256(Path(__file__).with_name('recommender.py').read_bytes()).hexdigest(),
                    data_sha256={name: item['sha256'] for name, item in manifest.items()})
    model_path.with_suffix('.json').write_text(json.dumps(metadata, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(metadata, indent=2))
    return model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=DEFAULT_DATA)
    parser.add_argument('--model', type=Path, default=ROOT / 'artifacts/model.joblib')
    parser.add_argument('--download', action='store_true')
    args = parser.parse_args()
    prepare(args.data, args.model, args.download)


if __name__ == '__main__':
    main()
