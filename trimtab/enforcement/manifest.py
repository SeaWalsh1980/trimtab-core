"""This base's enforcement manifest: which hook and tests enforce each row it owns (ADR 0063 §2). Pure.

The manifest says how a row is enforced, never what the row is: the row's text and owner come
from the doctrine record, and an entry names the SHA-256 of the text it was written for. Each
entry also names its decision function and the value that function returns to allow, so the
mutation test can replace the function and show that the row's tests then fail.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from trimtab.enforcement.record import BASE, BOTH, Row

PATH = "trimtab/enforcement/manifest.json"
SCHEMA_VERSION = 1
SHA256 = re.compile(r"[0-9a-f]{64}")
HOOK_FILE = re.compile(r"[a-z][a-z0-9-]*\.sh")
EVENTS = {"PreToolUse", "PostToolUse", "SessionStart"}
TEST_ID = re.compile(r"test_[a-z0-9_]+(\.[A-Za-z_][A-Za-z0-9_]*){1,2}")
FUNCTION = re.compile(r"trimtab(\.[a-z_][a-z0-9_]*)+:[a-z_][a-z0-9_]*")
ROW_ID = re.compile(r"E[1-9][0-9]?")
ENTRY_KEYS = {"row_sha256", "hooks", "tests", "decision"}
HOOK_KEYS = {"file", "event", "matcher"}
DECISION_KEYS = {"function", "allow"}


class ManifestError(Exception):
    """The manifest breaks its schema, so no row it maps can be shown."""


@dataclass(frozen=True)
class Hook:
    file: str
    event: str
    matcher: str


@dataclass(frozen=True)
class Entry:
    row_sha256: str
    hooks: tuple[Hook, ...]
    tests: tuple[str, ...]  # unittest ids under tests/trimtab, without the package
    function: str  # module:function, the decision the mutation test replaces
    allow: object  # what that function returns to allow


def _keys(obj, allowed: set[str], where: str) -> None:
    if not isinstance(obj, dict):
        raise ManifestError(f"{where} must be an object")
    extra = set(obj) - allowed
    missing = allowed - set(obj)
    if extra or missing:
        raise ManifestError(f"{where} has keys {sorted(obj)}, expected {sorted(allowed)}")


def _hook(obj, where: str) -> Hook:
    _keys(obj, HOOK_KEYS, where)
    file, event, matcher = obj["file"], obj["event"], obj["matcher"]
    if not isinstance(file, str) or not HOOK_FILE.fullmatch(file):
        raise ManifestError(f"{where}.file must be a hook file name under hooks/")
    if event not in EVENTS:
        raise ManifestError(f"{where}.event must be one of {sorted(EVENTS)}")
    if not isinstance(matcher, str) or not matcher or "\n" in matcher:
        raise ManifestError(f"{where}.matcher must be a non-empty one-line string")
    return Hook(file, event, matcher)


def _entry(obj, where: str) -> Entry:
    _keys(obj, ENTRY_KEYS, where)
    sha = obj["row_sha256"]
    if not isinstance(sha, str) or not SHA256.fullmatch(sha):
        raise ManifestError(f"{where}.row_sha256 must be 64 lower-case hex")
    hooks, tests = obj["hooks"], obj["tests"]
    if not isinstance(hooks, list):
        raise ManifestError(f"{where}.hooks must be a list")
    if not isinstance(tests, list) or not tests:
        raise ManifestError(f"{where}.tests must be a non-empty list")
    if not all(isinstance(t, str) and TEST_ID.fullmatch(t) for t in tests) or len(set(tests)) != len(tests):
        raise ManifestError(f"{where}.tests must be distinct unittest ids (module.Class.test)")
    decision = obj["decision"]
    _keys(decision, DECISION_KEYS, f"{where}.decision")
    if not isinstance(decision["function"], str) or not FUNCTION.fullmatch(decision["function"]):
        raise ManifestError(f"{where}.decision.function must be trimtab.module:function")
    return Entry(sha, tuple(_hook(h, f"{where}.hooks[{n}]") for n, h in enumerate(hooks)),
                 tuple(tests), decision["function"], decision["allow"])


def parse(text: str) -> dict[str, Entry]:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as err:
        raise ManifestError("the manifest is not valid JSON") from err
    _keys(data, {"schema_version", "rows"}, "the manifest")
    if type(data["schema_version"]) is not int or data["schema_version"] != SCHEMA_VERSION:
        raise ManifestError(f"the manifest's schema_version must be {SCHEMA_VERSION}")
    rows = data["rows"]
    if not isinstance(rows, dict):
        raise ManifestError("the manifest's rows must be an object")
    out = {}
    for rid, obj in rows.items():
        if not ROW_ID.fullmatch(rid):
            raise ManifestError(f"the manifest maps {rid!r}, which is not a row id")
        out[rid] = _entry(obj, f"rows.{rid}")
    return out


def entry_problem(row: Row, entry: Entry | None) -> str | None:
    """Why this entry does not map this base row as the record has it, or None."""
    if row.side not in (BASE, BOTH):
        return "the record's owner is the consuming project, not this base" if entry else None
    if entry is None:
        return "this base's manifest does not map it"
    if entry.row_sha256 != row.sha256:
        return "the manifest's entry was written for other row text; the row changed since"
    files = {h.file for h in entry.hooks}
    if files - row.hooks:
        return f"the manifest names hooks {sorted(files - row.hooks)} that its owner {row.owner!r} does not"
    if row.hooks and not files:
        return f"the manifest names no hook, and its owner {row.owner!r} is a guard"
    return None
