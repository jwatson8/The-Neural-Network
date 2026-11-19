"""Print aggregate model-update evidence; no keys, user profiles or raw events."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', type=Path, default=Path('/artifacts'))
    args = parser.parse_args()
    root = args.artifacts
    for name in ('model.json', 'model-activation.json', 'promotion-pending.json'):
        path = root / name
        if path.exists():
            print(json.dumps({'file': name, 'record': json.loads(path.read_text())}))
    for directory in sorted((root / 'daily').glob('*'))[-7:]:
        for name in ('report.json', 'promotion.json'):
            path = directory / name
            if path.is_file():
                print(json.dumps({'file': str(path.relative_to(root)), 'record': json.loads(path.read_text())}))
        for path in sorted(directory.glob('continuity-*.json'))[-2:]:
            print(json.dumps({'file': str(path.relative_to(root)), 'record': json.loads(path.read_text())}))


if __name__ == '__main__':
    main()
