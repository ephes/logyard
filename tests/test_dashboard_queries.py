"""Offline selector contracts; no Loki or Grafana instance is needed."""

import copy
import json
from pathlib import Path
import re
import unittest

from scripts.check_dashboard import selector_matchers, stream_selectors, validate_dashboard


DASHBOARD = (
    Path(__file__).resolve().parents[1]
    / "deploy/grafana/dashboards/logyard/logyard-home.json"
)


def query_targets(panels):
    for panel in panels:
        for target in panel.get("targets", []):
            yield panel, target
        yield from query_targets(panel.get("panels", []))


class DashboardQueryModeTests(unittest.TestCase):
    def test_every_target_needs_a_supported_query_mode(self):
        for nested in (False, True):
            for mode in (None, "", "both", "invalid", True, [], {}):
                for hidden in (False, True):
                    with self.subTest(nested=nested, mode=mode, hidden=hidden):
                        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
                        panel = dashboard["panels"][1]
                        target = copy.deepcopy(panel["targets"][0])
                        target.update(refId="B", hide=hidden)
                        if mode is None:
                            target.pop("queryType")
                        else:
                            target["queryType"] = mode
                        panel["targets"].append(target)
                        if nested:
                            dashboard["panels"][1] = dict(
                                id=100, title="Row", type="row", collapsed=True,
                                gridPos=dict(x=0, y=10, w=6, h=4), panels=[panel],
                            )
                        with self.assertRaisesRegex(ValueError, "queryType"):
                            validate_dashboard(dashboard)

    def test_query_modes_match_current_panel_roles(self):
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        for index, panel in enumerate(dashboard["panels"]):
            for target_index, target in enumerate(panel.get("targets", [])):
                expected = "range" if panel["type"] in ("logs", "timeseries") else "instant"
                with self.subTest(panel=panel["title"]):
                    self.assertEqual(target["queryType"], expected)
                    wrong = copy.deepcopy(dashboard)
                    wrong["panels"][index]["targets"][target_index]["queryType"] = (
                        "instant" if expected == "range" else "range"
                    )
                    with self.assertRaisesRegex(ValueError, "queryType"):
                        validate_dashboard(wrong)


class DashboardQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        cls.queries = [
            (panel["title"], target["expr"])
            for panel, target in query_targets(dashboard["panels"])
        ]

    def test_filter_policy(self):
        for title, query in self.queries:
            with self.subTest(panel=title):
                self.assertIn('source_type="journald"', query)
                self.assertIn('host=~"$host"', query)
                self.assertIn('service=~"$service"', query)
                if title in ("Errors (15m)", "Errors and Critical Logs"):
                    self.assertNotIn("$level", query)
                    self.assertIn('level=~"error|critical|alert|emerg"', query)
                elif title == "Log Volume by Level":
                    self.assertNotIn("$level", query)
                else:
                    self.assertIn('level=~"$level"', query)

    def test_summary_selectors_on_synthetic_streams(self):
        # One line per stream, all within the same 15-minute window.
        streams = [
            dict(host=host, service=service, level=level, source_type="journald")
            for host in ("host-a", "host-b")
            for service in ("service-a", "service-b")
            for level in ("info", "error")
        ]
        summaries = {
            "Active Services (15m)": "service",
            "Active Hosts (15m)": "host",
            "Top Services (15m)": "service",
        }
        for host, service, level, expected_lines in (
            ("host-a", "service-a", "info", 1),
            ("host-b", "service-b", ".*", 2),
            (".*", ".*", ".*", 8),
        ):
            variables = dict(host=host, service=service, level=level)
            for title, query in self.queries:
                if title not in summaries:
                    continue
                group = summaries[title]
                with self.subTest(panel=title, variables=variables):
                    # Evaluate only this dashboard's simple label matchers,
                    # not LogQL aggregation or Grafana interpolation.
                    selector = re.search(r"\{([^{}]+)\}", query)[1]
                    matchers = re.findall(r'(\w+)(=~|=)"([^"]*)"', selector)
                    selected = streams
                    for label, operator, value in matchers:
                        value = variables.get(value[1:], value) if value.startswith("$") else value
                        selected = [
                            stream for stream in selected
                            if (re.fullmatch(value, stream[label]) is not None
                                if operator == "=~" else stream[label] == value)
                        ]
                    self.assertEqual(len(selected), expected_lines)
                    expected_groups = 2 if host == ".*" else 1
                    self.assertEqual(len({stream[group] for stream in selected}), expected_groups)


class DashboardVariableQueryTests(unittest.TestCase):
    def setUp(self):
        self.dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))

    def test_scoped_custom_all_values_are_allowlisted(self):
        for index in range(3):
            for value in ('"} or {source_type="other', ".+", "$host", " .*", 0, False, [], {}):
                with self.subTest(index=index, value=value):
                    dashboard = copy.deepcopy(self.dashboard)
                    dashboard["templating"]["list"][index]["allValue"] = value
                    with self.assertRaisesRegex(ValueError, "allValue"):
                        validate_dashboard(dashboard)
            for value in (None, "", ".*"):
                dashboard = copy.deepcopy(self.dashboard)
                dashboard["templating"]["list"][index]["allValue"] = value
                validate_dashboard(dashboard)
            dashboard = copy.deepcopy(self.dashboard)
            del dashboard["templating"]["list"][index]["allValue"]
            validate_dashboard(dashboard)

    def test_scoped_boolean_options_must_enable_literal_escaping(self):
        for index in range(3):
            for key in ("includeAll", "multi"):
                for value in (None, 0, 1, "false", [], {}):
                    with self.subTest(index=index, key=key, value=value):
                        dashboard = copy.deepcopy(self.dashboard)
                        dashboard["templating"]["list"][index][key] = value
                        with self.assertRaisesRegex(ValueError, key + " must be a boolean"):
                            validate_dashboard(dashboard)
            for multi, include_all in ((True, False), (False, True), (True, True)):
                dashboard = copy.deepcopy(self.dashboard)
                dashboard["templating"]["list"][index].update(multi=multi, includeAll=include_all)
                validate_dashboard(dashboard)
            dashboard = copy.deepcopy(self.dashboard)
            dashboard["templating"]["list"][index].update(multi=False, includeAll=False)
            with self.assertRaisesRegex(ValueError, "enable multi or includeAll"):
                validate_dashboard(dashboard)
            for key in ("multi", "includeAll"):
                del dashboard["templating"]["list"][index][key]
            with self.assertRaisesRegex(ValueError, "enable multi or includeAll"):
                validate_dashboard(dashboard)

    def test_dropdowns_require_journald_and_each_parent_filter(self):
        for index, original in enumerate(self.dashboard["templating"]["list"]):
            body = stream_selectors(original["query"])[0]
            for matcher in body.split(", "):
                with self.subTest(variable=original["name"], missing=matcher):
                    dashboard = copy.deepcopy(self.dashboard)
                    variable = dashboard["templating"]["list"][index]
                    # Replace rather than delete, so a selector never becomes empty
                    # and the exact missing-filter diagnostic is asserted.
                    remaining = ", ".join('unrelated="synthetic"' if part == matcher else part
                                          for part in body.split(", "))
                    variable["query"] = original["query"].replace(body, remaining)
                    label = matcher.split("=")[0]
                    with self.assertRaisesRegex(ValueError, f"variable .*query scope: expected {label} filter"):
                        validate_dashboard(dashboard)

    def test_dropdown_query_must_return_its_own_label_from_one_selector(self):
        for index, original in enumerate(self.dashboard["templating"]["list"]):
            name = original["name"]
            for query in (
                f"label_values({name})",
                original["query"].replace(f", {name})", ", environment)"),
                original["query"] + ' or label_values({source_type="other"}, service)',
                'query_result({source_type="journald"})',
                'label_values("{source_type=\\"journald\\"}", ' + name + ')',
            ):
                with self.subTest(variable=name, query=query):
                    dashboard = copy.deepcopy(self.dashboard)
                    dashboard["templating"]["list"][index]["query"] = query
                    with self.assertRaisesRegex(ValueError, "variable .*query scope"):
                        validate_dashboard(dashboard)

    def test_dropdown_parent_matchers_cannot_be_spoofed_or_use_raw(self):
        original = self.dashboard["templating"]["list"][1]["query"]
        for replacement in ('host=~".*"', 'host="$host"', 'other=~"$host"',
                            'extra=`host=~"$host"`', 'host=~"${host:raw}"',
                            'host=~"[[host:text]]"', 'host=~"prefix-$host"'):
            with self.subTest(replacement=replacement):
                dashboard = copy.deepcopy(self.dashboard)
                dashboard["templating"]["list"][1]["query"] = original.replace(
                    'host=~"$host"', replacement,
                )
                with self.assertRaisesRegex(ValueError, "variable service:.*query scope"):
                    validate_dashboard(dashboard)

    def test_dropdowns_allow_default_formats_and_whitespace(self):
        for template, quote in (("${name}", '"'), ("${{{name}}}", '"'), ("[[{name}]]", '"')):
            with self.subTest(template=template):
                dashboard = copy.deepcopy(self.dashboard)
                for variable in dashboard["templating"]["list"]:
                    query = variable["query"].replace('"', quote)
                    for name in ("host", "service"):
                        query = query.replace(f"${name}", template.format(name=name))
                    variable["query"] = " \n" + query.replace("label_values(", "label_values( \n ") + " \n"
                validate_dashboard(dashboard)

    def test_dropdown_choices_on_synthetic_streams_follow_parents(self):
        streams = [
            dict(host="host-a", service="shared", level="info", source_type="journald"),
            dict(host="host-a", service="a-only", level="error", source_type="journald"),
            dict(host="host-b", service="b-only", level="debug", source_type="journald"),
            dict(host="host-c", service="other-source", level="trace", source_type="other"),
        ]
        selected_values = dict(host="host-a", service="a-only")
        expected = dict(host={"host-a", "host-b"}, service={"shared", "a-only"}, level={"error"})
        validate_dashboard(self.dashboard)
        for variable in self.dashboard["templating"]["list"]:
            with self.subTest(variable=variable["name"]):
                matchers = selector_matchers(stream_selectors(variable["query"])[0], "test")
                selected = streams
                for label, operator, value in matchers:
                    value = (re.escape(selected_values[value[1:]])
                             if value.startswith("$") else value)
                    selected = [stream for stream in selected
                                if (re.fullmatch(value, stream[label]) is not None
                                    if operator == "=~" else stream[label] == value)]
                self.assertEqual({stream[variable["name"]] for stream in selected},
                                 expected[variable["name"]])


class DashboardInterpolationOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parent / "fixtures/loki-interpolation.json"
        cls.cases = json.loads(path.read_text(encoding="utf-8"))["cases"]

    def test_default_literal_choices_agree_with_real_loki_matchers(self):
        positive_names = {"backtick", "backslash", "regex", "multi", "custom-all", "default-all"}
        positive = [case for case in self.cases if not case["raw"] and case["name"] in positive_names]
        self.assertEqual({case["name"] for case in positive}, positive_names)
        for case in positive:
            with self.subTest(case=case["name"]):
                self.assertTrue(case["parse_ok"])
                # Decode only the captured selector's string literals; the real
                # LogQL parse and RE2 matches were captured independently in Go.
                matchers = selector_matchers(stream_selectors(case["expr"])[0], "oracle")
                decoded = [(label, op, json.loads('"' + value + '"'))
                           for label, op, value in matchers]
                self.assertEqual({label: value for label, _, value in decoded}, case["patterns"])
                matches = [index for index, stream in enumerate(case["streams"])
                           if all((re.fullmatch(value, stream[label]) is not None
                                   if op == "=~" else value == stream[label])
                                  for label, op, value in decoded)]
                self.assertEqual(matches, case["matches"])
                expected = list(range(len(case["values"])))
                if case["name"] == "custom-all":
                    expected.append(len(case["values"]))
                self.assertEqual(matches, expected)

    def test_raw_templates_are_refused_even_when_some_labels_happen_to_work(self):
        for case in self.cases:
            if not case["raw"]:
                continue
            with self.subTest(case=case["name"]):
                dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
                dashboard["panels"][1]["targets"][0]["expr"] = case["template"]
                with self.assertRaisesRegex(ValueError, "unsupported service selector formatter"):
                    validate_dashboard(dashboard)
        backtick = next(c for c in self.cases if c["name"] == "backtick" and c["raw"])
        self.assertFalse(backtick["parse_ok"], "Grafana :regex cannot escape raw delimiters")

    def test_upstream_default_quote_failure_is_recorded(self):
        # This explicitly preserves the known gap instead of faking a passing
        # interpolation round trip. See fixtures/README.md and the owner question.
        for name in ("quote", "selector"):
            case = next(c for c in self.cases if c["name"] == name and not c["raw"])
            with self.subTest(case=name):
                self.assertFalse(case["parse_ok"])
                self.assertNotIn("matches", case)


class DashboardTargetScopeTests(unittest.TestCase):
    def test_selector_variable_formatters_are_allowlisted(self):
        source = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        for index, panel in enumerate(source["panels"]):
            for target_index, target in enumerate(panel.get("targets", [])):
                for name in ("host", "service", "level"):
                    if f"${name}" not in target["expr"]:
                        continue
                    for template in ("${{{name}:{formatter}}}", "[[{name}:{formatter}]]"):
                        for formatter in ("raw", "text", "csv", "glob", "json", "unknown", "regex:raw"):
                            reference = template.format(name=name, formatter=formatter)
                            with self.subTest(panel=panel["title"], reference=reference):
                                dashboard = copy.deepcopy(source)
                                dashboard["panels"][index]["targets"][target_index]["expr"] = (
                                    target["expr"].replace(f"${name}", reference)
                                )
                                with self.assertRaisesRegex(ValueError, "scope"):
                                    validate_dashboard(dashboard)

    def test_raw_string_scoped_variables_are_rejected_everywhere(self):
        for name in ("host", "service", "level"):
            for reference in (f"${{{name}:regex}}", f"[[{name}:regex]]"):
                for extra in (False, True):
                    with self.subTest(name=name, reference=reference, extra=extra):
                        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
                        target = dashboard["panels"][1]["targets"][0]
                        if extra:
                            target["expr"] = target["expr"].replace(
                                'source_type="journald"',
                                f'source_type="journald", extra=~`{reference}`',
                            )
                        else:
                            target["expr"] = target["expr"].replace(f'"${name}"', f'`{reference}`')
                        with self.assertRaisesRegex(ValueError, "unsupported .* selector formatter"):
                            validate_dashboard(dashboard)
        for index, name in ((1, "host"), (2, "service")):
            dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
            variable = dashboard["templating"]["list"][index]
            variable["query"] = variable["query"].replace(f'"${name}"', f'`${{{name}:regex}}`')
            with self.assertRaisesRegex(ValueError, "unsupported .* selector formatter"):
                validate_dashboard(dashboard)

    def test_reference_validation_does_not_interpret_selected_value_metadata(self):
        # Values stay in variable metadata; the checker must not interpret their
        # quotes or regex punctuation as query syntax or interpolate them itself.
        for template, quote in (("${name}", '"'), ("${{{name}}}", '"'), ("[[{name}]]", '"')):
            with self.subTest(template=template):
                dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
                for variable in dashboard["templating"]["list"]:
                    value = 'synthetic."quoted"[value](a|b)+\\tail'
                    variable["current"] = dict(text=value, value=value)
                for _, target in query_targets(dashboard["panels"]):
                    for name in ("host", "service", "level"):
                        target["expr"] = target["expr"].replace(
                            f'"${name}"', quote + template.format(name=name) + quote,
                        )
                validate_dashboard(dashboard)

    def test_formatter_must_match_the_string_quoting(self):
        # Default interpolation double-escapes for "..." strings; :regex writes
        # single backslashes and cannot preserve raw-string delimiters.
        for replacement in ("`$host`", "`${host}`", "`[[host]]`",
                            '"${host:regex}"', '"[[host:regex]]"'):
            with self.subTest(replacement=replacement):
                dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
                target = dashboard["panels"][1]["targets"][0]
                target["expr"] = target["expr"].replace('"$host"', replacement)
                with self.assertRaisesRegex(ValueError, "unsupported host selector formatter"):
                    validate_dashboard(dashboard)

    def test_extra_matcher_cannot_use_an_unsafe_formatter(self):
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        target = dashboard["panels"][1]["targets"][0]
        target["expr"] = target["expr"].replace(
            'level=~"$level"', 'level=~"$level", extra=~"${host:raw}"',
        )
        with self.assertRaisesRegex(ValueError, "scope"):
            validate_dashboard(dashboard)

    def test_added_unscoped_target_is_rejected_in_any_position_or_visibility(self):
        for hidden in (False, True):
            for reverse in (False, True):
                for nested in (False, True):
                    with self.subTest(hidden=hidden, reverse=reverse, nested=nested):
                        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
                        panel = dashboard["panels"][1]
                        extra = copy.deepcopy(panel["targets"][0])
                        extra.update(refId="B", hide=hidden,
                                     expr='sum(count_over_time({source_type="journald"}[5m]))')
                        panel["targets"].append(extra)
                        if reverse:
                            panel["targets"].reverse()
                        if nested:
                            dashboard["panels"][1] = dict(
                                id=100, title="Row", type="row", collapsed=True,
                                gridPos=dict(x=0, y=10, w=6, h=4), panels=[panel],
                            )
                        with self.assertRaisesRegex(ValueError, "target B:.*scope"):
                            validate_dashboard(dashboard)

    def test_every_selector_must_have_the_required_scope(self):
        source = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        for index, panel in enumerate(source["panels"]):
            for original in panel.get("targets", []):
                matchers = re.search(r"\{([^{}]+)\}", original["expr"])[1].split(", ")
                for matcher in matchers:
                    with self.subTest(panel=panel["title"], missing=matcher):
                        dashboard = copy.deepcopy(source)
                        target = dashboard["panels"][index]["targets"][0]
                        target["expr"] = original["expr"].replace(
                            ", ".join(matchers), ", ".join(m for m in matchers if m != matcher),
                        )
                        with self.assertRaisesRegex(ValueError, "scope"):
                            validate_dashboard(dashboard)
        dashboard = copy.deepcopy(source)
        target = dashboard["panels"][1]["targets"][0]
        target["expr"] += ' + sum(count_over_time({host="other"}[5m]))'
        with self.assertRaisesRegex(ValueError, "scope"):
            validate_dashboard(dashboard)

    def test_level_exceptions_cannot_follow_selected_or_fixed_other_levels(self):
        for title in ("Errors (15m)", "Errors and Critical Logs", "Log Volume by Level"):
            for value in ("$level", "info"):
                with self.subTest(panel=title, value=value):
                    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
                    panel = next(p for p in dashboard["panels"] if p["title"] == title)
                    target = panel["targets"][0]
                    expr = re.sub(r', level=~"[^"]*"', "", target["expr"])
                    target["expr"] = expr.replace('service=~"$service"',
                                                f'service=~"$service", level=~"{value}"')
                    with self.assertRaisesRegex(ValueError, "scope"):
                        validate_dashboard(dashboard)

    def test_scoped_helpers_and_multiple_selectors_remain_valid(self):
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        for panel in dashboard["panels"]:
            if not panel.get("targets"):
                continue
            helper = copy.deepcopy(panel["targets"][0])
            helper.update(refId="B", hide=True)
            # Repeated refIds across panels and reordered targets remain supported.
            helper["expr"] += " or " + helper["expr"]
            panel["targets"].insert(0, helper)
        validate_dashboard(dashboard)

    def test_required_matchers_inside_strings_do_not_count(self):
        # A required matcher hidden in a backtick or quoted string literal is
        # not a matcher; this selector actually selects every host.
        for literal in ('`, host=~"$host", `', '"x, host=~\\"$host\\", y"'):
            with self.subTest(literal=literal):
                dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
                target = dashboard["panels"][1]["targets"][0]
                target["expr"] = (
                    'sum(count_over_time({source_type="journald", host=~`.+`, '
                    f'service=~"$service", level=~"$level", x!={literal}}}[5m]))'
                )
                with self.assertRaisesRegex(ValueError, "scope"):
                    validate_dashboard(dashboard)

    def test_backtick_matchers_and_pipeline_templates_remain_valid(self):
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        target = dashboard["panels"][1]["targets"][0]
        target["expr"] = (
            'sum(count_over_time({source_type=`journald`, host=~"$host", '
            'service=~"$service", level=~"$level"} '
            '| line_format "{{.message}}" | label_format x=`{{.host}}` [5m]))'
        )
        validate_dashboard(dashboard)

    def test_comment_quotes_cannot_hide_an_unscoped_selector(self):
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        target = dashboard["panels"][1]["targets"][0]
        target["expr"] = (
            'sum(count_over_time({source_type="journald", host=~"$host", '
            'service=~"$service", level=~"$level"}[5m])) # "\n'
            ' or sum(count_over_time({source_type=~".+"}[5m])) # "'
        )
        with self.assertRaisesRegex(ValueError, "scope"):
            validate_dashboard(dashboard)

    def test_commented_out_selectors_are_ignored(self):
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        target = dashboard["panels"][1]["targets"][0]
        target["expr"] += ' # {host="other"} "unbalanced'
        validate_dashboard(dashboard)

    def test_unparseable_selectors_are_rejected(self):
        for selector in ('source_type="journald" host=~"$host"', 'host=~$host', ''):
            with self.subTest(selector=selector):
                dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
                target = dashboard["panels"][1]["targets"][0]
                target["expr"] = f"sum(count_over_time({{{selector}}}[5m]))"
                with self.assertRaisesRegex(ValueError, "scope"):
                    validate_dashboard(dashboard)


if __name__ == "__main__":
    unittest.main()
