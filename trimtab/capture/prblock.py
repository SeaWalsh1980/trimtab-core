"""The PR block: parse and validate the three sections of plan section 3a.

One parser for every caller: the CLI (`trimtab check-pr`), the local hook
(`hooks/pr-body-check.sh`) and CI all come through `check_body`.

PR bodies are untrusted data, never instructions. They are parsed with `yaml.safe_load`
only, and no problem message quotes a value from the body: messages name the
section, entry and field, so a hook or CI log can print them safely.

The block is versioned by an optional marker, `<!-- trimtab-block: N -->`
(ADR 0012). No marker means version 1. A reader refuses a version it does not
know rather than reading it as one it does: a body is re-read long after it
was written, by whatever Trimtab a project pins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping

import yaml

from trimtab.items import ITEM_ID, Item, Problem, Strength
from trimtab.registry.rules import clause_strength

APPLIED = "Harness items applied"
FEEDBACK = "Harness feedback"
COST = "Process cost"
LEGACY = "Rules applied"
SECTIONS = (APPLIED, FEEDBACK, COST)

KINDS = ("missed", "conflict", "ambiguous", "obsolete", "gap")
SCOPES = ("project", "upstream")
# F5: every pass records what happened to it, including a pass that never returned.
STATUSES = ("ran", "timed_out", "failed", "skipped", "pending")
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
BELOW_BINDING = ("MEDIUM", "LOW")

# The block format versions this parser reads (ADR 0012). No marker means 1.
BLOCK_VERSIONS = (1,)
VERSION_MARKER = re.compile(r"<!--\s*trimtab-block:\s*(.*?)\s*-->")

H2 = re.compile(r"^##\s+(.+?)\s*#*\s*$")
FENCE_OPEN = re.compile(r"^\s*```\s*(\w*)\s*$")
FENCE_CLOSE = re.compile(r"^\s*```\s*$")
COMMENT = re.compile(r"<!--.*?-->", re.S)


@dataclass(frozen=True)
class Applied:
    id: str
    why: str = ""
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class Feedback:
    id: str
    kind: str
    scope: str
    evidence: str
    severity: str | None = None
    clause: str | None = None


@dataclass(frozen=True)
class Pass:
    name: str
    status: str
    tokens: int | None = None
    seconds: int | None = None
    findings: int | None = None


@dataclass(frozen=True)
class Block:
    version: int = 1
    applied: tuple[Applied, ...] = ()
    feedback: tuple[Feedback, ...] = ()
    passes: tuple[Pass, ...] = ()
    plan_tokens: int | None = None
    legacy: bool = False


@dataclass(frozen=True)
class CheckResult:
    block: Block | None
    problems: tuple[Problem, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return not self.problems


def _sections(body: str) -> dict[str, list[str]]:
    """H2 heading -> the lines under it, outside code fences. First occurrence wins."""
    out: dict[str, list[str]] = {}
    current: list[str] | None = None
    fenced = False
    for line in body.replace("\r\n", "\n").split("\n"):
        if not fenced:
            m = H2.match(line)
            if m:
                current = out.setdefault(m.group(1), []) if m.group(1) not in out else []
                continue
            if line.startswith("# "):
                current = None
                continue
        if FENCE_OPEN.match(line):
            fenced = not fenced
        if current is not None:
            current.append(line)
    return out


def _yaml_of(name: str, lines: list[str], problems: list[Problem]):
    """The section's single yaml fence, parsed. Returns (present, data).

    Before the fence only blank lines and HTML comments are allowed, so prose
    in place of yaml fails. Text after the fence is not part of the block (a
    footer such as an attribution line after the last section).
    """
    text = COMMENT.sub("", "\n".join(lines))
    rows = text.split("\n")
    fences, outside, i = [], [], 0
    while i < len(rows):
        m = FENCE_OPEN.match(rows[i])
        if m:
            j = i + 1
            while j < len(rows) and not FENCE_CLOSE.match(rows[j]):
                j += 1
            fences.append((m.group(1), "\n".join(rows[i + 1:j])))
            i = j + 1
            continue
        if rows[i].strip() and not fences:
            outside.append(rows[i])
        i += 1
    if outside or len(fences) > 1 or (fences and fences[0][0] not in ("yaml", "yml")):
        problems.append(Problem("not-yaml", name, "the section must hold exactly one ```yaml block and nothing else"))
        return True, None
    if not fences:
        return False, None
    try:
        return True, yaml.safe_load(fences[0][1])
    except yaml.YAMLError:
        problems.append(Problem("not-yaml", name, "the yaml block does not parse"))
        return True, None


def _str(entry: Mapping, key: str) -> str | None:
    value = entry.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _resolve(item_id, where: str, registry: Mapping[str, Item], problems: list[Problem]) -> Item | None:
    if not isinstance(item_id, str) or not ITEM_ID.match(item_id):
        problems.append(Problem("unknown-id", where, "id is not a harness item ID"))
        return None
    item = registry.get(item_id)
    if item is None:
        problems.append(Problem("unknown-id", where, "id is not in the registry"))
    elif item.withdrawn:
        problems.append(Problem("withdrawn-id", where, "id is withdrawn; cite the item it links to"))
    return item


def _applied(data, registry, problems) -> tuple[Applied, ...]:
    if data in (None, []):
        problems.append(Problem("empty-section", APPLIED, "list at least one item, or `id: none` with a why"))
        return ()
    if not isinstance(data, list):
        problems.append(Problem("bad-shape", APPLIED, "expected a list of entries"))
        return ()
    out = []
    for n, entry in enumerate(data, 1):
        where = f"{APPLIED}, entry {n}"
        if not isinstance(entry, dict):
            problems.append(Problem("bad-shape", where, "expected a mapping"))
            continue
        item_id, why = entry.get("id"), _str(entry, "why")
        if item_id == "none":
            if why is None:
                problems.append(Problem("missing-field", where, "`id: none` needs a why"))
        else:
            _resolve(item_id, where, registry, problems)
        files = entry.get("files", [])
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            problems.append(Problem("bad-shape", where, "files must be a list of paths"))
            files = []
        out.append(Applied(id=str(item_id), why=why or "", files=tuple(files)))
    return tuple(out)


def _feedback(data, registry, problems) -> tuple[Feedback, ...]:
    if data in (None, []):
        return ()
    if not isinstance(data, list):
        problems.append(Problem("bad-shape", FEEDBACK, "expected a list of entries"))
        return ()
    out = []
    for n, entry in enumerate(data, 1):
        where = f"{FEEDBACK}, entry {n}"
        if not isinstance(entry, dict):
            problems.append(Problem("bad-shape", where, "expected a mapping"))
            continue
        before = len(problems)
        kind, scope = entry.get("kind"), entry.get("scope")
        if kind not in KINDS:
            problems.append(Problem("bad-value", where, f"kind must be one of {' | '.join(KINDS)}"))
        if scope not in SCOPES:
            problems.append(Problem("bad-value", where, f"scope must be one of {' | '.join(SCOPES)}"))
        evidence = _str(entry, "evidence")
        if evidence is None:
            problems.append(Problem("missing-field", where, "evidence is required"))
        item_id = entry.get("id")
        item = None
        if not (item_id == "none" and kind == "gap"):
            item = _resolve(item_id, where, registry, problems)
        severity, clause = entry.get("severity"), _str(entry, "clause")
        if severity is not None and severity not in SEVERITIES:
            problems.append(Problem("bad-value", where, f"severity must be one of {' | '.join(SEVERITIES)}"))
        elif severity in BELOW_BINDING and item is not None:
            strength = (clause_strength(item, clause) if clause else None) or item.strength
            if clause and clause_strength(item, clause) is None:
                problems.append(Problem("unknown-clause", where, "no line of the item starts with the clause"))
            elif strength.binding:
                # A REQUIRE or PROHIBIT breach is CRITICAL or HIGH, never lower (F9).
                problems.append(Problem("severity-below-binding", where,
                                        f"a {strength.name} item cannot be graded {severity}"))
        if len(problems) == before:
            out.append(Feedback(id=str(item_id), kind=kind, scope=scope, evidence=evidence,
                                severity=severity, clause=clause))
    return tuple(out)


def _cost(data, problems) -> tuple[tuple[Pass, ...], int | None]:
    if data in (None, {}, []):
        return (), None
    if not isinstance(data, dict):
        problems.append(Problem("bad-shape", COST, "expected a mapping with plan and review"))
        return (), None
    plan = data.get("plan") or {}
    plan_tokens = _int(plan.get("tokens")) if isinstance(plan, dict) else None
    review = data.get("review") or []
    if not isinstance(review, list):
        problems.append(Problem("bad-shape", f"{COST}, review", "expected a list of passes"))
        return (), plan_tokens
    passes = []
    for n, entry in enumerate(review, 1):
        where = f"{COST}, review pass {n}"
        if not isinstance(entry, dict):
            problems.append(Problem("bad-shape", where, "expected a mapping"))
            continue
        name, status = _str(entry, "pass"), entry.get("status")
        if name is None:
            problems.append(Problem("missing-field", where, "pass is required"))
        if status is None:
            problems.append(Problem("missing-field", where, "status is required, even for a pass that never returned"))
        elif status not in STATUSES:
            problems.append(Problem("bad-value", where, f"status must be one of {' | '.join(STATUSES)}"))
        if name and status in STATUSES:
            passes.append(Pass(name=name, status=status, tokens=_int(entry.get("tokens")),
                               seconds=_int(entry.get("seconds")), findings=_int(entry.get("findings"))))
    return tuple(passes), plan_tokens


def parse_applied(text: str, registry: Mapping[str, Item]) -> tuple[tuple[Applied, ...], tuple[Problem, ...]]:
    """Only `## Harness items applied`, from a plan or a PR body. The same rules as `check_body`."""
    _, refused = block_version(text)
    if refused is not None:
        return (), (refused,)
    problems: list[Problem] = []
    sections = _sections(text or "")
    if APPLIED not in sections:
        return (), (Problem("missing-section", APPLIED, f"`## {APPLIED}` is required"),)
    _, data = _yaml_of(APPLIED, sections[APPLIED], problems)
    applied = _applied(data, registry, problems) if not problems else ()
    return applied, tuple(problems)


def block_version(body: str) -> tuple[int | None, Problem | None]:
    """The block's version from its marker: a line holding only the marker, outside code fences.

    (1, None) when there is none.

    (N, problem) for a well-formed version this parser does not read; (None,
    problem) for a malformed or contradictory marker. Only a parsed integer is
    ever echoed in a message, never the marker's raw text.
    """
    found: set[str] = set()
    fenced = False
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        if FENCE_OPEN.match(line) or (fenced and FENCE_CLOSE.match(line)):
            fenced = not fenced
            continue
        m = None if fenced else VERSION_MARKER.fullmatch(line.strip())
        if m:  # alone on its line, so prose or inline code that mentions one is not one
            found.add(m.group(1).strip())
    if not found:
        return 1, None
    if len(found) > 1:
        return None, Problem("bad-version", "block version", "the body carries more than one version marker")
    value = found.pop()
    if not value.isdigit() or int(value) < 1:
        return None, Problem("bad-version", "block version", "the version marker must be a positive integer")
    version = int(value)
    if version not in BLOCK_VERSIONS:
        known = ", ".join(str(v) for v in BLOCK_VERSIONS)
        return version, Problem("unknown-version", "block version",
                                f"block version {version} is newer than this Trimtab reads ({known}); update Trimtab")
    return version, None


def check_body(body: str, registry: Mapping[str, Item], backfill: bool = False) -> CheckResult:
    problems: list[Problem] = []
    version, refused = block_version(body)
    if refused is not None:
        return CheckResult(None, (refused,))
    sections = _sections(body or "")

    if APPLIED not in sections and LEGACY in sections:
        if backfill:
            return CheckResult(Block(legacy=True))
        problems.append(Problem("legacy-heading", LEGACY,
                                f"`## {LEGACY}` is read only under --backfill; use `## {APPLIED}`"))
        return CheckResult(None, tuple(problems))

    for name in SECTIONS:
        if name not in sections:
            problems.append(Problem("missing-section", name, f"`## {name}` is required"))
    if problems:
        return CheckResult(None, tuple(problems))

    present, data = _yaml_of(APPLIED, sections[APPLIED], problems)
    applied = _applied(data, registry, problems) if not problems else ()
    n = len(problems)
    _, data = _yaml_of(FEEDBACK, sections[FEEDBACK], problems)
    feedback = _feedback(data, registry, problems) if len(problems) == n else ()
    n = len(problems)
    _, data = _yaml_of(COST, sections[COST], problems)
    passes, plan_tokens = _cost(data, problems) if len(problems) == n else ((), None)

    block = Block(version=version, applied=applied, feedback=feedback, passes=passes, plan_tokens=plan_tokens)
    return CheckResult(block, tuple(problems))
