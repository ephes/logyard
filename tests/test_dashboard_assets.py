"""Synthetic failures for the offline asset check."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.check_dashboard import DEFAULT_PATH, validate_dashboard


def row(children, collapsed=True):
    return dict(
        id=99, title="Row", type="row", panels=children, collapsed=collapsed,
        gridPos=dict(x=0, y=100, w=24, h=1),
    )


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
        for panel in (duplicate, row([duplicate])):
            with self.subTest(panel=panel["type"]):
                dashboard = copy.deepcopy(self.dashboard)
                dashboard["panels"].append(panel)
                with self.assertRaisesRegex(ValueError, "panel id"):
                    validate_dashboard(dashboard)

    def test_expanded_row_with_top_level_children(self):
        self.dashboard["panels"] = [
            row([], collapsed=False),
            dict(id=100, title="Child", type="text", gridPos=dict(x=0, y=101, w=24, h=2)),
        ]
        validate_dashboard(self.dashboard)

    def test_grid_overlap_is_rejected(self):
        for x, y in ((5, 0), (0, 3), (5, 3), (0, 0)):
            with self.subTest(x=x, y=y):
                self.dashboard["panels"] = [
                    dict(id=1, title="First", type="text", gridPos=dict(x=0, y=0, w=6, h=4)),
                    dict(id=2, title="Second", type="text", gridPos=dict(x=x, y=y, w=6, h=4)),
                ]
                with self.assertRaisesRegex(ValueError, "panels 1 and 2 overlap"):
                    validate_dashboard(self.dashboard)

    def test_grid_touching_edges_are_allowed(self):
        for x, y in ((6, 0), (0, 4), (6, 4)):
            with self.subTest(x=x, y=y):
                self.dashboard["panels"] = [
                    dict(id=1, title="First", type="text", gridPos=dict(x=0, y=0, w=6, h=4)),
                    dict(id=2, title="Second", type="text", gridPos=dict(x=x, y=y, w=6, h=4)),
                ]
                validate_dashboard(self.dashboard)

    def test_grid_positions_are_bounded_integers(self):
        for key, value in (("x", -1), ("y", -1), ("w", 0), ("h", 0),
                           ("x", True), ("y", 1.5), ("w", "6"), ("w", 25)):
            with self.subTest(key=key, value=value):
                dashboard = copy.deepcopy(self.dashboard)
                dashboard["panels"][0]["gridPos"][key] = value
                with self.assertRaisesRegex(ValueError, "gridPos"):
                    validate_dashboard(dashboard)
        for value in (None, {}, dict(x=23, y=0, w=2, h=1)):
            with self.subTest(gridPos=value):
                dashboard = copy.deepcopy(self.dashboard)
                dashboard["panels"][0]["gridPos"] = value
                with self.assertRaisesRegex(ValueError, "gridPos"):
                    validate_dashboard(dashboard)

    def test_collapsed_row_children_do_not_occupy_top_level_grid(self):
        child = copy.deepcopy(self.dashboard["panels"][0])
        child["id"] = 100
        # Saved child coordinates can intersect the row header while collapsed.
        child["gridPos"] = dict(x=0, y=100, w=24, h=10)
        self.dashboard["panels"].append(row([child]))
        validate_dashboard(self.dashboard)
        # The visible row header itself must not intersect another top-level panel.
        self.dashboard["panels"][-1]["gridPos"]["y"] = 0
        with self.assertRaisesRegex(ValueError, "panels 1 and 99 overlap"):
            validate_dashboard(self.dashboard)

    def test_duplicate_target_refids_including_rows(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                dashboard = copy.deepcopy(self.dashboard)
                panel = dashboard["panels"][1]
                panel["targets"].append(copy.deepcopy(panel["targets"][0]))
                if nested:
                    dashboard["panels"][1] = row([panel])
                with self.assertRaisesRegex(ValueError, "duplicate target refId"):
                    validate_dashboard(dashboard)

    def test_target_refids_are_scoped_to_each_panel(self):
        panel = self.dashboard["panels"][1]
        other = copy.deepcopy(panel)
        other["id"] = 100
        self.dashboard["panels"].append(row([other]))
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
                    dashboard["panels"][1] = row([panel])
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
            dict(id=1, title="Text", type="text", gridPos=dict(x=0, y=0, w=24, h=2)),
            row([]),
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

    def test_variable_dependency_cycles_are_rejected(self):
        for reference in ("$service", "${service:regex}", "[[service:regex]]", "$host"):
            with self.subTest(reference=reference):
                dashboard = copy.deepcopy(self.dashboard)
                dashboard["templating"]["list"][0]["query"] = reference
                with self.assertRaisesRegex(ValueError, "variable dependency cycle"):
                    validate_dashboard(dashboard)
        # A longer cycle must also be rejected.
        self.dashboard["templating"]["list"][0]["query"] = "$level"
        with self.assertRaisesRegex(ValueError, "variable dependency cycle"):
            validate_dashboard(self.dashboard)

    def test_acyclic_formatted_dependencies_and_globals_are_allowed(self):
        variables = self.dashboard["templating"]["list"]
        variables[0]["query"] = "label_values({time=\"$__from\"}, host)"
        variables[1]["query"] = 'label_values({host=~"${host:regex}"}, service)'
        variables[2]["query"] = 'label_values({service=~"[[service:regex]]"}, level)'
        # Declaration order does not determine the dependency order.
        self.dashboard["templating"]["list"].reverse()
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
