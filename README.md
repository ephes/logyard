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
identical values and escaped spellings of the same key. Inputs must be standard
JSON: `NaN`, `Infinity`, `-Infinity` and numbers that overflow to infinity
are rejected, including in extension/annotation options and when calling the
validator directly. Finite numeric values and strings containing these words
remain supported. Inputs must be UTF-8
regular files of at most 1 MiB (1,048,576 bytes), inclusive. Symlinks to regular
files are supported. The checker opens the path once, nonblocking where supported,
checks the descriptor's type and size before reading, and reads at most the byte
limit plus one byte to detect growth after the size check. Nonregular files,
oversized assets and invalid UTF-8 fail with exit 1 and a concise diagnostic.
After reading, the checker compares the same descriptor's device, inode, size,
nanosecond modification time and change time, and requires the consumed byte
count to equal its initial size. Detected changes fail with `dashboard changed
while reading`. The path is never reopened: an atomic replacement before the
initial descriptor check can leave coherent old opened bytes that validate;
later invocations see the new file. A replacement during reading also validates
the old bytes if descriptor metadata stays stable, but is conservatively refused
if it changes the old inode's change time (for example, when unlinking it).
Retry the check on the completed file after a refusal. These metadata checks
detect ordinary concurrent edits; they do not provide an immutable filesystem
snapshot or detect every possible write on filesystems with coarse timestamps.
It validates required dashboard/panel/query fields, unique
panel ids (including nested rows), unique target `refId` values within each
query panel, the `logyard-loki` datasource and defined variable references.
Different panels may reuse a target `refId`. Each query panel must have at least
one visible target: `hide` must be a boolean when present and defaults to false
when omitted. Hidden helper targets are allowed alongside a visible target;
text and row panels do not need targets. These checks also apply inside rows.
Every target must explicitly set `queryType` to `instant` or `range`. Stat and
bar-gauge panels require `instant`; logs and time-series panels require `range`.
Hidden helper targets follow the same mode contract as visible targets.
The checker and query regressions inspect every target, including hidden helpers
and targets saved inside rows, regardless of target order. Every selector must
contain `source_type="journald"`, `host=~"$host"` and `service=~"$service"`.
Ordinary panels also require `level=~"$level"`. Only the two named error panels
use `level=~"error|critical|alert|emerg"`; Log Volume by Level must omit level
matchers to compare all levels. These exceptions cannot reference `$level`.
Selector values for `host`, `service` and `level` allow `$name`, `${name}` and
`[[name]]` only inside double-quoted strings. All explicit formatters, including
`:regex`, `:raw` and `:text`, are rejected, including in additional matchers
alongside the required filters. Backtick strings remain supported for static
values and pipeline templates, but cannot interpolate these filters: `:regex`
does not escape a literal backtick in producer label data.
The same rule covers the whole target expression and every variable query that
contains a selector, not only the selectors: Grafana interpolates producer label
values into pipeline stages, metric wrappers and comments too. References to
`host`, `service` and `level` outside a double-quoted string (for example
`` |~ `$service` ``, `| x=$host`, `sum by ($service)`, a range `[$host]` or a
`# $level` comment), and any explicit formatter there such as `|= "${host:raw}"`,
fail with `unsupported <name> interpolation`. Default references inside
double-quoted pipeline strings, such as `|~ "$host"`, remain supported. This
only keeps a value inside its LogQL string. Stages that parse their string
argument as another language (`pattern`, `regexp`, `json`/`logfmt` parameters,
`line_format`/`label_format` templates) still read the escaped value in that
language; for example, `lokiSpecialRegexEscape` leaves `<` and `>` unchanged for
`pattern`. The escaping is regex escaping, so use the scoped filters only in
regex matchers: `=~` selectors, `|~` line filters and `=~` label filters. In
substring or equality filters such as `|= "$host"`, a value with regex
punctuation arrives escaped and silently fails to match.
Default Loki interpolation escapes regex backslashes a second time for a
double-quoted LogQL string, preserving backticks, backslashes and regex
punctuation as literal data. **Known upstream limitation:** the pinned Grafana
v13.0.0 formatter does not escape double quotes. Label values containing `"`
can break the query; the checker does not promise safe execution for them.
The offline [interpolation oracle](tests/fixtures/README.md) records this failure
alongside successful literal selections using the real Loki v3.6.0 parser.
Supporting quotes needs a separately agreed formatter policy; the provisioned
asset and producer label policy are unchanged by this checker fix.
Each brace selector outside
string literals is split into whole `label op "value"` (or backtick) matchers,
so text inside a quoted or backtick string never counts as a matcher, and
pipeline templates such as `line_format "{{.message}}"` are not selectors.
A `#` line comment ends at its newline; quotes and braces inside it are ignored.
This is a focused contract for this asset's selectors, not a general LogQL
parser or execution check.
The three filter dropdown queries must each be a single
`label_values({selector}, label)` call returning their own label. All require
`source_type="journald"`; `service` additionally requires `host=~"$host"`,
and `level` requires both `host=~"$host"` and `service=~"$service"`.
The same safe interpolation forms and whole-matcher checks apply here, so
unscoped `label_values(service)`, wrong returned labels and parent filters
hidden inside strings are rejected before provisioning. This keeps service
choices within the selected host and level choices within the selected
host/service. For `host`, `service` and `level`, `includeAll` and `multi` must be booleans
when present (omission means false), and at least one must be true: Grafana's
Loki datasource otherwise bypasses literal regex escaping. `allValue` may be
omitted, null or empty for Grafana's default expansion, or exactly `.*` to select
all values. Other custom All expressions are rejected even when All is disabled.
[Grafana does not escape custom All values](https://grafana.com/docs/grafana/latest/visualizations/dashboards/variables/add-template-variables/#custom-all-value),
so they must never contain matcher syntax or interpolated references. Ordinary
single and multiple selections use literal regex escaping; custom All `.*`
intentionally selects every value within the other filters. Additional query
variables remain supported as dropdowns with datasource, reference and dependency
checks. Their selector matchers follow the same formatter/quoting checks for
`host`, `service` and `level`. Their values cannot be interpolated into any panel
target expression or any variable query containing a selector: only the three
policy-scoped filters and the supported built-in globals are allowed there.
This conservative allowlist rejects additional variables even with default
formatting or an apparently safe All value, until a separate interpolation
contract is supported for them. It also covers references in comments and
pipelines, where Grafana still interpolates values. Additional dropdowns can
use the supported host/service/level filters in their selectors; selector-free
variable dependencies and references in display metadata remain supported.
The required dropdown parent scope is specific to the three filters.
All panels, including saved children in nested collapsed rows, must have
positive-size integer `gridPos` rectangles with nonnegative coordinates within
Grafana's 24-column grid. Top-level panels cannot overlap; touching edges are
allowed. Row headers occupy that grid, while saved children inside collapsed
rows are excluded from top-level overlap checks and may overlap the row header.
Within each collapsed row, saved siblings must not overlap each other; touching
edges are allowed. Each nested row has its own saved layout, so its children may
share coordinates with panels outside that row, including visible panels.
Every row must have a boolean `collapsed` flag. Only rows may have a `panels`
list; an expanded row may have an empty list, but its children live at the top
level and are checked there. Leaf panels cannot store children.
Dashboards may contain at most 256 panels in total, including all row headers
and saved children in collapsed/nested rows. This budget is enforced before any
pairwise overlap checks, bounding those checks to at most 32,640 comparisons.
Grafana's built-in annotation datasource is separate from
the Loki query datasource. The check accepts `$name`, `${name:format}` and
`[[name]]` references plus an explicit allowlist of Grafana/Loki globals.
Reference matching follows Grafana's variable pattern: ASCII names (as in
JavaScript, so `$hostä` refers to `host`), `[[name:format]]` only with a word
format, and `${name.fieldPath:format}` field paths, so these count for
undefined-variable, dependency and interpolation checks too. Variable names must
be ASCII. In target `expr` and variable `query` fields, every `${` must start such
a reference with a field path and format of at most 128 characters each, and
reference text cannot contain `"`, a backtick, `#`, `{`, `$`, `[`, `]` or a
newline; otherwise the check fails with `unsupported variable reference syntax`.
This keeps the checker's string and comment tokenization identical to what Loki
sees after Grafana replaces each reference, and keeps matching linear in size.
The supported globals are `__interval`, `__interval_ms`, `__range`, `__range_s`,
`__range_ms`, `__auto`, `__from` and `__to`. They are reserved for built-in
references; defining a query variable with one of these names fails with
`reserved variable name <name>`. Use a different custom variable name.
References between variable `query` strings must be acyclic, including no self-references;
formatted references count as dependencies and built-in globals do not.
In target `expr` and variable `query` fields, an unknown reference that appears only
inside a `#` line comment is not reported as undefined. Comments do not hide
references otherwise: Grafana interpolates and resolves dependencies over the whole
query text, so references in comments still count for the dependency graph and the
fixed-level panel exceptions (a value with a newline would end the comment). The
`label_values(selector, label)` wrapper of the host, service and level variables is
checked on the raw query: a comment inside or after it fails, because Grafana would
then fall back to a label-names query. References inside double-quoted or backtick
strings always count, even after a literal `#`. Titles, descriptions, variable metadata and other strings keep
ordinary reference checking; they do not use LogQL comment rules.
Dependency graphs are walked iteratively: long acyclic chains and shared
dependencies are supported within the asset byte limit, independently of Python's
recursion limit. Cycle diagnostics show the first four path entries and the closing dependency
in stable name order, with `...` for omitted entries.
JSON nesting is limited to 64 container levels, including extension objects and
saved rows: the dashboard root object is level 1, each nested object or array adds
one level, and scalar values add none. Excessive nesting during decoding or
validation fails with exit 1 and a concise diagnostic instead of a traceback.
CLI validation failures emit one ASCII diagnostic line of at most 1,024 bytes,
including its final newline. Paths and scalar identifiers are escaped and
clipped to bounded previews; malformed panel ids report their sibling index
without formatting the rejected value or nested containers. Terminal controls,
newlines and Unicode direction marks cannot act on the terminal. The same path
escaping applies to success output. Validation never rewrites the asset.
It checks this asset's structure, not the full Grafana schema or LogQL syntax.
Synthetic fixtures cover invalid assets and dropdown scope without network
access, provisioning or real logs. Run both commands locally before handing off
changes. There are no separate lint or type-check gates in this
standard-library-only repository.

## Source of Truth

Canonical platform design:

- `ops-meta/specs/2026-03-04_log_aggregation_platform.md`

Live operator runbooks:

- `ops-control/docs/LOGYARD_DEPLOY_MACMINI.md`
- `ops-control/docs/LOGYARD_VECTOR_MACMINI.md`
- `ops-control/docs/LOGYARD_NYXMON_CHECKS.md`
- `ops-control/docs/LOGYARD_INGRESS_MACMINI.md`
