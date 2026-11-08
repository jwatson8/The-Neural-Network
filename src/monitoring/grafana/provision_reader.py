"""Provision Grafana's restricted PostgreSQL reader role."""

import os

from psycopg import sql

from src.database.database import get_engine

READER_ROLE = "grafana_reader"
READABLE_TABLES = ("monitoring_runs", "data_quality_logs")


def _required_secret(name: str) -> str:
    value = os.getenv(name, "")
    if not value.strip():
        raise RuntimeError(f"Set {name} before enabling monitoring")
    return value


def provision_reader(password: str) -> None:
    """Create or update the least-privileged Grafana database role."""
    engine = get_engine()
    database_name = engine.url.database
    if not database_name:
        raise RuntimeError("The database connection must name a database")

    role = sql.Identifier(READER_ROLE)
    database = sql.Identifier(database_name)
    connection = engine.raw_connection()
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s",
                (READER_ROLE,),
            )
            if cursor.fetchone() is None:
                cursor.execute(sql.SQL("CREATE ROLE {}").format(role))

            cursor.execute(
                sql.SQL(
                    "ALTER ROLE {} WITH LOGIN PASSWORD {} NOINHERIT "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
                ).format(role, sql.Literal(password))
            )
            cursor.execute(
                sql.SQL("ALTER ROLE {} SET default_transaction_read_only TO 'on'").format(
                    role
                )
            )
            cursor.execute(
                sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(
                    database, role
                )
            )
            cursor.execute(
                sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA public FROM {}").format(role)
            )
            cursor.execute(
                sql.SQL(
                    "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {}"
                ).format(role)
            )
            cursor.execute(
                sql.SQL(
                    "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {}"
                ).format(role)
            )
            cursor.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, role)
            )
            cursor.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role)
            )
            tables = sql.SQL(", ").join(
                sql.Identifier("public", table_name)
                for table_name in READABLE_TABLES
            )
            cursor.execute(
                sql.SQL("GRANT SELECT ON TABLE {} TO {}").format(tables, role)
            )
        finally:
            cursor.close()
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    _required_secret("GRAFANA_ADMIN_PASSWORD")
    provision_reader(_required_secret("GRAFANA_DB_PASSWORD"))


if __name__ == "__main__":
    main()
