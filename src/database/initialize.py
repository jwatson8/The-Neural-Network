"""Bootstrap an empty database; verify existing schemas without changing them."""

import argparse

from sqlalchemy import Connection, Engine, inspect, text
from sqlmodel import SQLModel

from src.database import models  # noqa: F401 - registers table metadata
from src.database.database import get_engine

MIGRATION_COMMAND = "python -m src.database.migrations.apply_all --apply"


def verify_schema(bind: Engine | Connection) -> None:
    """Check required tables/columns and experiment links without issuing DDL."""
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    problems = []
    for table in SQLModel.metadata.sorted_tables:
        if table.name not in existing_tables:
            problems.append(f"missing table {table.name}")
            continue
        columns = {column["name"]: column for column in inspector.get_columns(table.name)}
        for name in table.columns.keys():
            if name not in columns:
                problems.append(f"missing column {table.name}.{name}")
        if table.name == "recommendation_serves" and "experiment_assignment_id" in columns:
            if not columns["experiment_assignment_id"]["nullable"]:
                problems.append("recommendation_serves.experiment_assignment_id must be nullable")
            foreign_keys = inspector.get_foreign_keys(table.name)
            if not any(
                fk["constrained_columns"] == ["experiment_assignment_id"]
                and fk["referred_table"] == "experiment_assignments"
                and fk["referred_columns"] == ["assignment_id"]
                for fk in foreign_keys
            ):
                problems.append("missing recommendation experiment assignment foreign key")

    if not problems and bind.dialect.name == "postgresql":
        # An interrupted/manual upgrade can leave a NOT VALID FK in place.
        query = text(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conrelid = 'recommendation_serves'::regclass "
            "AND conname = 'recommendation_serves_experiment_assignment_id_fkey'"
        )
        if isinstance(bind, Engine):
            with bind.connect() as connection:
                validated = connection.execute(query).scalar_one_or_none()
        else:
            validated = bind.execute(query).scalar_one_or_none()
        if validated is not True:
            problems.append("recommendation experiment assignment foreign key is not validated")

    if problems:
        raise RuntimeError(
            "Database schema requires an explicit migration; initialization made no changes. "
            + "; ".join(problems)
            + f". Back up the database, run `{MIGRATION_COMMAND}`, verify the schema, "
            "and then deploy the application. Other missing schema objects require their own migrations."
        )


def create_tables() -> None:
    """Create the complete schema only when empty; otherwise perform read-only checks."""
    engine = get_engine()
    with engine.begin() as connection:
        if inspect(connection).get_table_names():
            verify_schema(connection)
        else:
            SQLModel.metadata.create_all(connection)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify schema without creating or changing it.")
    args = parser.parse_args()
    if args.check:
        verify_schema(get_engine())
    else:
        create_tables()


if __name__ == "__main__":
    main()
