"""Check the provisioned dashboard offline using only the standard library."""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys


DEFAULT_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy/grafana/dashboards/logyard/logyard-home.json"
)
MAX_NESTING = 64
MAX_ASSET_BYTES = 1024 * 1024
LOKI = {"type": "loki", "uid": "logyard-loki"}
# Grafana/Loki globals supported in dashboard queries; unknown names fail closed.
BUILTINS = {
    "__interval", "__interval_ms", "__range", "__range_s", "__range_ms",
    "__auto", "__from", "__to",
}
VARIABLE = re.compile(
    r"\$(?:([A-Za-z_]\w*)|\{([A-Za-z_]\w*)(?::[^{}]+)?\})"
    r"|\[\[([A-Za-z_]\w*)(?::[^\[\]]+)?\]\]"
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_object(pairs):
    """Reject ambiguous JSON objects before any structural validation."""
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON object key")
        result[key] = value
    return result


def read_asset(path):
    """Read one regular-file descriptor, bounded even if it grows after fstat."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode), "dashboard must be a regular file")
        message = f"dashboard exceeds {MAX_ASSET_BYTES}-byte limit"
        require(info.st_size <= MAX_ASSET_BYTES, message)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(MAX_ASSET_BYTES + 1)
        require(len(content) <= MAX_ASSET_BYTES, message)
        return content.decode("utf-8")
    finally:
        os.close(descriptor)


def variable_references(query):
    return {
        next(group for group in match.groups() if group is not None)
        for match in VARIABLE.finditer(query)
    }


def validate_dashboard(dashboard):
    """Validate this asset's shape and references, not the Grafana/LogQL schema."""
    require(isinstance(dashboard, dict), "dashboard must be an object")
    require(dashboard.get("uid") == "logyard-home", "expected uid logyard-home")
    require(isinstance(dashboard.get("title"), str) and dashboard["title"].strip(),
            "dashboard needs a title")
    version = dashboard.get("schemaVersion")
    require(type(version) is int and version > 0, "schemaVersion must be a positive integer")
    templating = dashboard.get("templating")
    require(isinstance(templating, dict), "templating must be an object")
    variables = templating.get("list")
    require(isinstance(variables, list), "templating.list must be a list")
    names = set()
    for variable in variables:
        require(isinstance(variable, dict), "variable must be an object")
        name = variable.get("name")
        require(isinstance(name, str) and re.fullmatch(r"[A-Za-z_]\w*", name),
                "variable needs a valid name")
        require(name not in names, f"duplicate variable {name}")
        names.add(name)
        require(variable.get("type") == "query", f"{name}: expected query variable")
        require(isinstance(variable.get("query"), str) and variable["query"].strip(),
                f"{name}: missing query")
        require(variable.get("datasource") == LOKI, f"{name}: expected Loki datasource {LOKI}")
    require({"host", "service", "level"} <= names, "host, service and level variables are required")

    # Bound every container, including extensions, before recursive panel checks.
    pending = [(dashboard, 1)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, (dict, list)):
            require(depth <= MAX_NESTING,
                    f"dashboard nesting exceeds {MAX_NESTING} container levels")
            children = value.values() if isinstance(value, dict) else value
            pending.extend((child, depth + 1) for child in children)
        elif isinstance(value, str):
            for name in variable_references(value):
                require(name in names or name in BUILTINS, f"undefined variable {name}")

    panel_ids = set()

    def check_panels(panels):
        require(isinstance(panels, list), "panels must be a list")
        for panel in panels:
            require(isinstance(panel, dict), "panel must be an object")
            panel_id = panel.get("id")
            require(type(panel_id) is int and panel_id > 0 and panel_id not in panel_ids,
                    f"invalid or duplicate panel id {panel_id}")
            panel_ids.add(panel_id)
            for key in ("title", "type"):
                require(isinstance(panel.get(key), str) and panel[key].strip(),
                        f"panel {panel_id}: missing {key}")
            grid = panel.get("gridPos")
            require(isinstance(grid, dict) and all(
                type(grid.get(key)) is int for key in ("x", "y", "w", "h")
            ), f"panel {panel['id']}: gridPos needs integer x, y, w, h")
            x, y, w, h = (grid[key] for key in ("x", "y", "w", "h"))
            require(x >= 0 and y >= 0 and w > 0 and h > 0 and x + w <= 24,
                    f"panel {panel['id']}: gridPos must fit the 24-column grid")
            if panel["type"] not in ("text", "row"):
                require(panel.get("datasource") == LOKI,
                        f"panel {panel_id}: expected Loki datasource {LOKI}")
                targets = panel.get("targets")
                require(isinstance(targets, list) and targets, f"panel {panel_id}: missing targets")
                ref_ids = set()
                for target in targets:
                    require(isinstance(target, dict), f"panel {panel_id}: target must be an object")
                    require(target.get("datasource") == LOKI,
                            f"panel {panel_id}: expected target datasource {LOKI}")
                    for key in ("expr", "refId"):
                        require(isinstance(target.get(key), str) and target[key].strip(),
                                f"panel {panel_id}: missing target {key}")
                    ref_id = target["refId"]
                    require(ref_id not in ref_ids,
                            f"panel {panel_id}: duplicate target refId {ref_id}")
                    ref_ids.add(ref_id)
                    require("hide" not in target or type(target["hide"]) is bool,
                            f"panel {panel_id}: target hide must be a boolean")
                require(any(not target.get("hide", False) for target in targets),
                        f"panel {panel_id}: needs at least one visible target")
            if "panels" in panel:
                check_panels(panel["panels"])

    panels = dashboard.get("panels")
    require(isinstance(panels, list) and panels, "dashboard panels must be a nonempty list")
    check_panels(panels)

    # Row headers occupy the visible grid. Collapsed rows' saved children do
    # not; expanded row children appear in the top-level panels list instead.
    rectangles = []
    for panel in panels:
        x, y, w, h = (panel["gridPos"][key] for key in ("x", "y", "w", "h"))
        for other_id, left, top, right, bottom in rectangles:
            require(not (x < right and left < x + w and y < bottom and top < y + h),
                    f"panels {other_id} and {panel['id']} overlap")
        rectangles.append((panel["id"], x, y, x + w, y + h))

    dependencies = {
        variable["name"]: variable_references(variable["query"]) & names
        for variable in variables
    }
    visiting = []
    visited = set()

    def visit(name):
        require(name not in visiting,
                f"variable dependency cycle: {' -> '.join(visiting + [name])}")
        if name in visited:
            return
        visiting.append(name)
        for dependency in sorted(dependencies[name]):
            visit(dependency)
        visiting.pop()
        visited.add(name)

    for name in sorted(names):
        visit(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_PATH)
    path = parser.parse_args().path
    try:
        validate_dashboard(json.loads(read_asset(path),
                                      object_pairs_hook=unique_object))
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, RecursionError):
            error = "dashboard nesting or dependency traversal is too deep"
        print(f"Invalid dashboard {path}: {error}", file=sys.stderr)
        return 1
    print(f"Valid dashboard: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
