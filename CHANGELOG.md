# Changelog

## Unreleased

- Validate the built-in dashboard annotation's Grafana datasource and require
  additional annotation queries to be explicitly disabled with a hidden toggle.
- Require the specialized dashboard panel IDs to retain query panels with their
  provisioned instant or range targets, including inside collapsed rows.
- Keep the error-only and all-level query scope rules tied to stable panel IDs when titles change.
- Reject dashboard panel IDs, schema versions and grid bottoms outside the browser-safe integer range.
- Refuse dashboards with more than 65,536 decoded JSON nodes before semantic checks.
- Size dashboard read requests to the checked file size plus one byte.
- Keep contiguous query spans when removing LogQL comments, reducing temporary allocations.
- Reject surrounding whitespace in dashboard target `refId` values.
- Reuse parsed selector matchers when checking the required dashboard dropdown scope.
- Share full-query variable references between dashboard target scope checks.
- Check dashboard overlaps between neighboring vertical intervals in each Grafana grid column.
- Skip overlap checks for empty dashboard row child lists.
- Build target reference previews for duplicate diagnostics only when a duplicate is found.
- Build dashboard selector and scope diagnostics only on failure.
- Require text panels to omit `targets` or use an empty list, including within rows.
- Keep ASCII escape sequences intact when truncating dashboard diagnostics.
- Bound the dashboard scalar and metadata reference traversal stack by nesting
  depth, including for wide extension arrays and objects.
- Reject row headers whose `targets` is anything but an empty list, including
  nested rows.
- Reject repeated required scope labels in panel and filter dropdown selectors.
- Reject unpaired Unicode surrogate escapes in dashboard JSON keys and values.
- Refuse dashboards with more than 256 variables before scanning query text.
- Show the actual variable dependency cycle in the bounded diagnostic, starting
  at the repeated variable.
- Format overlap diagnostics only for the first colliding pair of panels.

- Apply the host/service/level interpolation policy to the whole query text:
  pipeline stages, metric wrappers and comments in targets and selector-bearing
  variable queries can no longer interpolate these filters raw, unquoted, in
  backtick strings or with an explicit formatter. Match references like
  Grafana does (ASCII names, word-only `[[name:format]]`, `${name.fieldPath:format}`),
  require ASCII variable names, and refuse unparseable `${`, overlong reference
  parts and quotes, backticks, `#`, `{`, `$`, brackets or newlines inside
  reference text in queries.

- Refuse additional query-variable interpolation in panel targets and
  selector-bearing dropdown queries until an escaping/All policy is supported;
  validate core-filter formatters in additional dropdown selectors too.

- Bound dashboard CLI failure diagnostics to one escaped ASCII line of at most
  1,024 bytes; avoid rendering rejected panel ids and clip identifier/cycle
  previews without changing the asset.

- Reject overlapping saved siblings within collapsed dashboard rows, including
  nested rows, while keeping each row's saved layout separate from the visible grid.
- Refuse detected dashboard changes during bounded reads by comparing descriptor
  metadata and consumed length, with a documented policy for atomic replacements.
