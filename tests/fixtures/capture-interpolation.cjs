// Optional fixture capture, not part of the offline gate. Run from a temporary
// directory containing the pinned Grafana sources listed in README.md.
const fs = require('node:fs');
const crypto = require('node:crypto');
const vm = require('node:vm');
const source = fs.readFileSync('datasource.ts', 'utf8');
const text = fs.readFileSync('string.ts', 'utf8');
const helpers = source.slice(source.indexOf('export function lokiSpecialRegexEscape'))
  .replace('export function lokiSpecialRegexEscape<T>(value: T)', 'function lokiSpecialRegexEscape(value)')
  + text.slice(text.indexOf('export function escapeRegex'))
  .replace('export function escapeRegex(value: string): string', 'function escapeRegex(value)');
const { lokiSpecialRegexEscape, escapeRegex } = vm.runInNewContext(
  helpers + '; ({ lokiSpecialRegexEscape, escapeRegex })'
);
const cases = [
  ['backtick', 'synthetic`tail'],
  ['quote', 'synthetic"tail'],
  ['backslash', 'synthetic\\tail'],
  ['regex', 'synthetic.[a|b]+(x){2}^$?'],
  ['selector', 'synthetic"} or {source_type="other'],
  ['multi', ['synthetic`tail', 'synthetic.[a|b]+']],
];
const rows = [];
for (const [name, value] of cases) {
  for (const raw of [false, true]) {
    const values = Array.isArray(value) ? value : [value];
    const escaped = values.map(raw ? escapeRegex : lokiSpecialRegexEscape);
    const output = raw && Array.isArray(value) ? '(' + escaped.join('|') + ')' : escaped.join('|');
    const quote = raw ? '`' : '"';
    const prefix = '{source_type="journald", host=~"$host", service=~';
    const suffix = ', level=~"$level"}';
    const template = prefix + quote + (raw ? '${service:regex}' : '$service') + quote + suffix;
    const expr = template.replace('$host', 'host-a').replace('$level', 'info').replace(raw ? '${service:regex}' : '$service', () => output);
    rows.push({name, values, raw, template, expr});
  }
}
// TemplateSrv returns custom All directly, or formats the list of choices for default All.
rows.push({name:'custom-all',values:['svc-a','svc-b'],raw:false,
  template:'{source_type="journald", host=~"$host", service=~"$service", level=~"$level"}',
  expr:'{source_type="journald", host=~"host-a", service=~".*", level=~"info"}'});
rows.push({name:'default-all',values:['svc.a','svc`b'],raw:false,
  template:'{source_type="journald", host=~"$host", service=~"$service", level=~"$level"}',
  expr:'{source_type="journald", host=~"host-a", service=~"'+['svc.a','svc`b'].map(lokiSpecialRegexEscape).join('|')+'", level=~"info"}'});
const base = 'https://github.com/grafana/grafana/blob/v13.0.0/';
console.log(JSON.stringify({
  grafana_version: '13.0.0', scenes_version: '7.3.8', loki_version: '3.6.0',
  sources: {
    datasource: base + 'public/app/plugins/datasource/loki/datasource.ts',
    escapeRegex: base + 'packages/grafana-data/src/text/string.ts',
    formatter: 'https://www.npmjs.com/package/@grafana/scenes/v/7.3.8',
    parser: 'https://github.com/grafana/loki/blob/v3.6.0/pkg/logql/syntax/parser.go',
  },
  source_sha256: Object.fromEntries(['datasource.ts', 'string.ts', 'scenes.tgz'].map(
    path => [path, crypto.createHash('sha256').update(fs.readFileSync(path)).digest('hex')]
  )),
  cases: rows,
}, null, 2));
