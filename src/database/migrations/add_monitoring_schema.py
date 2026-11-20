"""Apply the monitoring latency and drift-placeholder schema migration."""

import argparse
from pathlib import Path

from sqlalchemy import Engine, inspect

SQL_PATH = Path(__file__).with_suffix(".sql")


def migrate(engine: Engine, *, bootstrap_if_empty: bool = False) -> None:
    """Add monitoring aggregates while preserving existing monitoring rows."""
    if engine.dialect.name != "postgresql":
        raise ValueError("Monitoring schema migration requires PostgreSQL")
    statements = SQL_PATH.read_text(encoding="utf-8").split("\n-- next\n")
    with engine.begin() as connection:
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
    parser.add_argument("--apply", action="store_true", required=True)
    parser.add_argument("--bootstrap-if-empty", action="store_true")
    args = parser.parse_args()
    from src.database.database import get_engine

    migrate(get_engine(), bootstrap_if_empty=args.bootstrap_if_empty)


if __name__ == "__main__":
    main()
