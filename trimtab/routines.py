"""Render routine specs from templates with the instance's values (stage S2 spec, section 4).

Rendering changes nothing: it prints a spec. Creating a routine stays a session
step the operator approves: the routine then writes on the operator's authority
on a schedule, and a side effect needs a preview and a confirmation (ADR 0005).
Values the instance does not hold yet are passed with --set, so no ID is ever
written into git.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from string import Template

from trimtab import roots

NAME = re.compile(r"[a-z0-9-]+")
PROMPT_KEY = "prompt_json"


class RenderError(Exception):
    """A template is missing or malformed, or values for its placeholders are."""


def _templates() -> Path:
    return roots.code_root() / "routines" / "templates"


def values(instance: Path, overrides: Mapping[str, str]) -> dict[str, str]:
    """The instance's values (its repository, from instance.json), then `overrides` over them."""
    return {"instance_repo": roots.instance_repo(instance), **overrides}


def _fill(path: Path, given: Mapping[str, str]) -> str:
    template = Template(path.read_text(encoding="utf-8"))
    if not hasattr(template, "get_identifiers"):  # added in Python 3.11
        raise RenderError("rendering routine templates needs Python 3.11 or later")
    if not template.is_valid():
        raise RenderError(f"{path.name}: a `$` that is not a placeholder (write `$$` for a literal one)")
    missing = sorted(set(template.get_identifiers()) - set(given))
    if missing:
        raise RenderError(f"{path.name}: no value for {', '.join(missing)} (pass --set KEY=VALUE)")
    return template.substitute(given)


def render(name: str, given: Mapping[str, str]) -> dict:
    """The routine's parsed create body, with its prompt embedded. Never half-rendered."""
    if not NAME.fullmatch(name):
        raise RenderError(f"{name!r} is not a routine name (lower-case letters, digits and hyphens)")
    if PROMPT_KEY in given:  # render() fills it with the prompt; a value passed in would be dropped silently
        raise RenderError(f"{PROMPT_KEY} is reserved for the rendered prompt and cannot be set")
    spec = _templates() / f"{name}.json.tmpl"
    prompt = _templates() / f"{name}.prompt.md.tmpl"
    if not spec.is_file() or not prompt.is_file():
        raise RenderError(f"no templates for routine {name!r} under routines/templates/")
    text = _fill(prompt, given)
    # Each value lands inside a JSON string, so it is escaped as one: a quote in
    # a value cannot close the string and add keys (say, "enabled": true).
    escaped = {key: json.dumps(value)[1:-1] for key, value in given.items()}
    rendered = _fill(spec, {**escaped, PROMPT_KEY: json.dumps(text)})
    try:
        return json.loads(rendered)
    except json.JSONDecodeError as err:
        raise RenderError(f"{spec.name} does not render to JSON: {err}") from err
