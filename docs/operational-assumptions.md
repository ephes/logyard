# Logyard Operational Assumptions

These assumptions are intentionally small and explicit so the deployment path stays easy to operate.

## Runtime

- Loki runs as a single binary in one container on `macmini`.
- Shared Grafana stays owned by the existing Graphyard deployment.
- Logyard adds a Loki datasource and a small Logyard dashboard into that shared Grafana instance.

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
