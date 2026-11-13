"""Synthetic failures for the offline asset check."""

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.check_dashboard import DEFAULT_PATH, main, validate_dashboard


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


def synthetic_dashboard():
    return dict(
        uid="logyard-home", title="Synthetic", schemaVersion=1,
        templating=dict(list=[
            dict(name=name, type="query", query=f"label_values({name})",
                 datasource=dict(type="loki", uid="logyard-loki"))
            for name in ("host", "service", "level")
        ]),
        panels=[dict(id=1, title="Text", type="text",
                     gridPos=dict(x=0, y=0, w=24, h=1))],
    )


class DashboardVariableGraphTests(unittest.TestCase):
    BUILTIN_NAMES = (
        "__interval", "__interval_ms", "__range", "__range_s", "__range_ms",
        "__auto", "__from", "__to",
    )

    def test_builtin_names_are_reserved_for_definitions(self):
        for name in self.BUILTIN_NAMES:
            with self.subTest(name=name):
                dashboard = synthetic_dashboard()
                dashboard["templating"]["list"].append(dict(
                    name=name, type="query", query="literal",
                    datasource=dict(type="loki", uid="logyard-loki"),
                ))
                with self.assertRaisesRegex(ValueError, f"reserved variable name {name}$"):
                    validate_dashboard(dashboard)

    def test_all_builtin_references_remain_allowed(self):
        for template in ("${name}", "${{{name}:regex}}", "[[{name}]]"):
            with self.subTest(template=template):
                dashboard = synthetic_dashboard()
                dashboard["templating"]["list"][0]["query"] = " ".join(
                    template.format(name=name) for name in self.BUILTIN_NAMES
                )
                validate_dashboard(dashboard)

    def chain(self, length=1100):
        dashboard = synthetic_dashboard()
        dashboard["templating"]["list"].extend(
            dict(name=f"v{index:04}", type="query",
                 query=f"$v{index + 1:04}" if index + 1 < length else "literal",
                 datasource=dict(type="loki", uid="logyard-loki"))
            for index in range(length)
        )
        return dashboard

    def test_long_acyclic_chain_is_valid_shallow_json(self):
        dashboard = self.chain()
        content = json.dumps(dashboard).encode("utf-8")
        self.assertLess(len(content), 1024 * 1024)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_bytes(content)
            result = subprocess.run(
                [sys.executable, "-m", "scripts.check_dashboard", str(path)],
                cwd=DEFAULT_PATH.parents[4], capture_output=True, text=True, timeout=5,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_deep_tail_cycle_retains_stable_diagnostic(self):
        dashboard = self.chain()
        dashboard["templating"]["list"][-1]["query"] = "$v1098"
        expected = "variable dependency cycle: " + " -> ".join(
            [f"v{index:04}" for index in range(1100)] + ["v1098"]
        )
        with self.assertRaises(ValueError) as error:
            validate_dashboard(dashboard)
        self.assertEqual(str(error.exception), expected)

    def test_shared_subgraphs_are_not_cycles(self):
        dashboard = synthetic_dashboard()
        variables = dashboard["templating"]["list"]
        for index in range(30):
            for side in ("a", "b"):
                variables.append(dict(
                    name=f"v{index:02}{side}", type="query",
                    query=f"$v{index + 1:02}a $v{index + 1:02}b" if index < 29 else "literal",
                    datasource=dict(type="loki", uid="logyard-loki"),
                ))
        validate_dashboard(dashboard)
        variables[-1]["query"] = "$v00a"
        with self.assertRaisesRegex(ValueError, "variable dependency cycle"):
            validate_dashboard(dashboard)


class DashboardAdmissionTests(unittest.TestCase):
    BYTE_LIMIT = 1024 * 1024

    def run_main(self, path):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["check_dashboard", str(path)]), \
                redirect_stdout(output), redirect_stderr(errors):
            code = main()
        return subprocess.CompletedProcess([], code, output.getvalue(), errors.getvalue())

    def run_cli(self, content):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_bytes(content)
            return subprocess.run(
                [sys.executable, "-m", "scripts.check_dashboard", str(path)],
                cwd=DEFAULT_PATH.parents[4], capture_output=True, text=True, timeout=5,
            )

    def assert_invalid(self, result, diagnostic):
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Invalid dashboard", result.stderr)
        self.assertIn(diagnostic, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_nonfinite_numbers_are_rejected_everywhere(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            for location in ("extension", "annotations"):
                with self.subTest(value=value, location=location):
                    dashboard = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
                    dashboard[location] = {"options": [dict(value=value)]}
                    with self.assertRaisesRegex(ValueError, "nonfinite JSON number"):
                        validate_dashboard(dashboard)
                    self.assert_invalid(
                        self.run_cli(json.dumps(dashboard).encode("utf-8")),
                        "nonfinite JSON number",
                    )

    def test_cli_refuses_overflowing_json_number(self):
        dashboard = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
        content = json.dumps(dashboard)[:-1] + ',"extension":1e999}'
        self.assert_invalid(self.run_cli(content.encode("utf-8")), "nonfinite JSON number")

    def test_finite_numbers_and_constant_strings_remain_valid(self):
        dashboard = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
        dashboard["extension"] = [0, -1, 1.25, 1e308, 10**100, "NaN", "Infinity"]
        validate_dashboard(dashboard)
        result = self.run_cli(json.dumps(dashboard).encode("utf-8"))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_rejects_duplicate_json_keys_at_any_depth(self):
        dashboard = synthetic_dashboard()
        panel = dashboard["panels"][0]
        panel.update(type="logs", datasource=dict(type="loki", uid="logyard-loki"),
                     targets=[dict(expr="synthetic query", refId="A",
                                   datasource=dict(type="loki", uid="logyard-loki"))])
        dashboard["extension"] = {"note": "same"}
        content = json.dumps(dashboard)
        for original, replacement in (
            ('"uid": "logyard-home"', '"uid": "wrong", "uid": "logyard-home"'),
            ('"uid": "logyard-loki"', '"uid": "wrong", "uid": "logyard-loki"'),
            ('"x": 0', '"x": -1, "x": 0'),
            ('"expr": "synthetic query"', '"expr": "", "expr": "synthetic query"'),
            ('"note": "same"', '"note": "same", "note": "same"'),
            ('"uid": "logyard-home"', '"\\u0075id": "wrong", "uid": "logyard-home"'),
        ):
            with self.subTest(replacement=replacement):
                mutated = content.replace(original, replacement, 1)
                self.assertNotEqual(mutated, content)
                self.assert_invalid(self.run_cli(mutated.encode("utf-8")),
                                    "duplicate JSON object key")

    def test_cli_accepts_distinct_keys_and_reused_keys_in_separate_objects(self):
        dashboard = synthetic_dashboard()
        dashboard["extension"] = {"note": "same", "child": {"note": "other"}}
        result = self.run_cli(json.dumps(dashboard).encode("utf-8"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("Valid dashboard", result.stdout)

    def test_cli_byte_limit_includes_exact_boundary(self):
        content = json.dumps(synthetic_dashboard()).encode("utf-8")
        exact = content + b" " * (self.BYTE_LIMIT - len(content))
        result = self.run_cli(exact)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_invalid(self.run_cli(exact + b" "), "byte limit")

    def test_cli_limit_counts_utf8_bytes(self):
        dashboard = synthetic_dashboard()
        dashboard["extension"] = "\u00e9" * (self.BYTE_LIMIT // 2)
        content = json.dumps(dashboard, ensure_ascii=False)
        self.assertLess(len(content), self.BYTE_LIMIT)
        self.assertGreater(len(content.encode("utf-8")), self.BYTE_LIMIT)
        self.assert_invalid(self.run_cli(content.encode("utf-8")), "byte limit")

    def test_cli_rejects_invalid_utf8(self):
        self.assert_invalid(self.run_cli(b"\xff"), "utf-8")

    def test_nonregular_files_are_rejected_before_reading(self):
        # Synthetic metadata avoids platform-specific devices, FIFOs or sockets.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_text(json.dumps(synthetic_dashboard()), encoding="utf-8")
            for mode in (stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK,
                         stat.S_IFSOCK, stat.S_IFDIR):
                with self.subTest(mode=mode), \
                        patch("os.fstat",
                              return_value=SimpleNamespace(st_mode=mode, st_size=0)), \
                        patch("os.fdopen") as reader, \
                        patch("os.close", wraps=os.close) as close:
                    self.assert_invalid(self.run_main(path), "regular file")
                    reader.assert_not_called()
                    close.assert_called_once()

    def test_open_is_single_and_nonblocking(self):
        # Inject the flag so this assertion also works on non-POSIX platforms.
        nonblocking = 1 << 29
        real_open = os.open
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_text(json.dumps(synthetic_dashboard()), encoding="utf-8")
            with patch.object(os, "O_NONBLOCK", nonblocking, create=True), \
                    patch("os.open",
                          side_effect=lambda path, flags: real_open(path, flags & ~nonblocking)) as opener:
                result = self.run_main(path)
                self.assertEqual(result.returncode, 0, result.stderr)
                opener.assert_called_once()
                self.assertTrue(opener.call_args.args[1] & nonblocking)

    def test_stat_size_refuses_oversized_asset_without_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_text(json.dumps(synthetic_dashboard()), encoding="utf-8")
            with patch("os.fstat", return_value=SimpleNamespace(
                    st_mode=stat.S_IFREG, st_size=self.BYTE_LIMIT + 1)), \
                    patch("os.fdopen") as reader, \
                    patch("os.close", wraps=os.close) as close:
                self.assert_invalid(self.run_main(path), "byte limit")
                reader.assert_not_called()
                close.assert_called_once()

    def test_actual_read_is_bounded_when_file_grows_after_stat(self):
        content = json.dumps(synthetic_dashboard()).encode("utf-8")
        oversized = content + b" " * (self.BYTE_LIMIT + 100 - len(content))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_bytes(content)
            # The descriptor reports the initial size but now supplies more data.
            with io.BytesIO(oversized) as stream, \
                    patch.object(stream, "read", wraps=stream.read) as read, \
                    patch("os.fdopen", return_value=stream), \
                    patch("os.close", wraps=os.close) as close:
                self.assert_invalid(self.run_main(path), "byte limit")
                read.assert_called_once_with(self.BYTE_LIMIT + 1)
                close.assert_called_once()


class DashboardPanelBudgetTests(unittest.TestCase):
    LIMIT = 256

    def dashboard_with_panels(self, count):
        dashboard = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
        prototype = dashboard["panels"][0]
        prototype.pop("options")  # Omit bulky operator notes in the compact fixture.
        dashboard["panels"] = []
        for index in range(count):
            panel = copy.deepcopy(prototype)
            panel.update(id=index + 1, gridPos=dict(x=0, y=index, w=1, h=1))
            dashboard["panels"].append(panel)
        return dashboard

    def test_panel_budget_precedes_any_overlap_comparisons(self):
        from scripts.check_dashboard import require

        dashboard = self.dashboard_with_panels(2000)
        self.assertLess(len(json.dumps(dashboard).encode("utf-8")), 1024 * 1024)

        def observe(condition, message):
            self.assertNotIn(" overlap", message, "overlap work began before admission")
            require(condition, message)

        with patch("scripts.check_dashboard.require", side_effect=observe):
            with self.assertRaisesRegex(ValueError, "panel count exceeds 256"):
                validate_dashboard(dashboard)

    def test_panel_budget_boundary_includes_saved_nested_children(self):
        for nested in (False, True):
            for count in (self.LIMIT, self.LIMIT + 1):
                with self.subTest(nested=nested, count=count):
                    dashboard = self.dashboard_with_panels(count)
                    if nested:
                        children = dashboard["panels"][2:]
                        inner = row(children)
                        inner["id"] = 2
                        outer = row([inner])
                        outer["id"] = 1
                        dashboard["panels"] = [outer]
                    if count == self.LIMIT:
                        validate_dashboard(dashboard)
                    else:
                        with self.assertRaisesRegex(ValueError, "panel count exceeds 256"):
                            validate_dashboard(dashboard)


class DashboardDepthTests(unittest.TestCase):
    def test_extension_container_depth_boundary(self):
        # The root object is depth 1; scalar values do not add container depth.
        for kind in (dict, list):
            for depth in (64, 65):
                with self.subTest(kind=kind, depth=depth):
                    dashboard = synthetic_dashboard()
                    extension = "literal"
                    for _ in range(depth - 1):
                        extension = {"child": extension} if kind is dict else [extension]
                    dashboard["extension"] = extension
                    if depth == 64:
                        validate_dashboard(dashboard)
                    else:
                        with self.assertRaisesRegex(ValueError, "nesting.*64"):
                            validate_dashboard(dashboard)

    def test_cli_rejects_deep_extensions_and_rows_without_traceback(self):
        extension = "literal"
        for _ in range(80):
            extension = {"child": [extension]}
        dashboard = synthetic_dashboard()
        dashboard["extension"] = extension
        nested_rows = synthetic_dashboard()
        for panel_id in range(2, 82):
            parent = row(nested_rows["panels"])
            parent["id"] = panel_id
            nested_rows["panels"] = [parent]
        # Construct raw JSON so the fixture encoder cannot hit its own limit.
        decoding_depth = 10000
        contents = (
            json.dumps(dashboard), json.dumps(nested_rows),
            json.dumps(synthetic_dashboard())[:-1] + ',"extension":'
            + '[' * decoding_depth + '0' + ']' * decoding_depth + '}',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            for content in contents:
                path.write_text(content, encoding="utf-8")
                result = subprocess.run(
                    [sys.executable, "-m", "scripts.check_dashboard", str(path)],
                    cwd=DEFAULT_PATH.parents[4], capture_output=True, text=True,
                )
                with self.subTest(prefix=content[:40]):
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("Invalid dashboard", result.stderr)
                    self.assertIn("nesting", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertEqual(result.stdout, "")

    def test_cli_normalizes_decoding_and_validation_recursion(self):
        from contextlib import redirect_stderr
        import io
        from unittest.mock import patch
        from scripts.check_dashboard import main

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_text(json.dumps(synthetic_dashboard()), encoding="utf-8")
            for target in ("json.loads", "validate_dashboard"):
                with self.subTest(target=target):
                    errors = io.StringIO()
                    with patch.object(sys, "argv", ["check_dashboard", str(path)]), \
                            patch(f"scripts.check_dashboard.{target}",
                                  side_effect=RecursionError("implementation detail")), \
                            redirect_stderr(errors):
                        self.assertEqual(main(), 1)
                    self.assertIn("Invalid dashboard", errors.getvalue())
                    self.assertNotIn("implementation detail", errors.getvalue())
                    self.assertNotIn("Traceback", errors.getvalue())


class DashboardChildGridTests(unittest.TestCase):
    def test_saved_child_grid_shapes_are_validated_at_each_level(self):
        valid = dict(x=0, y=100, w=24, h=1)
        invalid_grids = [None, {}, dict(x=0, y=100, w=24)]
        for key, value in (
            ("x", -1), ("y", -1), ("w", -2), ("h", 0),
            ("x", True), ("y", 1.5), ("w", "6"), ("h", False),
            ("w", 25), ("x", 1),
        ):
            invalid_grids.append(dict(valid, **{key: value}))
        for nested in (False, True):
            for grid in invalid_grids:
                with self.subTest(nested=nested, grid=grid):
                    dashboard = synthetic_dashboard()
                    child = dict(id=100, title="Child", type="text", gridPos=grid)
                    parent = row([child])
                    if nested:
                        outer = row([parent])
                        outer["id"] = 98
                        parent = outer
                    dashboard["panels"] = [parent]
                    with self.assertRaisesRegex(ValueError, "panel 100: gridPos"):
                        validate_dashboard(dashboard)
        dashboard = synthetic_dashboard()
        child = dict(id=100, title="Child", type="text")
        dashboard["panels"] = [row([child])]
        with self.assertRaisesRegex(ValueError, "panel 100: gridPos"):
            validate_dashboard(dashboard)

    def test_saved_nested_child_grids_may_overlap_row_headers(self):
        dashboard = synthetic_dashboard()
        child = dict(id=100, title="Child", type="text",
                     gridPos=dict(x=0, y=100, w=24, h=10))
        inner = row([child])
        outer = row([inner])
        outer["id"] = 98
        dashboard["panels"] = [outer]
        validate_dashboard(dashboard)


if __name__ == "__main__":
    unittest.main()
