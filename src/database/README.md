# Recommendation database

This package owns the PostgreSQL persistence layer. Application code passes
validated objects from `schemas.py` to table-specific functions in
`interaction.py`; session and transaction ownership remain explicit. The schema
contains the six recommendation-domain tables plus durable cold-start profiles
and a lease-based cold-start work queue.

Start PostgreSQL and create the tables from the repository root:

```sh
docker compose up -d database database-init
docker compose ps
```

The default local connection is exposed on port `5432`. Override credentials or
pool settings in `.env` using the variables documented in `.env.example`.

Database tests are colocated in `src/database/tests` and can be run with:

```sh
python -m unittest discover -s src/database/tests -v
```

`initialize.py` uses `create_all` only as a pre-production bootstrap. It never
alters existing tables, so reset the disposable local volume after incompatible
schema changes. Before production schema changes begin, add Alembic migrations.
