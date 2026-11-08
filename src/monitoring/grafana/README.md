# Grafana Monitoring

Set unique values for `GRAFANA_ADMIN_PASSWORD` and `GRAFANA_DB_PASSWORD` in
`.env`, then build and start the monitoring profile:

```sh
docker compose --profile monitoring up -d --build
```

Grafana is bound to the VM's localhost. From your computer, connect to the VM
with port forwarding and leave the session open:

```sh
ssh -L 3000:127.0.0.1:3000 user@vm
```

Open `http://localhost:3000` and sign in as `GRAFANA_ADMIN_USER` (default
`admin`) using `GRAFANA_ADMIN_PASSWORD`. The database reader password is only
used by provisioned services and is never entered in the Grafana UI.

The Monitoring Overview dashboard includes an application table-count panel. It
queries Flask's `GET /database/summary` endpoint over the Compose network through
the provisioned Infinity datasource; it does not expand the PostgreSQL reader's
permissions beyond the monitoring tables.

View service logs with:

```sh
docker compose logs -f monitoring-worker grafana grafana-database-init
```

Stop the monitoring profile without deleting its data:

```sh
docker compose --profile monitoring down
```

Adding `--volumes` also deletes PostgreSQL and Grafana's persistent state and
should only be used when that data is intentionally disposable.
