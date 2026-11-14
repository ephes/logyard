# Interpolation oracle

`loki-interpolation.json` is captured from Grafana **v13.0.0** and Loki
**v3.6.0**, independently of the Python dashboard checker. It contains no real
logs. The normal unittest gate reads it offline with the standard library.

The capture runs Grafana's actual `lokiSpecialRegexEscape` and `escapeRegex`
functions in Node. Grafana's pinned `@grafana/scenes` **7.3.8** Regex formatter
uses `escapeRegex`, wrapping multiple choices in `(a|b)`; the Loki default
formatter joins escaped choices with `|`. The shipped dashboard's options
activate escaping (`includeAll=true`, `multi=false`). Default All expands the
choices; custom All `.*` bypasses formatting. Sources and downloaded-source
SHA-256 hashes are stored in the fixture.

Every interpolated expression is passed to Loki's public `syntax.ParseExpr`.
For accepted expressions, `LogSelectorExpr.Matchers()` supplies decoded patterns
and its real RE2 `Matches` method selects synthetic streams. Each literal case
has a selected stream and an unrelated journald stream, plus an out-of-source
stream; multiple choices and both All paths are covered too. Python replays only
these literal/alternation regex results, checks them against the captured RE2
results, and checks the dashboard reference policy separately. No replacement
LogQL parser, Grafana provisioning or live Loki service is involved.

The raw-string/`:regex` backtick and multi cases fail parsing. The checker must
reject every raw scoped-filter template, even when a particular label happens
to parse successfully. Static backtick strings remain allowed.

**Open acceptance gap:** Grafana's default formatter leaves `"` unchanged;
the double-quoted `quote` and `selector` cases fail Loki parsing. This is an
upstream behavior, not a successful round trip. The fixture deliberately records
`parse_ok=false`, and a regression keeps that limitation visible. Slice 70's
full quote-value acceptance needs an owner decision on a different supported
formatter policy; it is not claimed complete by the raw-string restriction.

## Regeneration (optional, network required)

Run outside the checkout in a temporary directory with Node and Go installed.
Set `repo` to the checkout's absolute path; caches must be writable in the sandbox.
These commands download source/dependencies only and start no services:

```sh
repo=/absolute/path/to/logyard
curl -fsSL https://raw.githubusercontent.com/grafana/grafana/v13.0.0/public/app/plugins/datasource/loki/datasource.ts -o datasource.ts
curl -fsSL https://raw.githubusercontent.com/grafana/grafana/v13.0.0/packages/grafana-data/src/text/string.ts -o string.ts
curl -fsSL https://registry.npmjs.org/@grafana/scenes/-/scenes-7.3.8.tgz -o scenes.tgz
node "$repo/tests/fixtures/capture-interpolation.cjs" > input.json
cp "$repo/tests/fixtures/capture-interpolation.go" main.go
go mod init logyard-oracle
GOPATH="$PWD/gopath" GOCACHE="$PWD/cache" go get github.com/grafana/loki/v3/pkg/logql/syntax@v3.6.0
GOPATH="$PWD/gopath" GOCACHE="$PWD/cache" go run . > output.json
python3 -m json.tool output.json > "$repo/tests/fixtures/loki-interpolation.json"
```

On a version update, inspect the datasource callback, TemplateSrv custom-All
bypass, and Scenes Regex formatter again. Capturing current behavior does not
establish correctness for every Grafana version or every LogQL expression.
