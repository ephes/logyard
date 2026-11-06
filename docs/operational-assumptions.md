# Logyard Operational Assumptions

These assumptions are intentionally small and explicit so the deployment path stays easy to operate.

## Runtime

- Loki runs as a single binary in one container on `macmini`.
- Shared Grafana stays owned by the existing Graphyard deployment.
- Logyard adds a Loki datasource and a small Logyard dashboard into that shared Grafana instance.

## Dashboard Filters

- Every query panel is scoped to the selected `host` and `service`.
- `level` filters Recent Logs, Log Lines, Active Services, Active Hosts and Top Services.
- Errors and Errors and Critical Logs retain the fixed `error|critical|alert|emerg` levels regardless of `level`.
- Log Volume by Level compares all levels within the selected host/service scope.

Dashboard changes must pass the offline asset check and query regressions listed
in the README before adoption. CI checks asset structure and synthetic selector
behavior; it does not provision Grafana or verify live Loki data. Deployment
and live verification remain part of the operator adoption workflow.

## Ingestion

- Vector is the standard producer.
- Journald is the default source.
- File or container log collection is additive and opt-in.

## Labels

The index stays intentionally small:

- `host`
- `service`
- `source_type`
- `environment`
- `level`

Everything else should stay in the log payload unless there is a strong reason to index it.

## Retention

The MVP retention window is `14d`.

That value is chosen to be:

- explicit
- small enough for single-server operation
- long enough for normal homelab debugging workflows

## Backup Scope

Loki data is intentionally disposable in MVP.

What matters is reproducibility:

- repo-managed assets
- deploy logic in `ops-library`
- inventory and operator docs in `ops-control`

If longer-term audit or compliance retention is needed later, that decision should add a new storage and backup design instead of silently treating local Loki data as authoritative backup state.
