"""Check the provisioned dashboard offline using only the standard library."""

import argparse
import json
import math
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
MAX_PANELS = 256
LOKI = {"type": "loki", "uid": "logyard-loki"}
QUERY_MODES = {"stat": "instant", "bargauge": "instant",
               "logs": "range", "timeseries": "range"}
# Grafana/Loki globals supported in dashboard queries; unknown names fail closed.
BUILTINS = {
    "__interval", "__interval_ms", "__range", "__range_s", "__range_ms",
    "__auto", "__from", "__to",
}
VARIABLE = re.compile(
    r"\$(?:([A-Za-z_]\w*)|\{([A-Za-z_]\w*)(?::[^{}]+)?\})"
    r"|\[\[([A-Za-z_]\w*)(?::[^\[\]]+)?\]\]"
)
# One stream matcher: label, operator, and a double-quoted or backtick string.
MATCHER = re.compile(r'\s*([A-Za-z_]\w*)\s*(=~|!~|!=|=)\s*("(?:\\.|[^"\\])*"|`[^`]*`)\s*')
ERROR_PANELS = {"Errors (15m)", "Errors and Critical Logs"}


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


def reject_constant(value):
    """JSON has no NaN or infinity literals, even in unvalidated extensions."""
    raise ValueError("nonfinite JSON number")


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


def skip_string(expr, index):
    """Return the index after the double-quoted or backtick string at index."""
    quote = expr[index]
    index += 1
    while index < len(expr):
        if quote == '"' and expr[index] == "\\":
            index += 2
            continue
        if expr[index] == quote:
            return index + 1
        index += 1
    raise ValueError("unterminated string in query")


def stream_selectors(expr):
    """Brace selectors outside strings and # comments; ${var} is not a selector."""
    selectors = []
    index = 0
    while index < len(expr):
        char = expr[index]
        if char in '"`':
            index = skip_string(expr, index)
        elif char == "#":
            # LogQL line comment: quotes inside it open no string.
            end = expr.find("\n", index)
            index = len(expr) if end == -1 else end + 1
        elif char == "$" and expr.startswith("{", index + 1):
            end = expr.find("}", index + 2)
            require(end != -1, "unterminated variable reference in query")
            index = end + 1
        elif char == "{":
            start = index = index + 1
            while index < len(expr) and expr[index] != "}":
                if expr[index] in '"`':
                    index = skip_string(expr, index)
                else:
                    index += 1
            require(index < len(expr), "unterminated selector in query")
            selectors.append(expr[start:index])
            index += 1
        else:
            index += 1
    return selectors


def selector_matchers(selector, context):
    """Split one selector into (label, operator, string content) tuples."""
    matchers = []
    position = 0
    while True:
        match = MATCHER.match(selector, position)
        require(match, context + ": unparseable selector")
        label, operator, literal = match.groups()
        content = literal[1:-1]
        for reference in VARIABLE.finditer(content):
            name = next(group for group in reference.groups() if group is not None)
            if name in ("host", "service", "level"):
                require(re.fullmatch(selector_reference(name), reference.group()),
                        context + f": unsupported {name} selector formatter")
        matchers.append((label, operator, content))
        position = match.end()
        if position == len(selector):
            return matchers
        require(selector[position] == ",", context + ": unparseable selector")
        position += 1


def selector_reference(name):
    """Only default Loki interpolation and explicit regex formatting are allowed."""
    return rf'(?:\${name}|\$\{{{name}(?::regex)?\}}|\[\[{name}(?::regex)?\]\])'


def require_scope_matchers(matchers, required, context):
    for label, (operator, value) in required.items():
        require(any(name == label and op == operator and re.fullmatch(value, content)
                    for name, op, content in matchers),
                context + f": expected {label} filter")


def check_variable_query_scope(variable):
    """Check this dashboard's label_values wrapper and parent selectors only."""
    name = variable["name"]
    if name not in ("host", "service", "level"):
        return
    context = f"variable {name}: invalid query scope"
    query = variable["query"]
    prefix = re.match(r'\s*label_values\s*\(\s*', query)
    require(prefix, context + ": expected label_values(selector, label)")
    selectors = stream_selectors(query)
    require(len(selectors) == 1, context + ": expected one selector")
    selector = "{" + selectors[0] + "}"
    require(query.startswith(selector, prefix.end()),
            context + ": expected selector argument")
    suffix = query[prefix.end() + len(selector):]
    require(re.fullmatch(rf'\s*,\s*{name}\s*\)\s*', suffix),
            context + f": expected returned label {name}")
    required = {"source_type": ("=", r"journald")}
    if name in ("service", "level"):
        required["host"] = ("=~", selector_reference("host"))
    if name == "level":
        required["service"] = ("=~", selector_reference("service"))
    require_scope_matchers(selector_matchers(selectors[0], context), required, context)


def check_target_scope(panel, target):
    """Enforce this asset's label policy, without parsing LogQL expressions."""
    context = f"panel {panel['id']}: target {target['refId']}: invalid query scope"
    selectors = stream_selectors(target["expr"])
    require(selectors, context + ": missing selector")
    required = {"source_type": ("=", r"journald")}
    for label in ("host", "service", "level"):
        required[label] = ("=~", selector_reference(label))
    if panel["title"] in ERROR_PANELS:
        required["level"] = ("=~", r"error\|critical\|alert\|emerg")
    elif panel["title"] == "Log Volume by Level":
        del required["level"]
    if panel["title"] in ERROR_PANELS or panel["title"] == "Log Volume by Level":
        require("level" not in variable_references(target["expr"]),
                context + ": unexpected level variable")
    for selector in selectors:
        matchers = selector_matchers(selector, context)
        require_scope_matchers(matchers, required, context)
        if panel["title"] == "Log Volume by Level":
            require(all(name != "level" for name, _, _ in matchers),
                    context + ": volume must compare all levels")


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
        require(name not in BUILTINS, f"reserved variable name {name}")
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
        elif isinstance(value, float):
            require(math.isfinite(value), "nonfinite JSON number")

    panel_ids = set()

    def check_panels(panels):
        require(isinstance(panels, list), "panels must be a list")
        for panel in panels:
            require(isinstance(panel, dict), "panel must be an object")
            panel_id = panel.get("id")
            require(type(panel_id) is int and panel_id > 0 and panel_id not in panel_ids,
                    f"invalid or duplicate panel id {panel_id}")
            panel_ids.add(panel_id)
            require(len(panel_ids) <= MAX_PANELS,
                    f"panel count exceeds {MAX_PANELS}")
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
                    mode = target.get("queryType")
                    require(mode in ("instant", "range"),
                            f"panel {panel_id}: target queryType must be instant or range")
                    expected_mode = QUERY_MODES.get(panel["type"])
                    require(expected_mode is None or mode == expected_mode,
                            f"panel {panel_id}: {panel['type']} target queryType must be {expected_mode}")
                    check_target_scope(panel, target)
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
    active = set()
    visited = set()

    for name in sorted(names):
        if name in visited:
            continue
        active.add(name)
        stack = [(name, iter(sorted(dependencies[name])))]
        while stack:
            current, children = stack[-1]
            dependency = next(children, None)
            if dependency is None:
                stack.pop()
                active.remove(current)
                visited.add(current)
            elif dependency in active:
                path = [entry[0] for entry in stack] + [dependency]
                raise ValueError(f"variable dependency cycle: {' -> '.join(path)}")
            elif dependency not in visited:
                active.add(dependency)
                stack.append((dependency, iter(sorted(dependencies[dependency]))))

    for variable in variables:
        check_variable_query_scope(variable)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_PATH)
    path = parser.parse_args().path
    try:
        validate_dashboard(json.loads(read_asset(path),
                                      object_pairs_hook=unique_object,
                                      parse_constant=reject_constant))
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, RecursionError):
            error = "dashboard nesting is too deep"
        print(f"Invalid dashboard {path}: {error}", file=sys.stderr)
        return 1
    print(f"Valid dashboard: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
