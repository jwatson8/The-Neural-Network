"""Apply the frozen Phase 1 PostgreSQL DDL atomically and idempotently."""

import argparse
from pathlib import Path

from sqlalchemy import Engine

SQL_PATH = Path(__file__).with_suffix(".sql")


def migrate(engine: Engine) -> None:
    """Add experiment persistence and a nullable serve FK; preserve existing rows.

    Requires an existing users/recommendation_serves schema. Uses a transaction,
    advisory lock and bounded lock/statement timeouts. Does not enable experiments.
    """
    if engine.dialect.name != "postgresql":
        raise ValueError("Phase 1 migration requires PostgreSQL")
    statements = SQL_PATH.read_text(encoding="utf-8").split("\n-- next\n")
    with engine.begin() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", required=True,
                        help="Explicitly apply to the database selected by DATABASE_URL / DATABASE_*.")
    parser.parse_args()
    from src.database.database import get_engine

    migrate(get_engine())


if __name__ == "__main__":
    main()
