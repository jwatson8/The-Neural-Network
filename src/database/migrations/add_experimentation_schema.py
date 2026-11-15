"""Apply the experiment schema PostgreSQL DDL atomically and idempotently."""

import argparse
from pathlib import Path

from sqlalchemy import Engine, inspect

SQL_PATH = Path(__file__).with_suffix(".sql")


def migrate(engine: Engine, *, bootstrap_if_empty: bool = False) -> None:
    """Add experiment persistence and a nullable serve FK; preserve existing rows.

    Requires existing users/recommendation_serves unless bootstrap_if_empty is set.
    The Compose migration job can bootstrap an empty schema under the same lock.
    Uses a transaction, advisory lock and bounded lock/statement timeouts.
    Does not enable experiments.
    """
    if engine.dialect.name != "postgresql":
        raise ValueError("Experiment schema migration requires PostgreSQL")
    statements = SQL_PATH.read_text(encoding="utf-8").split("\n-- next\n")
    with engine.begin() as connection:
        # The frozen SQL begins with two timeouts and the advisory lock. Acquire
        # them before optional fresh-schema bootstrap as well as versioned DDL.
        for statement in statements[:3]:
            connection.exec_driver_sql(statement)
        if bootstrap_if_empty and not inspect(connection).get_table_names():
            from sqlmodel import SQLModel
            from src.database import models  # noqa: F401 - registers metadata

            SQLModel.metadata.create_all(connection)
        for statement in statements[3:]:
            connection.exec_driver_sql(statement)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", required=True,
                        help="Explicitly apply to the database selected by DATABASE_URL / DATABASE_*.")
    parser.add_argument("--bootstrap-if-empty", action="store_true",
                        help="Create a fresh schema only if empty (used by the Compose migration job).")
    args = parser.parse_args()
    from src.database.database import get_engine

    migrate(get_engine(), bootstrap_if_empty=args.bootstrap_if_empty)


if __name__ == "__main__":
    main()
