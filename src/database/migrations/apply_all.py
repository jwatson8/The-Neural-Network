"""Apply repository PostgreSQL migrations in their required order."""

import argparse

from sqlalchemy import Engine

from src.database.migrations import (
    add_experimentation_schema,
    add_monitoring_schema,
    add_rejected_requests_schema,
)


def migrate_all(engine: Engine, *, bootstrap_if_empty: bool = False) -> None:
    """Apply independently owned migrations in deployment order."""
    add_experimentation_schema.migrate(
        engine, bootstrap_if_empty=bootstrap_if_empty
    )
    add_monitoring_schema.migrate(engine)
    add_rejected_requests_schema.migrate(engine)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", required=True)
    parser.add_argument("--bootstrap-if-empty", action="store_true")
    args = parser.parse_args()
    from src.database.database import get_engine

    migrate_all(get_engine(), bootstrap_if_empty=args.bootstrap_if_empty)


if __name__ == "__main__":
    main()
