# Changelog

## Unreleased

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
