"""Offline selector contracts; no Loki or Grafana instance is needed."""

import copy
import json
from pathlib import Path
import re
import unittest

from scripts.check_dashboard import validate_dashboard


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

    def test_safe_reference_forms_accept_literal_selected_values(self):
        # Values stay in variable metadata; the checker must not interpret their
        # quotes or regex punctuation as query syntax or interpolate them itself.
        for template in ("${name}", "${{{name}}}", "${{{name}:regex}}",
                         "[[{name}]]", "[[{name}:regex]]"):
            with self.subTest(template=template):
                dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
                for variable in dashboard["templating"]["list"]:
                    value = 'synthetic."quoted"[value](a|b)+\\tail'
                    variable["current"] = dict(text=value, value=value)
                for _, target in query_targets(dashboard["panels"]):
                    for name in ("host", "service", "level"):
                        target["expr"] = target["expr"].replace(
                            f"${name}", template.format(name=name),
                        )
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
            'sum(count_over_time({source_type=`journald`, host=~`$host`, '
            'service=~"${service:regex}", level=~"$level"} '
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
