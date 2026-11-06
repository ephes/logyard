"""Check the provisioned dashboard offline using only the standard library."""

import argparse
import json
from pathlib import Path
import re
import sys


DEFAULT_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy/grafana/dashboards/logyard/logyard-home.json"
)
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

    def check_references(value):
        if isinstance(value, dict):
            for child in value.values():
                check_references(child)
        elif isinstance(value, list):
            for child in value:
                check_references(child)
        elif isinstance(value, str):
            for match in VARIABLE.finditer(value):
                name = next(group for group in match.groups() if group is not None)
                require(name in names or name in BUILTINS, f"undefined variable {name}")

    check_references(dashboard)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_PATH)
    path = parser.parse_args().path
    try:
        validate_dashboard(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError) as error:
        print(f"Invalid dashboard {path}: {error}", file=sys.stderr)
        return 1
    print(f"Valid dashboard: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
