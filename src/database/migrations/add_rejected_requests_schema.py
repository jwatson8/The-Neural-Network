"""Add the rejected_requests table for Kafka course-log records the consumer rejects."""

import argparse
from pathlib import Path

from sqlalchemy import Engine

SQL_PATH = Path(__file__).with_suffix(".sql")


def migrate(engine: Engine) -> None:
    """Create rejected_requests and its indexes if absent; idempotent."""
    if engine.dialect.name != "postgresql":
        raise ValueError("Rejected request schema migration requires PostgreSQL")
    statements = SQL_PATH.read_text(encoding="utf-8").split("\n-- next\n")
    with engine.begin() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", required=True)
    parser.parse_args()
    from src.database.database import get_engine

    migrate(get_engine())


if __name__ == "__main__":
    main()
