# Logyard

Logyard is the homelab log aggregation platform.

It deliberately stays lean:

- Vector agents on producer hosts
- Loki single-binary runtime on `macmini`
- shared Grafana for search and exploration
- Nyxmon as the alert transport owner

There is no custom ingest app, bespoke search UI, or service-specific database in this repo.

## Operator Model

Production operator flow lives in `ops-control` runbooks. This repo owns the service-facing assets and assumptions:

- Grafana landing dashboard content
- Logyard-specific product documentation
- the canonical repo for deployment-facing assets consumed by `ops-library`

The current production target is a single-server deployment on `macmini`.

## Scope Decisions

These are the implementation defaults this repo expects the deploy logic to enforce:

- retention: `14d`
- source scope: journald first
- labels: low-cardinality and stable
  - required labels: `host`, `service`, `source_type`, `environment`, `level`
- operator UX: Grafana Explore first, plus a minimal provisioned Logyard landing dashboard
- backup scope: Loki log data is intentionally out of backup scope for MVP; Git-managed config and deploy automation are the source of truth

## Repository Layout

```text
deploy/
  grafana/
    dashboards/
      logyard/
        logyard-home.json
docs/
  operational-assumptions.md
```

## Grafana Assets

`deploy/grafana/dashboards/logyard/logyard-home.json` is the Logyard landing dashboard for Grafana.
It is not intended to replace Explore. It gives operators:

- a stable bookmarkable entrypoint for Logyard
- lightweight `host` / `service` / `level` filters for the first triage pass
- live recent-log and error-log panels
- quick volume/status panels for hosts and services
- starter LogQL queries
- the documented scope and retention policy in the UI

All query panels follow the selected `host` and `service`. The `level` filter
also applies to Recent Logs, Log Lines, Active Services, Active Hosts and Top
Services. The error panels always show their fixed error levels, and Log Volume
by Level compares all levels within the selected host/service scope.

Run the offline query regressions with Python 3.11+ (standard library only):

```sh
python3 -m unittest discover -s tests -v
```

## Source of Truth

Canonical platform design:

- `ops-meta/specs/2026-03-04_log_aggregation_platform.md`

Live operator runbooks:

- `ops-control/docs/LOGYARD_DEPLOY_MACMINI.md`
- `ops-control/docs/LOGYARD_VECTOR_MACMINI.md`
- `ops-control/docs/LOGYARD_NYXMON_CHECKS.md`
- `ops-control/docs/LOGYARD_INGRESS_MACMINI.md`
