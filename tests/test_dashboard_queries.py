"""Offline selector contracts; no Loki or Grafana instance is needed."""

from collections import Counter
import copy
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from scripts.check_dashboard import (
    check_scoped_interpolation, require, selector_matchers, stream_selectors, validate_dashboard,
)


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

    def test_dropdowns_reject_repeated_required_scope_labels(self):
        for index, name, label, duplicate in (
            (0, "host", "source_type", 'source_type!="journald"'),
            (1, "service", "host", 'host!~"$host"'),
            (2, "level", "service", 'service!~"$service"'),
        ):
            with self.subTest(variable=name, label=label):
                dashboard = copy.deepcopy(self.dashboard)
                variable = dashboard["templating"]["list"][index]
                variable["query"] = variable["query"].replace("}", f", {duplicate}}}", 1)
                with self.assertRaisesRegex(
                    ValueError, f"variable {name}: invalid query scope: repeated {label} matcher"
                ):
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


class DashboardExtraVariableTests(unittest.TestCase):
    def dashboard_with_extra(self, **options):
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        dashboard["templating"]["list"].append(dict(
            name="extra", type="query",
            query='label_values({source_type="journald", host=~"$host", '
                  'service=~"$service"}, environment)',
            datasource=dict(type="loki", uid="logyard-loki"), **options,
        ))
        return dashboard

    def test_additional_dropdowns_can_use_the_supported_parent_filters(self):
        for template in ("$host", "${host}", "[[host]]"):
            with self.subTest(template=template):
                dashboard = self.dashboard_with_extra(includeAll=True, allValue=".*")
                variable = dashboard["templating"]["list"][-1]
                variable["query"] = variable["query"].replace("$host", template)
                dashboard["description"] = "Display metadata may still reference ${extra:text}"
                validate_dashboard(dashboard)

    def test_additional_variables_are_refused_in_target_interpolation(self):
        for reference in ("$extra", "${extra}", "[[extra]]", "${extra:raw}",
                          "[[extra:raw]]", "${extra:regex}"):
            for all_value in (None, ".*", '.*"} or {service=~".*'):
                for hidden, nested in ((False, False), (True, False), (True, True)):
                    with self.subTest(reference=reference, all_value=all_value,
                                      hidden=hidden, nested=nested):
                        dashboard = self.dashboard_with_extra(includeAll=True, allValue=all_value)
                        panel = dashboard["panels"][1]
                        helper = copy.deepcopy(panel["targets"][0])
                        helper.update(refId="B", hide=hidden)
                        helper["expr"] = helper["expr"].replace(
                            'source_type="journald"',
                            f'source_type="journald", environment=~"{reference}"',
                        )
                        panel["targets"].append(helper)
                        if nested:
                            dashboard["panels"][1] = dict(
                                id=100, title="Row", type="row", collapsed=True,
                                gridPos=dict(x=0, y=10, w=6, h=4), panels=[panel],
                            )
                        with self.assertRaisesRegex(ValueError, "unsupported additional variable extra"):
                            validate_dashboard(dashboard)

    def test_additional_variables_cannot_bypass_selectors_via_pipeline_or_comment(self):
        for suffix in (' |~ "${extra:raw}"', ' # ${extra:raw}'):
            with self.subTest(suffix=suffix):
                dashboard = self.dashboard_with_extra()
                dashboard["panels"][1]["targets"][0]["expr"] += suffix
                with self.assertRaisesRegex(ValueError, "unsupported additional variable extra"):
                    validate_dashboard(dashboard)

    def test_additional_variable_interpolation_is_refused_in_dropdown_selectors(self):
        for reference in ("$extra", "${extra:raw}", "[[extra:raw]]"):
            for name in ("service", "choices"):
                with self.subTest(reference=reference, name=name):
                    dashboard = self.dashboard_with_extra(includeAll=True,
                                                          allValue='.*"} or {host=~".*')
                    # Keep this fixture acyclic: extra depends only on host.
                    dashboard["templating"]["list"][-1]["query"] = (
                        'label_values({source_type="journald", host=~"$host"}, environment)'
                    )
                    variable = dict(name=name, type="query",
                                    query='label_values({source_type="journald", host=~"$host", '
                                          f'environment=~"{reference}"}}, service)',
                                    datasource=dict(type="loki", uid="logyard-loki"), includeAll=True)
                    if name == "service":
                        dashboard["templating"]["list"][1] = variable
                    else:
                        dashboard["templating"]["list"].append(variable)
                    with self.assertRaisesRegex(ValueError, "unsupported additional variable extra"):
                        validate_dashboard(dashboard)

    def test_additional_dropdowns_also_validate_core_selector_formatters(self):
        for reference in ("${host:raw}", "[[host:regex]]"):
            with self.subTest(reference=reference):
                dashboard = self.dashboard_with_extra()
                variable = dashboard["templating"]["list"][-1]
                variable["query"] = variable["query"].replace("$host", reference)
                with self.assertRaisesRegex(ValueError, "unsupported host selector formatter"):
                    validate_dashboard(dashboard)


class DashboardTargetScopeTests(unittest.TestCase):
    def test_valid_matchers_do_not_build_scope_diagnostics(self):
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        extra_matchers = 'environment=~"prod|staging", environment!="staging", ' * 64
        for _, target in query_targets(dashboard["panels"]):
            target["expr"] = target["expr"].replace("{", "{" + extra_matchers, 1)
        for variable in dashboard["templating"]["list"]:
            variable["query"] = variable["query"].replace("{", "{" + extra_matchers, 1)

        diagnostics = Counter()

        def observe(condition, message):
            _, separator, detail = message.partition(": invalid query scope: ")
            if separator:
                diagnostics[detail] += 1
            require(condition, message)

        with patch("scripts.check_dashboard.require", side_effect=observe):
            validate_dashboard(dashboard)
        self.assertEqual(diagnostics, {})

    def test_scope_errors_keep_target_and_label_context(self):
        for clause, message in (
            ('host=~$host', "unparseable selector"),
            ('host=~"$host" service=~"$service"', "unparseable selector"),
            ('host=~"${host:raw}"', "unsupported host selector formatter"),
            ('host=~"$host", host=~"$host"', "repeated host matcher"),
            ('host="fixed"', "expected host filter"),
        ):
            with self.subTest(clause=clause):
                dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
                panel = dashboard["panels"][1]
                target = panel["targets"][0]
                target["expr"] = target["expr"].replace('host=~"$host"', clause)
                with self.assertRaises(ValueError) as failure:
                    validate_dashboard(dashboard)
                self.assertEqual(
                    str(failure.exception),
                    f"panel {panel['id']}: target {target['refId']}: invalid query scope: {message}",
                )

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

    def test_targets_reject_repeated_required_scope_labels(self):
        source = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        for title, label, duplicate in (
            ("Log Lines (5m)", "host", 'host!~"$host"'),
            ("Log Lines (5m)", "service", 'service=~"$service"'),
            ("Errors (15m)", "level", 'level!~"error|critical|alert|emerg"'),
        ):
            with self.subTest(panel=title, label=label):
                dashboard = copy.deepcopy(source)
                panel = next(panel for panel in dashboard["panels"] if panel["title"] == title)
                target = panel["targets"][0]
                target["expr"] = target["expr"].replace("{", f"{{{duplicate}, ", 1)
                with self.assertRaisesRegex(
                    ValueError,
                    f"panel {panel['id']}: target {target['refId']}:"
                    f" invalid query scope: repeated {label} matcher",
                ):
                    validate_dashboard(dashboard)

        validate_dashboard(source)

    def test_repeated_nonrequired_matchers_remain_valid(self):
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        matchers = 'environment=~"prod|staging", environment!="staging", '
        target = dashboard["panels"][1]["targets"][0]
        target["expr"] = target["expr"].replace("{", "{" + matchers, 1)
        variable = dashboard["templating"]["list"][0]
        variable["query"] = variable["query"].replace("{", "{" + matchers, 1)
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


class DashboardScopedInterpolationTests(unittest.TestCase):
    """Producer label values reach every interpolated position, not only selectors."""

    UNSAFE_SUFFIXES = (
        ' |~ `{ref}`',
        ' | label_format x=`{ref}`',
        ' | x={ref}',
        ' |= "x" {ref}',
        ' # {ref}',
        ' |= "{raw}"',
        ' |~ "{regex}"',
        ' | line_format "{text}"',
    )

    @staticmethod
    def references(name):
        return dict(ref=f"${name}", raw=f"${{{name}:raw}}",
                    regex=f"[[{name}:regex]]", text=f"${{{name}:text}}")

    def recent_logs(self, dashboard):
        return next(panel for panel in dashboard["panels"]
                    if panel["title"] == "Recent Logs")["targets"][0]

    def test_pipeline_and_comment_interpolation_needs_default_double_quotes(self):
        for name in ("host", "service", "level"):
            for suffix in self.UNSAFE_SUFFIXES:
                expr_suffix = suffix.format(**self.references(name))
                with self.subTest(name=name, suffix=expr_suffix):
                    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
                    self.recent_logs(dashboard)["expr"] += expr_suffix
                    with self.assertRaisesRegex(
                        ValueError, f"invalid query scope: unsupported {name} interpolation"
                    ):
                        validate_dashboard(dashboard)

    def test_metric_wrappers_cannot_interpolate_scoped_filters(self):
        for name in ("host", "service", "level"):
            for template in ("sum by (${name}) (count_over_time({selector}[5m]))",
                             "sum(count_over_time({selector}[${name}]))"):
                with self.subTest(name=name, template=template):
                    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
                    target = self.recent_logs(dashboard)
                    target["expr"] = template.replace("{selector}", target["expr"]).replace(
                        "{name}", name)
                    with self.assertRaisesRegex(
                        ValueError, f"invalid query scope: unsupported {name} interpolation"
                    ):
                        validate_dashboard(dashboard)

    def test_hidden_and_nested_targets_are_checked_too(self):
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        panel = next(panel for panel in dashboard["panels"] if panel["title"] == "Recent Logs")
        helper = copy.deepcopy(panel["targets"][0])
        helper.update(refId="B", hide=True)
        helper["expr"] += " |~ `$service`"
        panel["targets"].append(helper)
        dashboard["panels"].remove(panel)
        dashboard["panels"].append(dict(
            id=100, title="Row", type="row", collapsed=True,
            gridPos=dict(x=0, y=100, w=24, h=1), panels=[panel],
        ))
        with self.assertRaisesRegex(ValueError, "unsupported service interpolation"):
            validate_dashboard(dashboard)

    def test_grafana_field_path_and_brace_format_references_are_seen(self):
        # Grafana also interpolates ${name.fieldPath[:format]} and formats
        # containing "{"; an unknown format falls back to unescaped output.
        for name in ("host", "service", "level"):
            for suffix in (' |~ `${{{name}.0}}`', ' | x=${{{name}.0:raw}}',
                           ' # ${{{name}.text}}', ' |= "${{{name}.0}}"',
                           ' |= "${{{name}:a{{b}}"'):
                expr_suffix = suffix.format(name=name)
                with self.subTest(name=name, suffix=expr_suffix):
                    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
                    self.recent_logs(dashboard)["expr"] += expr_suffix
                    with self.assertRaisesRegex(
                        ValueError, f"unsupported ({name} interpolation|variable reference syntax)"
                    ):
                        validate_dashboard(dashboard)
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        target = self.recent_logs(dashboard)
        target["expr"] = target["expr"].replace('"$host"', '"${host.0}"')
        with self.assertRaisesRegex(ValueError, "unsupported host selector formatter"):
            validate_dashboard(dashboard)

    def test_reference_text_cannot_carry_logql_string_or_comment_syntax(self):
        for reference in ('${__interval:a"}', '${__interval.a`b}', '${__range:x#y}',
                          '${__range.a{b}', '${__from:a\nb}', '${__interval:$host}',
                          '${__interval.[[host]]}', '${1}', '${__interval:',
                          '${__interval:' + 'x' * 129 + '}',
                          '${host.' + 'x' * 129 + '}'):
            with self.subTest(reference=reference, field="target"):
                dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
                self.recent_logs(dashboard)["expr"] += f' |= "x" {reference}'
                with self.assertRaisesRegex(
                    ValueError, "panel 8: invalid target: unsupported variable reference syntax"
                ):
                    validate_dashboard(dashboard)
            with self.subTest(reference=reference, field="variable"):
                dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
                dashboard["templating"]["list"][0]["query"] += f" {reference}"
                with self.assertRaisesRegex(
                    ValueError, "variable host: invalid query: unsupported variable reference syntax"
                ):
                    validate_dashboard(dashboard)

    def test_bracket_references_follow_grafana_word_formats(self):
        # Grafana's [[name:format]] takes only word formats, so it interpolates
        # the $host inside [[__interval:$host]] on its own.
        for suffix in (' |= "x" [[__interval:$host]]', ' # [[__interval:$host]]'):
            with self.subTest(suffix=suffix):
                dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
                self.recent_logs(dashboard)["expr"] += suffix
                with self.assertRaisesRegex(ValueError, "unsupported host interpolation"):
                    validate_dashboard(dashboard)

    def test_reference_names_are_ascii_like_grafana(self):
        # JavaScript \w is ASCII-only: Grafana reads "$hostä" as $host.
        for suffix in (' # $hostä', ' # x $levelı'):
            with self.subTest(suffix=suffix):
                dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
                self.recent_logs(dashboard)["expr"] += suffix
                with self.assertRaisesRegex(ValueError, "unsupported (host|level) interpolation"):
                    validate_dashboard(dashboard)
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        dashboard["templating"]["list"].append(dict(
            name="hostä", type="query", query="label_values(environment)",
            datasource=dict(type="loki", uid="logyard-loki"),
        ))
        with self.assertRaisesRegex(ValueError, "variable needs a valid name"):
            validate_dashboard(dashboard)

    def test_quotes_inside_other_references_do_not_open_strings(self):
        # Grafana replaces the whole reference before Loki tokenizes the query,
        # so its text cannot make a later reference "quoted". The dashboard
        # gate refuses such reference text first; this is defense in depth.
        with self.assertRaisesRegex(ValueError, "unsupported host interpolation"):
            check_scoped_interpolation(' |= ${__interval:a"} $host ${__range:b"}', "ctx")

    def test_default_double_quoted_pipeline_interpolation_remains_valid(self):
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        self.recent_logs(dashboard)["expr"] += (
            ' |= "$host" |~ "${service}" | line_format "{{.message}} [[level]]"'
            ' | label_format x=`{{.host}}` # static note'
        )
        validate_dashboard(dashboard)

    def test_selector_bearing_variable_queries_are_checked_outside_selectors(self):
        for reference in ("${host:raw}", "$host"):
            with self.subTest(reference=reference):
                dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
                dashboard["templating"]["list"].append(dict(
                    name="extra", type="query",
                    query='label_values({source_type="journald", '
                          f'service=~"$service"}}, {reference})',
                    datasource=dict(type="loki", uid="logyard-loki"),
                ))
                with self.assertRaisesRegex(ValueError, "unsupported host interpolation"):
                    validate_dashboard(dashboard)


if __name__ == "__main__":
    unittest.main()
