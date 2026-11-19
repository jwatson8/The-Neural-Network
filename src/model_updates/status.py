"""Print aggregate model-update evidence; no keys, user profiles or raw events."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', type=Path, default=Path('/artifacts'))
    args = parser.parse_args()
    root = args.artifacts
    from sqlalchemy import func
    from sqlmodel import Session, select
    from src.database.database import get_engine
    from src.database.models import StreamEvent, User, Movie
    with Session(get_engine()) as session:
        counts = {model.__tablename__: session.exec(select(func.count()).select_from(model)).one()
                  for model in (StreamEvent, User, Movie)}
        latest = session.exec(select(func.max(StreamEvent.event_time))).one()
    print(json.dumps({'database_counts': counts, 'latest_event_at': str(latest)}))
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
