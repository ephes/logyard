"""Synthetic failures for the offline asset check."""

import copy
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch

from scripts import check_dashboard
from scripts.check_dashboard import DEFAULT_PATH, diagnostic_text, main, validate_dashboard


def row(children, collapsed=True):
    return dict(
        id=99, title="Row", type="row", panels=children, collapsed=collapsed,
        gridPos=dict(x=0, y=100, w=24, h=1),
    )


def required_panels():
    dashboard = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
    panels = [panel for panel in dashboard["panels"] if panel["id"] in (3, 7, 9)]
    # Keep the provisioned roles clear of the synthetic layouts under test.
    for panel in panels:
        panel["gridPos"]["y"] += 300
    return panels


class DashboardAssetTests(unittest.TestCase):
    def setUp(self):
        self.dashboard = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))

    def test_committed_dashboard(self):
        validate_dashboard(self.dashboard)

    def test_rejects_unknown_panel_types_including_rows(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                dashboard = copy.deepcopy(self.dashboard)
                panel = dashboard["panels"][1]
                panel["type"] = "stats"
                if nested:
                    dashboard["panels"][1] = row([panel])
                with self.assertRaisesRegex(ValueError, "panel 2: unsupported panel type stats"):
                    validate_dashboard(dashboard)

    def test_builtin_annotation_requires_grafana_datasource_object(self):
        annotation = self.dashboard["annotations"]["list"][0]
        for datasource in (
            {"type": "loki", "uid": "logyard-loki"},
            {"type": "grafana", "uid": "other"},
            {"type": "loki", "uid": "-- Grafana --"},
            "-- Grafana --",
            None,
        ):
            with self.subTest(datasource=datasource):
                annotation["datasource"] = datasource
                with self.assertRaisesRegex(ValueError, "annotation 0: expected built-in Grafana datasource"):
                    validate_dashboard(self.dashboard)

    def test_unvalidated_annotation_queries_must_be_explicitly_disabled(self):
        annotations = self.dashboard["annotations"]
        extra = dict(
            name="Synthetic", hide=True, enable=False,
            datasource={"type": "loki", "uid": "logyard-loki"},
        )
        annotations["list"].append(extra)
        annotations["list"][0]["description"] = "Annotations for $host"
        annotations["options"] = {"threshold": 1.25}
        validate_dashboard(self.dashboard)

        extra["enable"] = True
        with self.assertRaisesRegex(ValueError, "annotation 1: additional queries must be disabled"):
            validate_dashboard(self.dashboard)
        del extra["enable"]
        with self.assertRaisesRegex(ValueError, "annotation 1: additional queries must be disabled"):
            validate_dashboard(self.dashboard)

        # A visible toggle lets viewers enable the unvalidated query.
        extra["enable"] = False
        for hide in (False, None):
            with self.subTest(hide=hide):
                if hide is None:
                    extra.pop("hide", None)
                else:
                    extra["hide"] = hide
                with self.assertRaisesRegex(ValueError, "annotation 1: additional queries must hide their toggle"):
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

    def test_panel_ids_that_round_to_the_same_browser_number_are_rejected(self):
        first_id = 2**53
        second_id = first_id + 1
        self.assertEqual(float(first_id), float(second_id))
        self.dashboard["panels"][0]["id"] = first_id
        self.dashboard["panels"][1]["id"] = second_id

        with self.assertRaisesRegex(ValueError, "panel id"):
            validate_dashboard(self.dashboard)

    def test_schema_version_must_be_a_browser_safe_integer(self):
        self.dashboard["schemaVersion"] = 2**53 - 1
        validate_dashboard(self.dashboard)
        self.dashboard["schemaVersion"] += 1
        with self.assertRaisesRegex(ValueError, "schemaVersion"):
            validate_dashboard(self.dashboard)

    def test_only_collapsed_rows_can_store_children(self):
        for kind, collapsed, children in (
            ("text", True, True), ("text", True, False),
            ("row", False, True), ("row", None, True),
            ("row", 1, True), ("row", "true", True),
        ):
            with self.subTest(kind=kind, collapsed=collapsed, children=children):
                dashboard = copy.deepcopy(self.dashboard)
                child = copy.deepcopy(dashboard["panels"][1])
                parent = row([child] if children else [], collapsed=collapsed)
                parent["type"] = kind
                dashboard["panels"] = [parent]
                with self.assertRaisesRegex(ValueError, "children|collapsed"):
                    validate_dashboard(dashboard)
        for collapsed in (None, 0, "false"):
            dashboard = copy.deepcopy(self.dashboard)
            parent = row([], collapsed=collapsed)
            parent.pop("panels")
            dashboard["panels"] = [parent]
            with self.subTest(collapsed=collapsed):
                with self.assertRaisesRegex(ValueError, "collapsed"):
                    validate_dashboard(dashboard)

    def test_expanded_row_with_top_level_children(self):
        self.dashboard["panels"] = [
            row([], collapsed=False),
            dict(id=100, title="Child", type="text", gridPos=dict(x=0, y=101, w=24, h=2)),
        ] + required_panels()
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
                ] + required_panels()
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

    def test_saved_row_grid_must_have_a_browser_safe_bottom(self):
        limit = 2**53 - 1
        child = dict(id=limit, title="Child", type="text",
                     gridPos=dict(x=0, y=limit - 1, w=24, h=1))
        self.dashboard["panels"].append(row([child]))
        validate_dashboard(self.dashboard)

        for y, h in ((limit - 1, 3), (limit + 1, 1), (0, limit + 1)):
            with self.subTest(y=y, h=h):
                child["gridPos"].update(y=y, h=h)
                with self.assertRaisesRegex(ValueError, "gridPos"):
                    validate_dashboard(self.dashboard)

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

    def test_padded_target_refids_are_rejected_including_rows(self):
        for nested in (False, True):
            for padding in (" {}", "{}\t", "\u00a0{}\n"):
                with self.subTest(nested=nested, padding=padding):
                    dashboard = copy.deepcopy(self.dashboard)
                    panel = dashboard["panels"][1]
                    target = panel["targets"][0]
                    panel["targets"].append(dict(target, refId=padding.format(target["refId"])))
                    if nested:
                        dashboard["panels"][1] = row([panel])
                    with self.assertRaisesRegex(ValueError, "target refId must not have surrounding whitespace"):
                        validate_dashboard(dashboard)

    def test_target_refids_reject_controls_and_excess_length(self):
        target = self.dashboard["panels"][1]["targets"][0]
        for ref_id in ("inside\ncontrol", "A" * 65, "target\x7f", "targetä", "target B"):
            with self.subTest(ref_id=ref_id):
                target["refId"] = ref_id
                with self.assertRaisesRegex(ValueError, "panel 2: invalid target refId"):
                    validate_dashboard(self.dashboard)

    def test_target_refids_accept_distinct_ids_at_length_boundaries(self):
        targets = self.dashboard["panels"][1]["targets"]
        targets.append(dict(targets[0], refId="helper_2-" + "x" * 55))
        self.assertEqual(len(targets[-1]["refId"]), 64)
        validate_dashboard(self.dashboard)

    def test_target_refids_are_scoped_to_each_panel(self):
        panel = self.dashboard["panels"][1]
        other = copy.deepcopy(panel)
        other["id"] = 100
        self.dashboard["panels"].append(row([other]))
        extra = copy.deepcopy(panel["targets"][0])
        extra["refId"] = "B"
        panel["targets"].append(extra)
        validate_dashboard(self.dashboard)

    def test_duplicate_target_refid_diagnostic_is_built_only_on_failure(self):
        panel = self.dashboard["panels"][1]
        template = panel["targets"][0]
        duplicate_ref = "target-duplicate"
        refs = [f"target-{index}" for index in range(64)] + [duplicate_ref]
        panel["targets"] = [dict(template, refId=ref) for ref in refs]
        ref_set = set(refs)

        with patch("scripts.check_dashboard.diagnostic_text", wraps=diagnostic_text) as format_text:
            validate_dashboard(self.dashboard)
        # The scope check also uses each reference in its own diagnostic context.
        self.assertEqual(
            [call.args[0] for call in format_text.call_args_list if call.args[0] in ref_set],
            refs,
        )

        panel["targets"].append(panel["targets"][-1].copy())
        with self.assertRaises(ValueError) as error:
            validate_dashboard(self.dashboard)
        self.assertEqual(
            str(error.exception),
            f"panel {panel['id']}: duplicate target refId {diagnostic_text(duplicate_ref)}",
        )

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
        ] + required_panels()
        validate_dashboard(self.dashboard)

    def test_specialized_panel_ids_require_query_panels_including_rows(self):
        for panel_id, expected_type in ((3, "stat"), (7, "timeseries"), (9, "logs")):
            for panel_type in ("text", "row"):
                for nested in (False, True):
                    with self.subTest(panel_id=panel_id, panel_type=panel_type, nested=nested):
                        dashboard = copy.deepcopy(self.dashboard)
                        panels = dashboard["panels"]
                        index = next(i for i, panel in enumerate(panels) if panel["id"] == panel_id)
                        original = panels[index]
                        replacement = dict(
                            id=panel_id, title="Renamed", type=panel_type, gridPos=original["gridPos"],
                        )
                        if panel_type == "row":
                            replacement["collapsed"] = False
                        if nested:
                            panels.pop(index)
                            panels.append(row([replacement]))
                        else:
                            panels[index] = replacement
                        with self.assertRaisesRegex(
                            ValueError, f"panel {panel_id}: requires type {expected_type}$",
                        ):
                            validate_dashboard(dashboard)

    def test_specialized_panel_ids_require_their_query_modes(self):
        panel = next(panel for panel in self.dashboard["panels"] if panel["id"] == 3)
        for target in panel["targets"]:
            target["queryType"] = "range"
        with self.assertRaisesRegex(ValueError, "panel 3: stat target queryType must be instant"):
            validate_dashboard(self.dashboard)

    def test_specialized_panel_ids_reject_other_types_with_the_same_query_mode(self):
        for panel_id, expected_type, replacement in (
            (3, "stat", "bargauge"),
            (7, "timeseries", "logs"),
            (9, "logs", "timeseries"),
        ):
            with self.subTest(panel_id=panel_id, replacement=replacement):
                dashboard = copy.deepcopy(self.dashboard)
                panel = next(panel for panel in dashboard["panels"] if panel["id"] == panel_id)
                panel["type"] = replacement
                with self.assertRaisesRegex(ValueError, f"panel {panel_id}: requires type {expected_type}$"):
                    validate_dashboard(dashboard)

    def test_specialized_query_panels_can_be_renamed_and_nested(self):
        panels = self.dashboard["panels"]
        index = next(i for i, panel in enumerate(panels) if panel["id"] == 7)
        panel = panels[index]
        panel["title"] = "Renamed"
        validate_dashboard(self.dashboard)
        panels.pop(index)
        panels.append(row([panel]))
        validate_dashboard(self.dashboard)

    def test_misplaced_targets_are_rejected_on_nested_rows(self):
        dashboard = copy.deepcopy(self.dashboard)
        panels = dashboard["panels"]
        panel = panels.pop(1)
        inner = dict(row([]), id=100, targets=panel["targets"])
        panels.append(row([inner]))
        with self.assertRaisesRegex(ValueError, "panel 100: row must not declare targets"):
            validate_dashboard(dashboard)

        for targets in ({"A": panel["targets"][0]}, "x", 5, None, False, ()):
            with self.subTest(targets=targets):
                inner["targets"] = targets
                with self.assertRaisesRegex(ValueError,
                                            "panel 100: row must not declare targets"):
                    validate_dashboard(dashboard)

        inner["targets"] = []
        validate_dashboard(dashboard)
        validate_dashboard(self.dashboard)

    def test_text_panels_allow_only_omitted_or_empty_targets(self):
        panel = dict(id=100, title="Text", type="text", gridPos=dict(x=0, y=0, w=24, h=2))
        stale_targets = [dict(refId="A", expr='{source_type="synthetic"}')]
        for panels in ([panel], [row([panel])]):
            self.dashboard["panels"] = panels + required_panels()
            for targets in (stale_targets, {}, "", None, False, ()):
                with self.subTest(nested=panels[0] is not panel, targets=targets):
                    panel["targets"] = targets
                    with self.assertRaisesRegex(ValueError, "panel 100: text must not declare targets"):
                        validate_dashboard(self.dashboard)
            panel["targets"] = []
            validate_dashboard(self.dashboard)
            del panel["targets"]
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
            '{source_type="journald", host=~"${host}", '
            'service=~"[[service]]", level=~"${level}"}[$__interval]'
        )
        validate_dashboard(self.dashboard)
        self.dashboard["panels"][1]["targets"][0]["expr"] = "$__unknown"
        with self.assertRaisesRegex(ValueError, "undefined variable"):
            validate_dashboard(self.dashboard)

    def test_unknown_references_only_in_panel_comments_are_not_undefined(self):
        for reference in ("$missing", "${missing:regex}", "[[missing]]"):
            with self.subTest(reference=reference):
                dashboard = copy.deepcopy(self.dashboard)
                for panel in dashboard["panels"]:
                    for target in panel.get("targets", []):
                        target["expr"] += f' # {reference} "unbalanced'
                validate_dashboard(dashboard)

    def test_comment_references_still_count_where_grafana_interpolates(self):
        # Grafana interpolates $level inside comments as well; a value with a
        # newline would end the comment, so fixed-level panels refuse it there too.
        errors = next(index for index, panel in enumerate(self.dashboard["panels"])
                      if panel.get("title") == "Errors (15m)")
        dashboard = copy.deepcopy(self.dashboard)
        dashboard["panels"][errors]["targets"][0]["expr"] += " # $level"
        with self.assertRaisesRegex(ValueError, "unexpected level variable"):
            validate_dashboard(dashboard)

    def test_error_target_shares_the_full_query_reference_inventory(self):
        for comment, error in (
            ("$missing", None),
            ("$missing $level", "unexpected level variable"),
            ("$missing $level $region", "unsupported additional variable region"),
        ):
            with self.subTest(comment=comment):
                dashboard = copy.deepcopy(self.dashboard)
                variables = dashboard["templating"]["list"]
                variables.append(dict(variables[0], name="region", query="label_values(region)"))
                panel = next(panel for panel in dashboard["panels"] if panel["title"] == "Errors (15m)")
                target = panel["targets"][0]
                target["expr"] += f" # {comment}"
                query = target["expr"]
                with patch.object(check_dashboard, "variable_references",
                                  wraps=check_dashboard.variable_references) as references:
                    if error is None:
                        validate_dashboard(dashboard)
                    else:
                        with self.assertRaisesRegex(ValueError, error):
                            validate_dashboard(dashboard)
                self.assertEqual(
                    [scan for scan in references.call_args_list if scan.args == (query,)],
                    [call(query, logql=True), call(query)],
                )

    def test_variable_query_comments_break_the_label_values_wrapper(self):
        # Grafana matches label_values(...) on the raw text and otherwise falls
        # back to a label-names query, so no comment may follow or split it.
        variables = self.dashboard["templating"]["list"]
        host = next(index for index, variable in enumerate(variables)
                    if variable["name"] == "host")
        query = variables[host]["query"]
        selector_end = query.index("}") + 1
        for changed in (query + " # note",
                        query[:selector_end] + " # c\n" + query[selector_end:],
                        query[:-1] + " # c\n)"):
            with self.subTest(query=changed):
                dashboard = copy.deepcopy(self.dashboard)
                dashboard["templating"]["list"][host]["query"] = changed
                with self.assertRaisesRegex(ValueError, "invalid query scope"):
                    validate_dashboard(dashboard)

    def test_scoped_dropdowns_parse_each_selector_once(self):
        names = {"host", "service", "level"}
        for variable in self.dashboard["templating"]["list"]:
            name = variable["name"]
            selector, = check_dashboard.stream_selectors(variable["query"])
            missing_scope = selector.replace('source_type="journald"', 'source_type="other"')
            for content, label, error in (
                (selector, name, None),
                (missing_scope, name, "expected source_type filter"),
                (selector + ", malformed", "other", "unparseable selector"),
                (missing_scope, "other", "expected returned label"),
            ):
                with self.subTest(name=name, error=error):
                    query = f"label_values({{{content}}}, {label})"
                    dropdown = dict(variable, query=query)
                    with patch.object(check_dashboard, "selector_matchers",
                                      wraps=check_dashboard.selector_matchers) as parse:
                        if error is None:
                            check_dashboard.check_variable_query_scope(dropdown, names)
                        else:
                            with self.assertRaisesRegex(ValueError, error):
                                check_dashboard.check_variable_query_scope(dropdown, names)
                    self.assertEqual(parse.call_args_list, [
                        call(content, f"variable {name}: invalid query scope"),
                    ])

    def test_references_inside_variable_query_comments_are_dependencies(self):
        # Grafana resolves dependencies over the whole text: level already
        # depends on host, so a commented $level in host is a cycle.
        variables = self.dashboard["templating"]["list"]
        host = next(index for index, variable in enumerate(variables)
                    if variable["name"] == "host")
        variables[host]["query"] += " # $level"
        with self.assertRaisesRegex(ValueError,
                                    "variable dependency cycle: host -> level -> host"):
            validate_dashboard(self.dashboard)

    def test_active_references_after_comments_and_inside_strings_still_count(self):
        for suffix in (' # ignored\n | line_format "$missing"',
                       ' | line_format "# $missing"',
                       ' | line_format `# $missing`',
                       r' | line_format "escaped \" # $missing"'):
            with self.subTest(suffix=suffix):
                dashboard = copy.deepcopy(self.dashboard)
                dashboard["panels"][1]["targets"][0]["expr"] += suffix
                with self.assertRaisesRegex(ValueError, "undefined variable missing"):
                    validate_dashboard(dashboard)
        dashboard = copy.deepcopy(self.dashboard)
        dashboard["templating"]["list"][0]["query"] += ' # ignored\n $service'
        with self.assertRaisesRegex(ValueError, "variable dependency cycle"):
            validate_dashboard(dashboard)

    def test_metadata_keeps_references_after_hash(self):
        for key in ("title", "description", "query"):
            with self.subTest(key=key):
                dashboard = copy.deepcopy(self.dashboard)
                dashboard["extension"] = {key: "# $missing"}
                with self.assertRaisesRegex(ValueError, "undefined variable missing"):
                    validate_dashboard(dashboard)
        self.dashboard["title"] += " # $missing"
        with self.assertRaisesRegex(ValueError, "undefined variable missing"):
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
        variables[0]["query"] = 'label_values({source_type="journald", time="$__from"}, host)'
        variables[1]["query"] = variables[1]["query"].replace('"$host"', '"${host}"')
        variables[2]["query"] = variables[2]["query"].replace('"$service"', '"[[service]]"')
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

    def test_cli_rejects_each_missing_specialized_panel(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            for panel_id in (3, 7, 9):
                with self.subTest(panel_id=panel_id):
                    dashboard = dict(self.dashboard, panels=[
                        panel for panel in self.dashboard["panels"] if panel["id"] != panel_id
                    ])
                    path.write_text(json.dumps(dashboard), encoding="utf-8")
                    result = subprocess.run(
                        [sys.executable, "-m", "scripts.check_dashboard", str(path)],
                        cwd=DEFAULT_PATH.parents[4], capture_output=True, text=True, timeout=5,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(f"missing required panel {panel_id}", result.stderr)


def synthetic_dashboard():
    scopes = dict(
        host='source_type="journald"',
        service='source_type="journald", host=~"$host"',
        level='source_type="journald", host=~"$host", service=~"$service"',
    )
    return dict(
        uid="logyard-home", title="Synthetic", schemaVersion=1,
        templating=dict(list=[
            dict(name=name, type="query", includeAll=True, query=f"label_values({{{scope}}}, {name})",
                 datasource=dict(type="loki", uid="logyard-loki"))
            for name, scope in scopes.items()
        ]),
        panels=[dict(id=1, title="Text", type="text",
                     gridPos=dict(x=0, y=0, w=24, h=1))] + required_panels(),
    )


class DashboardVariableGraphTests(unittest.TestCase):
    LIMIT = 256
    BUILTIN_NAMES = (
        "__interval", "__interval_ms", "__range", "__range_s", "__range_ms",
        "__auto", "__from", "__to",
    )

    def test_declared_variable_name_length_limit(self):
        dashboard = synthetic_dashboard()
        variable = dict(
            name="_" + "a0" * 31 + "Z", type="query", query="literal",
            datasource=dict(type="loki", uid="logyard-loki"),
        )
        dashboard["templating"]["list"].append(variable)
        dashboard["description"] = "${" + variable["name"] + "}"
        validate_dashboard(dashboard)

        variable["name"] += "Z"
        dashboard["description"] = "${" + variable["name"] + "}"
        with self.assertRaisesRegex(ValueError, r"^variable name exceeds 64 characters$"):
            validate_dashboard(dashboard)

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
                dashboard["templating"]["list"].append(dict(
                    name="globals", type="query",
                    query=" ".join(template.format(name=name) for name in self.BUILTIN_NAMES),
                    datasource=dict(type="loki", uid="logyard-loki"),
                ))
                validate_dashboard(dashboard)

    def chain(self, length):
        dashboard = synthetic_dashboard()
        dashboard["templating"]["list"].extend(
            dict(name=f"v{index:04}", type="query",
                 query=f"$v{index + 1:04}" if index + 1 < length else "literal",
                 datasource=dict(type="loki", uid="logyard-loki"))
            for index in range(length)
        )
        return dashboard

    def test_variable_budget_precedes_query_scanning(self):
        dashboard = self.chain(2000)
        self.assertLess(len(json.dumps(dashboard).encode("utf-8")), 1024 * 1024)
        for scanner in ("check_reference_syntax", "variable_references"):
            with self.subTest(scanner=scanner), patch(
                f"scripts.check_dashboard.{scanner}",
                side_effect=AssertionError("query scanning began before admission"),
            ):
                with self.assertRaisesRegex(ValueError, r"^variable count exceeds 256$"):
                    validate_dashboard(dashboard)

    def test_long_acyclic_chain_is_valid_shallow_json_at_variable_limit(self):
        dashboard = self.chain(self.LIMIT - 3)  # The required dropdowns count too.
        content = json.dumps(dashboard).encode("utf-8")
        self.assertLess(len(content), 1024 * 1024)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_bytes(content)
            # The chain is longer than this recursion limit, so the dependency
            # walk must stay iterative.
            result = subprocess.run(
                [sys.executable, "-c",
                 "import sys; from scripts.check_dashboard import main; "
                 "sys.setrecursionlimit(128); sys.exit(main())", str(path)],
                cwd=DEFAULT_PATH.parents[4], capture_output=True, text=True, timeout=5,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_cycle_diagnostic_shows_only_cycle_members(self):
        for length, start, expected in (
            (9, "v0007", "v0007 -> v0008 -> v0007"),
            (13, "v0007", "v0007 -> v0008 -> v0009 -> v0010 -> ... -> v0007"),
            (253, "v0251", "v0251 -> v0252 -> v0251"),
        ):
            with self.subTest(length=length, start=start):
                dashboard = self.chain(length)
                dashboard["templating"]["list"][-1]["query"] = f"${start}"
                with self.assertRaises(ValueError) as error:
                    validate_dashboard(dashboard)
                self.assertEqual(str(error.exception), f"variable dependency cycle: {expected}")

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

    def test_diagnostic_previews_clip_only_between_complete_escapes(self):
        for char, unit in (("界", r"\u754c"), ("🧪", r"\U0001f9ea"),
                           ("\x1b", r"\x1b"), ("\t", r"\t"), ("\\", r"\\")):
            with self.subTest(unit=unit):
                limit = 2 * len(unit) + 4
                preview = diagnostic_text(char * 10, limit)
                self.assertEqual(preview, unit * 2 + "...")
                self.assertLessEqual(len(preview.encode("ascii")), limit)
                self.assertEqual(diagnostic_text(char * 2, 2 * len(unit)), unit * 2)
        for text in ("plain", "'", '"', "'\"\\"):
            self.assertEqual(diagnostic_text(text), ascii(text)[1:-1])
        self.assertEqual(diagnostic_text(1 << 128), "<large integer>")
        self.assertEqual(diagnostic_text({"nested": "value"}), "<invalid value>")

    def test_cli_diagnostic_clips_between_complete_escapes(self):
        prefix = "Invalid dashboard synthetic.json: "
        unit = r"\u754c"
        available = 1024 - len(prefix) - 1
        # Fill the budget except for one byte where the next escape would start.
        count, padding = divmod(available - len("...") - 1, len(unit))
        message = "x" * padding + "界" * (count + 10)
        with patch("scripts.check_dashboard.read_asset", side_effect=ValueError(message)):
            result = self.run_main("synthetic.json")
        self.assert_invalid(result, "...")
        self.assertEqual(result.stderr, prefix + "x" * padding + unit * count + "...\n")
        self.assertLessEqual(len(result.stderr.encode("ascii")), 1024)

    def test_diagnostics_are_bounded_ascii_lines_and_preserve_the_asset(self):
        controls = "\x1b[31m\x1b]52;c;synthetic\x07\n\r\t\x7f\u202e"
        cases = []
        for value in (controls, {"nested": [controls + "界" * 2000] * 40}):
            dashboard = synthetic_dashboard()
            dashboard["panels"][0]["id"] = value
            cases.append((dashboard, "panel id"))
        dashboard = synthetic_dashboard()
        dashboard["templating"]["list"][0]["name"] = "v" * 20000
        cases.append((dashboard, "variable name exceeds 64 characters"))
        dashboard = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
        target = dashboard["panels"][1]["targets"][0]
        target["refId"] = controls + "界" * 20000
        cases.append((copy.deepcopy(dashboard), "invalid target refId"))
        target["refId"] = "A"
        target["expr"] = '{host="unscoped"}'
        cases.append((dashboard, "invalid query scope"))
        for dashboard, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / (controls.replace("/", "_") + ".json")
                content = json.dumps(dashboard).encode("utf-8")
                path.write_bytes(content)
                result = subprocess.run(
                    [sys.executable, "-m", "scripts.check_dashboard", str(path)],
                    cwd=DEFAULT_PATH.parents[4], capture_output=True, text=True, timeout=5,
                )
                self.assert_invalid(result, diagnostic)
                self.assertLessEqual(len(result.stderr.encode("utf-8")), 1024)
                self.assertEqual(result.stderr.count("\n"), 1)
                self.assertTrue(all(32 <= ord(char) <= 126 for char in result.stderr[:-1]))
                self.assertEqual(path.read_bytes(), content)

    def test_invalid_panel_id_does_not_construct_a_rejected_value_repr(self):
        class ReprForbidden(dict):
            def __repr__(self):
                raise AssertionError("rejected panel id was formatted")

        dashboard = synthetic_dashboard()
        dashboard["panels"][0]["id"] = ReprForbidden(nested=["synthetic"] * 1000)
        with self.assertRaisesRegex(ValueError, "panel id"):
            validate_dashboard(dashboard)

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

    def test_cli_rejects_excess_decoded_nodes_below_byte_limit(self):
        dashboard = synthetic_dashboard()
        dashboard["extension"] = [0] * 65_536
        content = json.dumps(dashboard, separators=(",", ":")).encode("utf-8")
        self.assertLess(len(content), self.BYTE_LIMIT)
        self.assert_invalid(self.run_cli(content), "dashboard node count exceeds 65536")

    def test_rejects_lone_surrogates_in_extension_keys_and_values(self):
        diagnostic = "dashboard text contains a Unicode surrogate"
        for surrogate in ("\ud800", "\udfff"):
            text = f"extension{surrogate}text"
            for extension in ({text: None}, {"value": [text]}):
                dashboard = synthetic_dashboard()
                dashboard["extension"] = extension
                with self.subTest(extension=extension, entry="CLI"):
                    result = self.run_cli(json.dumps(dashboard, ensure_ascii=True).encode("ascii"))
                    self.assert_invalid(result, diagnostic)
                    self.assertTrue(result.stderr.endswith(f": {diagnostic}\n"), result.stderr)
                with self.subTest(extension=extension, entry="validator"):
                    with self.assertRaisesRegex(ValueError, f"^{diagnostic}$"):
                        validate_dashboard(dashboard)

    def test_cli_accepts_paired_surrogate_escapes_in_keys_and_values(self):
        dashboard = synthetic_dashboard()
        dashboard["extension"] = {"\U0001f642": ["\U0001f642"]}
        content = json.dumps(dashboard, ensure_ascii=True).encode("ascii")
        self.assertEqual(content.count(br"\ud83d\ude42"), 2)
        result = self.run_cli(content)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

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

    def test_small_asset_read_requests_only_its_size_plus_one(self):
        content = json.dumps(synthetic_dashboard()).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_bytes(content)
            with io.BytesIO(content) as stream, \
                    patch.object(stream, "read", wraps=stream.read) as read, \
                    patch("os.fdopen", return_value=stream):
                result = self.run_main(path)
                self.assertEqual(result.returncode, 0, result.stderr)
                read.assert_called_once_with(len(content) + 1)

    def test_actual_read_is_bounded_when_file_grows_after_stat(self):
        content = json.dumps(synthetic_dashboard()).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            for size, diagnostic in ((len(content), "dashboard changed while reading"),
                                     (self.BYTE_LIMIT, "byte limit")):
                with self.subTest(size=size):
                    path.write_bytes(content.ljust(size, b" "))

                    def append():
                        with path.open("ab") as writer:
                            writer.write(b"  ")

                    self.assert_invalid(self.read_with_mutation(path, append), diagnostic)

    def read_with_mutation(self, path, mutate):
        real_fdopen = os.fdopen

        @contextmanager
        def open_stream(*args, **kwargs):
            # Unbuffered so the second chunk actually observes an in-place edit.
            with real_fdopen(*args, **kwargs, buffering=0) as stream:
                real_read = stream.read

                def read(limit):
                    prefix = real_read(64)
                    mutate()
                    return prefix + real_read(limit - len(prefix))

                with patch.object(stream, "read", side_effect=read):
                    yield stream

        with patch("os.fdopen", side_effect=open_stream), \
                patch("os.close", wraps=os.close) as close:
            result = self.run_main(path)
            close.assert_called_once()
        return result

    def test_equal_size_overwrite_between_read_chunks_is_rejected(self):
        original = json.dumps(synthetic_dashboard()).encode("utf-8")
        replacement = original.replace(b'"title": "Synthetic"', b'"title": "Different"')
        replacement = replacement.replace(b'"title": "Text"', b'"title": "Next"')
        self.assertNotEqual(original, replacement)
        self.assertEqual(len(original), len(replacement))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_bytes(original)
            info = path.stat()

            def overwrite():
                path.write_bytes(replacement)
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))

            self.assert_invalid(self.read_with_mutation(path, overwrite),
                                "dashboard changed while reading")

    def test_generation_and_consumed_length_are_checked(self):
        content = json.dumps(synthetic_dashboard()).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_bytes(content)
            initial = path.stat()
            # Isolate every generation field, including ctime when an editor
            # restores mtime, and length disagreement despite stable metadata.
            for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"):
                final = SimpleNamespace(**{
                    key: getattr(initial, key) + (key == field)
                    for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
                })
                with self.subTest(field=field), \
                        patch("os.fstat", side_effect=[initial, final]):
                    self.assert_invalid(self.run_main(path), "dashboard changed while reading")
            with io.BytesIO(content[:-1]) as stream, patch("os.fdopen", return_value=stream):
                self.assert_invalid(self.run_main(path), "dashboard changed while reading")

    def test_atomic_replacement_before_initial_stat_keeps_opened_generation(self):
        original = json.dumps(synthetic_dashboard()).encode("utf-8")
        real_fstat = os.fstat
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            replacement = Path(directory) / "replacement.json"
            path.write_bytes(original)
            # Deliberately invalid new path: the checker must use the old fd.
            replacement.write_bytes(b"invalid replacement")

            def replace_then_stat(descriptor):
                if replacement.exists():
                    replacement.replace(path)
                return real_fstat(descriptor)

            with patch("os.fstat", side_effect=replace_then_stat), \
                    patch("os.open", wraps=os.open) as opener:
                result = self.run_main(path)
                opener.assert_called_once()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(path.read_bytes(), b"invalid replacement")
            self.assert_invalid(self.run_main(path), "Expecting value")

    def test_atomic_replacement_during_read_has_conservative_metadata_policy(self):
        content = json.dumps(synthetic_dashboard()).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            replacement = Path(directory) / "replacement.json"
            path.write_bytes(content)
            replacement.write_bytes(b"invalid replacement")
            with path.open("rb") as opened:
                before = os.fstat(opened.fileno())
                result = self.read_with_mutation(path, lambda: replacement.replace(path))
                after = os.fstat(opened.fileno())
            # POSIX unlink/rename may change the old descriptor's ctime. If so,
            # refuse conservatively; otherwise the coherent old bytes validate.
            if before.st_ctime_ns != after.st_ctime_ns:
                self.assert_invalid(result, "dashboard changed while reading")
            else:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Valid dashboard", result.stdout)


class DashboardPanelBudgetTests(unittest.TestCase):
    LIMIT = 256

    def dashboard_with_panels(self, count):
        dashboard = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
        prototype = dashboard["panels"][0]
        prototype.pop("options")  # Omit bulky operator notes in the compact fixture.
        dashboard["panels"] = required_panels()
        for index, panel in enumerate(dashboard["panels"]):
            panel["gridPos"] = dict(x=0, y=index, w=1, h=1)
        for index in range(len(dashboard["panels"]), count):
            panel = copy.deepcopy(prototype)
            # Keep synthetic text panels clear of the provisioned query roles.
            panel.update(id=1000 + index, gridPos=dict(x=0, y=index, w=1, h=1))
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

    def test_overlap_diagnostics_only_format_colliding_pairs(self):
        dashboard = self.dashboard_with_panels(self.LIMIT)
        with patch("scripts.check_dashboard.diagnostic_text", wraps=diagnostic_text) as format_text:
            validate_dashboard(dashboard)
        self.assertLessEqual(format_text.call_count, 2 * self.LIMIT)

        panels = dashboard["panels"]
        panels[-1]["gridPos"]["y"] = panels[-2]["gridPos"]["y"]
        with self.assertRaisesRegex(ValueError, r"^panels 1254 and 1255 overlap$"):
            validate_dashboard(dashboard)

    def test_tall_single_column_layout_limits_comparisons(self):
        comparisons = 0

        class MeasuredY(int):
            def __lt__(self, other):
                nonlocal comparisons
                comparisons += 1
                return super().__lt__(other)

        class MeasuredGrid(dict):
            def __getitem__(self, key):
                value = super().__getitem__(key)
                # Shape validation via dict.get still sees the original int.
                return MeasuredY(value) if key == "y" else value

        dashboard = self.dashboard_with_panels(self.LIMIT)
        for panel in dashboard["panels"]:
            panel["gridPos"] = MeasuredGrid(panel["gridPos"])
        validate_dashboard(dashboard)
        self.assertLessEqual(comparisons, 3 * self.LIMIT)

    def test_panel_budget_boundary_includes_saved_nested_children(self):
        for nested in (False, True):
            for count in (self.LIMIT, self.LIMIT + 1):
                with self.subTest(nested=nested, count=count):
                    dashboard = self.dashboard_with_panels(count)
                    if nested:
                        children = dashboard["panels"][:-2]
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
    def test_node_budget_is_inclusive_and_stops_the_structural_walk(self):
        limit = 65_536
        dashboard = synthetic_dashboard()
        dashboard["extension"] = {"entries": []}
        walk = check_dashboard._walk_dashboard
        base_nodes = sum(1 for _ in walk(dashboard))
        entries = dashboard["extension"]["entries"]
        entries.extend([None] * (limit - base_nodes))
        validate_dashboard(dashboard)

        entries.extend([None] * 10_000)
        visited = 0

        def count_nodes(dashboard):
            nonlocal visited
            for node in walk(dashboard):
                visited += 1
                yield node

        with patch.object(check_dashboard, "_walk_dashboard", side_effect=count_nodes) as walker, \
                patch.object(check_dashboard, "check_target_scope") as check_scope:
            with self.assertRaisesRegex(ValueError, "^dashboard node count exceeds 65536$"):
                validate_dashboard(dashboard)
            walker.assert_called_once_with(dashboard)
            check_scope.assert_not_called()
        self.assertEqual(visited, limit + 1)

    def test_wide_extensions_use_bounded_traversal_memory(self):
        dashboard = synthetic_dashboard()
        dashboard["extension"] = {
            "entries": ["literal"] * 20_000,
            "options": {str(index): 0 for index in range(20_000)},
        }
        # Measure only validation. Neither pending siblings nor strings visited
        # for later reference checking should need storage proportional to width.
        tracemalloc.start()
        try:
            validate_dashboard(dashboard)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 256 * 1024)

    def test_extension_diagnostic_order(self):
        dashboard = synthetic_dashboard()
        dashboard["extension"] = {"options": ["\ud800", float("inf"), "$last"]}
        with self.assertRaisesRegex(ValueError, "^nonfinite JSON number$"):
            validate_dashboard(dashboard)
        dashboard["extension"]["options"] = ["$first", "$last"]
        with self.assertRaisesRegex(ValueError, "^undefined variable last$"):
            validate_dashboard(dashboard)

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
    def query_children(self):
        prototype = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))["panels"][1]
        children = [copy.deepcopy(prototype) for _ in range(2)]
        for index, child in enumerate(children):
            child.update(id=100 + index, gridPos=dict(x=0, y=0, w=6, h=4))
        return children

    def test_empty_expanded_rows_skip_layout_pass(self):
        empty_row_count = 64
        iterations = 0

        class EmptyChildren(list):
            def __iter__(self):
                nonlocal iterations
                iterations += 1
                return super().__iter__()

        dashboard = synthetic_dashboard()
        for index in range(empty_row_count):
            dashboard["panels"].append(dict(
                row(EmptyChildren(), collapsed=False), id=200 + index,
                gridPos=dict(x=0, y=200 + index, w=24, h=1),
            ))
        saved_children = self.query_children()
        saved_children[1]["gridPos"]["y"] = 4
        dashboard["panels"].append(row(saved_children))
        validate_dashboard(dashboard)
        self.assertLessEqual(iterations, empty_row_count, "empty rows need no layout pass")

        saved_children[1]["gridPos"]["y"] = 3
        with self.assertRaisesRegex(ValueError, r"^panels 100 and 101 overlap$"):
            validate_dashboard(dashboard)

    def test_saved_siblings_cannot_overlap_including_nested_rows(self):
        for nested in (False, True):
            for x, y in ((0, 0), (5, 3)):
                with self.subTest(nested=nested, x=x, y=y):
                    dashboard = synthetic_dashboard()
                    children = self.query_children()
                    children[1]["gridPos"].update(x=x, y=y)
                    parent = row(children)
                    if nested:
                        parent = dict(row([parent]), id=98)
                    dashboard["panels"] = [parent]
                    with self.assertRaisesRegex(ValueError, "panels 100 and 101 overlap"):
                        validate_dashboard(dashboard)

    def test_saved_siblings_can_be_disjoint_or_touch_edges(self):
        for x, y in ((6, 0), (0, 4), (7, 5)):
            with self.subTest(x=x, y=y):
                dashboard = synthetic_dashboard()
                children = self.query_children()
                children[1]["gridPos"].update(x=x, y=y)
                dashboard["panels"] = [row(children)] + required_panels()
                validate_dashboard(dashboard)

    def test_saved_siblings_use_grid_columns_and_report_ids_in_sibling_order(self):
        dashboard = synthetic_dashboard()
        lower, upper = self.query_children()
        lower["gridPos"].update(x=23, y=4, w=1)
        upper["gridPos"].update(x=22, y=0, w=2)
        dashboard["panels"] = [row([lower, upper])] + required_panels()
        validate_dashboard(dashboard)

        lower["gridPos"]["y"] = 3
        with self.assertRaisesRegex(ValueError, r"^panels 100 and 101 overlap$"):
            validate_dashboard(dashboard)

    def test_saved_siblings_are_scoped_to_their_own_row(self):
        dashboard = synthetic_dashboard()
        first, second = self.query_children()
        # Both children overlap the visible text panel and each other, but
        # belong to separate saved layouts with disjoint visible headers.
        other_row = dict(row([second]), id=98, gridPos=dict(x=0, y=102, w=24, h=1))
        dashboard["panels"].extend([row([first]), other_row])
        validate_dashboard(dashboard)

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
        dashboard["panels"] = [outer] + required_panels()
        validate_dashboard(dashboard)


if __name__ == "__main__":
    unittest.main()
