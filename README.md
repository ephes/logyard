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

`deploy/grafana/dashboards/logyard/logyard-home.json` is a small landing dashboard for Grafana.
It is not intended to replace Explore. It gives operators:

- a stable bookmarkable entrypoint for Logyard
- starter LogQL queries
- the documented scope and retention policy in the UI

## Source of Truth

Canonical platform design:

- `ops-meta/specs/2026-03-04_log_aggregation_platform.md`

Live operator runbooks:

- `ops-control/docs/LOGYARD_DEPLOY_MACMINI.md`
- `ops-control/docs/LOGYARD_VECTOR_MACMINI.md`
- `ops-control/docs/LOGYARD_NYXMON_CHECKS.md`
- `ops-control/docs/LOGYARD_INGRESS_MACMINI.md`
