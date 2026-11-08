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

Run the offline asset check and regressions from the repository root with
Python 3.11+ (standard library only):

```sh
python3 -m scripts.check_dashboard
python3 -m unittest discover -s tests -v
```

The asset check rejects duplicate JSON object keys at every depth, including
identical values and escaped spellings of the same key.
It validates required dashboard/panel/query fields, unique
panel ids (including nested rows), unique target `refId` values within each
query panel, the `logyard-loki` datasource and defined variable references.
Different panels may reuse a target `refId`. Each query panel must have at least
one visible target: `hide` must be a boolean when present and defaults to false
when omitted. Hidden helper targets are allowed alongside a visible target;
text and row panels do not need targets. These checks also apply inside rows.
All panels, including saved children in nested collapsed rows, must have
positive-size integer `gridPos` rectangles with nonnegative coordinates within
Grafana's 24-column grid. Top-level panels cannot overlap; touching edges are
allowed. Row headers occupy that grid, while saved children inside collapsed
rows are excluded from top-level overlap checks and may overlap the row header.
Expanded row children live at the top level and are checked there.
Grafana's built-in annotation datasource is separate from
the Loki query datasource. The check accepts `$name`, `${name:format}` and
`[[name]]` references plus an explicit allowlist of Grafana/Loki globals.
References between variable `query` strings must be acyclic, including no self-references;
formatted references count as dependencies and built-in globals do not.
JSON nesting is limited to 64 container levels, including extension objects and
saved rows: the dashboard root object is level 1, each nested object or array adds
one level, and scalar values add none. Excessive nesting during decoding or
validation fails with exit 1 and a concise diagnostic instead of a traceback.
It checks this asset's structure, not the full Grafana schema or LogQL syntax.
Synthetic fixtures cover invalid assets and dropdown scope without network
access, provisioning or real logs. GitHub Actions runs both commands on every
push and pull request; either failure blocks a green run. There are no separate
lint or type-check gates in this standard-library-only repository.

## Source of Truth

Canonical platform design:

- `ops-meta/specs/2026-03-04_log_aggregation_platform.md`

Live operator runbooks:

- `ops-control/docs/LOGYARD_DEPLOY_MACMINI.md`
- `ops-control/docs/LOGYARD_VECTOR_MACMINI.md`
- `ops-control/docs/LOGYARD_NYXMON_CHECKS.md`
- `ops-control/docs/LOGYARD_INGRESS_MACMINI.md`
