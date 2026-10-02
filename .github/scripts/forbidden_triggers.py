"""Fail if any workflow is triggered by pull_request_target.

That trigger runs with the repository's secrets on code a fork's pull request
controls, which this public repository never allows: a fork's pull request must
never see the review secret.
Triggers are read by parsing the YAML, so the check cannot match its own text
and catches every form: a mapping key, an inline string and an inline list.
"""

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("forbidden_triggers: PyYAML is required; failing closed")

FORBIDDEN = "pull_request_target"


def triggers(doc: dict) -> set[str]:
    on = doc.get("on", doc.get(True))  # YAML 1.1 reads a bare `on` key as True
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(t) for t in on}
    if isinstance(on, dict):
        return {str(k) for k in on}
    return set()


def main(directory: str) -> int:
    root = Path(directory)
    files = sorted([*root.glob("*.yml"), *root.glob("*.yaml")])
    if not files:
        print(f"::error::no workflow files under {root}; failing closed")
        return 1
    problems = []
    for path in files:
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            problems.append(f"{path.name}: not valid YAML")
            continue
        if not isinstance(doc, dict):
            problems.append(f"{path.name}: not a workflow mapping")
        elif FORBIDDEN in triggers(doc):
            problems.append(f"{path.name}: triggered by {FORBIDDEN}, which runs fork code with secrets")
    for problem in problems:
        print(f"::error::{problem}")
    print(f"forbidden_triggers: {len(files)} workflow(s), {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ".github/workflows"))
