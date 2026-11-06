"""Offline selector contracts; no Loki or Grafana instance is needed."""

import json
from pathlib import Path
import re
import unittest


DASHBOARD = (
    Path(__file__).resolve().parents[1]
    / "deploy/grafana/dashboards/logyard/logyard-home.json"
)


class DashboardQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        cls.queries = {
            panel["title"]: panel["targets"][0]["expr"]
            for panel in dashboard["panels"]
            if panel.get("targets")
        }

    def test_filter_policy(self):
        for title, query in self.queries.items():
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
            for title, group in summaries.items():
                with self.subTest(panel=title, variables=variables):
                    # Evaluate only this dashboard's simple label matchers,
                    # not LogQL aggregation or Grafana interpolation.
                    selector = re.search(r"\{([^{}]+)\}", self.queries[title])[1]
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


if __name__ == "__main__":
    unittest.main()
