"""Create database tables for local development and initial deployment."""

from sqlalchemy import text
from sqlmodel import SQLModel

from src.database import models  # noqa: F401 - registers table metadata
from src.database.database import get_engine


def create_tables() -> None:
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        # create_all creates missing tables but does not widen existing columns.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE movies ALTER COLUMN license_cost "
                    "TYPE NUMERIC(16, 10)"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE licensing_costs ALTER COLUMN license_cost "
                    "TYPE NUMERIC(16, 10)"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE monitoring_runs ALTER COLUMN total_license_cost "
                    "TYPE NUMERIC(16, 10)"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE course_requests ALTER COLUMN offset_id "
                    "TYPE BIGINT"
                )
            )


if __name__ == "__main__":
    create_tables()
