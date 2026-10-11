"""`trimtab lint structure`: what loads in every session, and whether it is too much.

Always-loaded content is every instruction file (`CLAUDE.md` and/or
`AGENTS.md`), the files they import with `@path`, and rules without a `paths:`
key. It is measured in bytes: the observer's `context_tokens` arrives only on
resumed sessions, so it measures a conversation, not this baseline.

Findings are proposal triggers (ADR 0006), not errors: over the
project's budget, an instruction file over 200 lines, or more than 10% growth
since a previous report or since the tree at a git ref (`--against`).
"""

from __future__ import annotations

import io
import re
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from trimtab.items import Problem, strongest
from trimtab.registry.rules import OVERRIDES, frontmatter

INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md", ".claude/CLAUDE.md")
MAX_LINES = 200
MAX_GROWTH = 0.10
MAX_IMPORT_DEPTH = 5  # Claude Code's own limit on nested imports
IMPORT = re.compile(r"(?<![\w`])@((?:\.{1,2}/)?[\w][\w./-]*\.\w+)")
FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class Measured:
    path: str
    bytes: int
    lines: int
    instruction: bool


@dataclass(frozen=True)
class Report:
    files: tuple[Measured, ...]
    budget_bytes: int
    findings: tuple[Problem, ...]

    @property
    def total_bytes(self) -> int:
        return sum(f.bytes for f in self.files)

    def to_dict(self) -> dict:
        return {"total_bytes": self.total_bytes, "budget_bytes": self.budget_bytes,
                "files": [{"path": f.path, "bytes": f.bytes, "lines": f.lines} for f in self.files]}


def _imports(text: str) -> list[str]:
    found, fenced = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            found += IMPORT.findall(line)
    return found


def _collect(root: Path, path: Path, depth: int, seen: dict[Path, bool], instruction: bool) -> None:
    real = path.resolve()
    if real in seen or not path.is_file() or depth > MAX_IMPORT_DEPTH:
        return
    try:
        real.relative_to(root.resolve())
    except ValueError:
        return  # outside the project: user-level files are measured on their own layer
    seen[real] = instruction
    for ref in _imports(path.read_text(encoding="utf-8", errors="replace")):
        _collect(root, path.parent / ref, depth + 1, seen, False)


def always_loaded(root: Path, rules_dirs: list[str]) -> dict[Path, bool]:
    seen: dict[Path, bool] = {}
    for name in INSTRUCTION_FILES:
        _collect(root, root / name, 0, seen, True)
    for d in rules_dirs:
        for rule in sorted((root / d).glob("*.md")):
            meta = frontmatter(rule.read_text(encoding="utf-8")) or {}
            if "paths" not in meta or rule.name == OVERRIDES:
                _collect(root, rule, 0, seen, False)
    return seen


def measure(root: Path, rules_dirs: list[str], budget_kb: int, previous: dict | None = None) -> Report:
    root = Path(root)
    files = []
    for real, instruction in always_loaded(root, rules_dirs).items():
        data = real.read_bytes()
        files.append(Measured(real.relative_to(root.resolve()).as_posix(), len(data),
                              data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1), instruction))
    files.sort(key=lambda f: f.path)
    budget = budget_kb * 1024
    total = sum(f.bytes for f in files)
    findings = []
    if total > budget:
        findings.append(Problem("over-budget", "always-loaded", f"{total} bytes against a budget of {budget}"))
    for f in files:
        if f.instruction and f.lines > MAX_LINES:
            findings.append(Problem("too-long", f.path, f"{f.lines} lines; the target is under {MAX_LINES}"))
    before = (previous or {}).get("total_bytes")
    if isinstance(before, int) and before > 0 and total > before * (1 + MAX_GROWTH):
        findings.append(Problem("growth", "always-loaded", f"grew {100 * (total - before) // before}% since the previous report"))
    return Report(tuple(files), budget, tuple(findings))


GIT_TIMEOUT_SECONDS = 60
H2 = re.compile(r"^##\s+(.+?)\s*$")


class TreeError(Exception):
    """git could not export the tree at a ref."""


def measure_at(root: Path, ref: str, rules_dirs: list[str], budget_kb: int) -> Report:
    """The same measurement over the tree as it stood at `ref`: the growth baseline.

    A routine has no disk to keep last run's report on, so the baseline is the
    committed tree at a ref instead (ADR 0006). Exported with `git archive`
    into a temporary directory, removed on every path.
    """
    try:
        done = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", ref],
                              capture_output=True, timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise TreeError(f"git archive {ref}: {type(err).__name__}") from err
    if done.returncode != 0:
        raise TreeError(f"git archive {ref} exited {done.returncode}")
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(done.stdout)) as tar:
            tar.extractall(tmp, filter="data")
        return measure(Path(tmp), rules_dirs, budget_kb)


@dataclass(frozen=True)
class Section:
    path: str
    heading: str
    bytes: int
    binding: bool  # holds a REQUIRE or PROHIBIT: stays always-loaded whatever its size


def sections(root: Path, report: Report, top: int = 10) -> list[Section]:
    """The largest `##` sections of the always-loaded files: what a structure proposal would move."""
    found = []
    for f in report.files:
        heading, chunk, fenced = "(preamble)", [], False

        def close():
            text = "".join(chunk)
            if text.strip():
                found.append(Section(f.path, heading, len(text.encode("utf-8")), strongest(text).binding))

        for line in (Path(root) / f.path).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True):
            if FENCE.match(line):
                fenced = not fenced
            m = None if fenced else H2.match(line)
            if m:
                close()
                heading, chunk = m.group(1), []
            chunk.append(line)
        close()
    return sorted(found, key=lambda s: (-s.bytes, s.path, s.heading))[:top]
