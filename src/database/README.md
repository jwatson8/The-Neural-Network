# Recommendation database

This package owns the PostgreSQL persistence layer. Application code should pass
validated objects from `schemas.py` to the table-specific functions in
`interaction.py`; session and transaction ownership remain explicit.

Start PostgreSQL and create the tables from the repository root:

```sh
docker compose up -d database database-init
docker compose ps
```

The default local connection is exposed on port `5432`. Override credentials or
pool settings in `.env` using the variables documented in `.env.example`.

`initialize.py` uses `create_all` only as a bootstrap while the data model is being
designed. Before production schema changes begin, add Alembic migrations and stop
using `create_all` for upgrades; `create_all` never alters existing tables.
