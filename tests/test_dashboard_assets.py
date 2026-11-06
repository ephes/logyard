"""Synthetic failures for the offline asset check."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.check_dashboard import DEFAULT_PATH, validate_dashboard


class DashboardAssetTests(unittest.TestCase):
    def setUp(self):
        self.dashboard = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))

    def test_committed_dashboard(self):
        validate_dashboard(self.dashboard)

    def test_invalid_shapes(self):
        for key, value in (
            ("panels", {}), ("panels", []), ("templating", {}),
            ("title", ""), ("uid", "wrong"), ("schemaVersion", False),
        ):
            with self.subTest(key=key, value=value):
                dashboard = copy.deepcopy(self.dashboard)
                dashboard[key] = value
                with self.assertRaises(ValueError):
                    validate_dashboard(dashboard)
        with self.assertRaises(ValueError):
            validate_dashboard([])

    def test_duplicate_panel_ids_including_rows(self):
        duplicate = copy.deepcopy(self.dashboard["panels"][1])
        for panel in (duplicate, dict(id=99, title="Row", type="row", panels=[duplicate])):
            with self.subTest(panel=panel["type"]):
                dashboard = copy.deepcopy(self.dashboard)
                dashboard["panels"].append(panel)
                with self.assertRaisesRegex(ValueError, "panel id"):
                    validate_dashboard(dashboard)

    def test_expanded_row_with_top_level_children(self):
        self.dashboard["panels"].append(dict(id=99, title="Row", type="row", panels=[]))
        validate_dashboard(self.dashboard)

    def test_duplicate_target_refids_including_rows(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                dashboard = copy.deepcopy(self.dashboard)
                panel = dashboard["panels"][1]
                panel["targets"].append(copy.deepcopy(panel["targets"][0]))
                if nested:
                    dashboard["panels"][1] = dict(
                        id=99, title="Row", type="row", panels=[panel],
                    )
                with self.assertRaisesRegex(ValueError, "duplicate target refId"):
                    validate_dashboard(dashboard)

    def test_target_refids_are_scoped_to_each_panel(self):
        panel = self.dashboard["panels"][1]
        other = copy.deepcopy(panel)
        other["id"] = 100
        self.dashboard["panels"].append(dict(
            id=99, title="Row", type="row", panels=[other],
        ))
        extra = copy.deepcopy(panel["targets"][0])
        extra["refId"] = "B"
        panel["targets"].append(extra)
        validate_dashboard(self.dashboard)

    def test_query_panels_require_a_visible_target_including_rows(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                dashboard = copy.deepcopy(self.dashboard)
                panel = dashboard["panels"][1]
                for target in panel["targets"]:
                    target["hide"] = True
                if nested:
                    dashboard["panels"][1] = dict(
                        id=99, title="Row", type="row", panels=[panel],
                    )
                with self.assertRaisesRegex(ValueError, "visible target"):
                    validate_dashboard(dashboard)

    def test_target_hide_must_be_boolean(self):
        for hide in (None, 0, 1, "false", "true", [], {}):
            with self.subTest(hide=hide):
                dashboard = copy.deepcopy(self.dashboard)
                panel = dashboard["panels"][1]
                extra = copy.deepcopy(panel["targets"][0])
                extra.update(refId="B", hide=hide)
                panel["targets"].append(extra)
                with self.assertRaisesRegex(ValueError, "hide must be a boolean"):
                    validate_dashboard(dashboard)

    def test_visible_targets_allow_hidden_helpers(self):
        panel = self.dashboard["panels"][1]
        hidden = copy.deepcopy(panel["targets"][0])
        hidden.update(refId="B", hide=True)
        panel["targets"].append(hidden)
        for hide in (None, False):
            with self.subTest(hide=hide):
                if hide is None:
                    panel["targets"][0].pop("hide", None)
                else:
                    panel["targets"][0]["hide"] = hide
                validate_dashboard(self.dashboard)

    def test_text_and_row_panels_need_no_targets(self):
        self.dashboard["panels"] = [
            dict(id=1, title="Text", type="text"),
            dict(id=2, title="Row", type="row", panels=[]),
        ]
        validate_dashboard(self.dashboard)

    def test_required_fields(self):
        for path, key in (
            (("panels", 1), "id"), (("panels", 1), "type"),
            (("panels", 1), "targets"), (("panels", 1, "targets", 0), "expr"),
            (("panels", 1, "targets", 0), "refId"),
            (("templating", "list", 0), "query"),
        ):
            with self.subTest(path=path, key=key):
                dashboard = copy.deepcopy(self.dashboard)
                node = dashboard
                for part in path:
                    node = node[part]
                del node[key]
                with self.assertRaises(ValueError):
                    validate_dashboard(dashboard)

    def test_missing_and_wrong_datasources(self):
        for path in (
            ("panels", 1), ("panels", 1, "targets", 0), ("templating", "list", 0),
        ):
            for datasource in (None, {"type": "loki", "uid": "wrong"}):
                with self.subTest(path=path, datasource=datasource):
                    dashboard = copy.deepcopy(self.dashboard)
                    node = dashboard
                    for part in path:
                        node = node[part]
                    node["datasource"] = datasource
                    with self.assertRaisesRegex(ValueError, "datasource"):
                        validate_dashboard(dashboard)

    def test_variable_references(self):
        for reference in ("$missing", "${missing}", "${missing:regex}", "[[missing]]"):
            for node in (self.dashboard["panels"][1]["targets"][0],
                         self.dashboard["templating"]["list"][1]):
                key = "expr" if "expr" in node else "query"
                original = node[key]
                node[key] = reference
                with self.subTest(reference=reference, key=key):
                    with self.assertRaisesRegex(ValueError, "undefined variable"):
                        validate_dashboard(self.dashboard)
                node[key] = original

    def test_defined_formatted_variables_and_allowed_builtins(self):
        self.dashboard["panels"][1]["targets"][0]["expr"] = (
            '{host=~"${host:regex}", service=~"[[service:regex]]"}[$__interval]'
        )
        validate_dashboard(self.dashboard)
        self.dashboard["panels"][1]["targets"][0]["expr"] = "$__unknown"
        with self.assertRaisesRegex(ValueError, "undefined variable"):
            validate_dashboard(self.dashboard)

    def test_variable_names_are_unique_and_required(self):
        self.dashboard["templating"]["list"][1]["name"] = "host"
        with self.assertRaises(ValueError):
            validate_dashboard(self.dashboard)
        self.dashboard["templating"]["list"] = []
        with self.assertRaises(ValueError):
            validate_dashboard(self.dashboard)

    def test_cli_rejects_bad_assets(self):
        invalid = copy.deepcopy(self.dashboard)
        invalid["panels"][1]["id"] = invalid["panels"][0]["id"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            for content in ("{", "[]", json.dumps(invalid)):
                path.write_text(content, encoding="utf-8")
                result = subprocess.run(
                    [sys.executable, "-m", "scripts.check_dashboard", str(path)],
                    cwd=DEFAULT_PATH.parents[4], capture_output=True, text=True,
                )
                with self.subTest(content=content[:20]):
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Invalid dashboard", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
