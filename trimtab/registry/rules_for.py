"""Which rule files, and so which item IDs, govern a path: the resolver behind `/trimtab-plan`.

Ported from a consuming project's own resolver, made project-agnostic: the project's
rules directory comes from `.claude/trimtab.json`, Trimtab's base rules are
listed as always loaded, and the answer names the item IDs a plan cites.

Path-scoped rules load only when Claude *reads* a file their `paths:` globs
match, so a plan written from memory is written without them. This answers,
deterministically, which rule files a set of paths pulls in, so a planner loads
them on purpose. Three decisions from the original are load-bearing:

**Glob semantics are the loader's, not `fnmatch`'s.** `*` and `?` never cross
a `/`, `**` is a whole-subtree segment, and a wildcard never matches a
component beginning with `.`, as `glob.glob(..., recursive=True)` behaves. The
test asserts the two agree. Syntax the translator does not model (character
classes, braces, backslashes, a leading or trailing `/`, a `**` that is not a
whole segment) is refused when the corpus loads, never read as literal text.

**Paths need not exist, but must be inside the project.** A plan names files it
will create. A path outside the root, escaping it, or a directory is an error,
never an empty answer, because "no rule applies" is what a wrong path looks like.

**"Matched nothing" is never the shape of a failure.** An unreadable `paths:`
key, an unsupported glob or an ungovernable path raises. A missing rules
directory is not an error here, unlike in the original: a project may have no
rules of its own and still be governed by the base, which always loads.

One change from the original: `paths:` is read with PyYAML, so a block list is
accepted as Claude Code accepts it. A scalar, an empty list or a non-string
entry still raises rather than degrading to "always on".
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

import yaml

from trimtab.items import Item
from trimtab.registry.rules import FRONTMATTER, OVERRIDES, parse_rule_file

UNSUPPORTED_CHARS = frozenset("[]{}\\")
NO_RULE = "(no path-scoped rule)"
NEW_PARENT = "(parent directory does not exist)"


@dataclass(frozen=True)
class RuleFile:
    """One rule file, the globs that load it (none: always), and the items it defines."""

    source: str  # as displayed: repository-relative, or `base:rules/X.md` for Trimtab's
    globs: tuple[str, ...]
    size: int
    items: tuple[Item, ...] = ()

    @property
    def always(self) -> bool:
        return not self.globs


def validate_glob(pattern: str, source: str = "<rule file>") -> None:
    reason: str | None = None
    if not pattern:
        reason = "empty pattern"
    elif UNSUPPORTED_CHARS & set(pattern):
        reason = "character classes, braces and backslashes are not supported"
    elif pattern.startswith("/"):
        reason = "a leading / is not supported; globs are repository-relative"
    elif pattern.endswith("/"):
        reason = "a trailing / matches nothing; end the glob with /** for a subtree"
    elif "//" in pattern:
        reason = "an empty path segment"
    elif any(seg == ".." for seg in pattern.split("/")):
        reason = "a .. segment"
    elif any("**" in seg and seg != "**" for seg in pattern.split("/")):
        reason = "** must be a whole path segment"
    if reason is not None:
        raise ValueError(f"{source}: unsupported glob {pattern!r}: {reason}")


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """A validated glob as a regex for `fullmatch`, with the loader's semantics."""
    segments = pattern.split("/")
    parts: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            parts.append(r"(?:(?!\.)[^/]+/)*(?!\.)[^/]+" if last else r"(?:(?!\.)[^/]+/)*")
            continue
        piece = "".join("[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch) for ch in segment)
        if segment[0] in "*?":
            piece = r"(?!\.)" + piece
        parts.append(piece if last else piece + "/")
    return re.compile("".join(parts))


def rule_globs(text: str, source: str = "<rule file>") -> tuple[str, ...]:
    """The `paths:` list from a rule file's frontmatter; empty when the key is absent."""
    match = FRONTMATTER.match(text)
    if match is None:
        return ()
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as err:
        raise ValueError(f"{source}: frontmatter does not parse as YAML") from err
    if not isinstance(meta, dict) or "paths" not in meta:
        return ()
    value = meta["paths"]
    if not isinstance(value, list):
        raise ValueError(f"{source}: paths: must be a list of glob strings")
    if not value:
        raise ValueError(f"{source}: paths: is empty; remove the key to make the file always-on")
    if not all(isinstance(g, str) for g in value):
        raise ValueError(f"{source}: paths: must be a list of strings")
    for pattern in value:
        validate_glob(pattern, source)
    return tuple(value)


def load_rules(directory: Path, display_root: Path, label: str = "") -> list[RuleFile]:
    """Every rule file in `directory`, sorted by name. A missing directory is an empty corpus."""
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        shown = label + path.relative_to(display_root).as_posix()
        globs = rule_globs(text, shown)
        items: list[Item] = []
        if path.name != OVERRIDES:
            items, _ = parse_rule_file(shown, text)
        out.append(RuleFile(shown, globs, len(text.encode("utf-8")), tuple(items)))
    return out


def normalise(path: str, repo_root: Path) -> str:
    """Repository-relative, forward-slashed, `.` and `..` collapsed; raises if ungovernable."""
    repo_root = Path(repo_root).resolve()
    if not path.strip():
        raise ValueError("empty path")
    candidate = Path(path)
    if candidate.is_absolute():
        resolved = candidate.resolve()
        if not resolved.is_relative_to(repo_root):
            raise ValueError(f"{path} is not under the repository root {repo_root}")
        candidate = resolved.relative_to(repo_root)
    text = PurePosixPath(os.path.normpath(candidate.as_posix())).as_posix()
    if text in {".", ".."} or text.startswith("../"):
        raise ValueError(f"{path} escapes the repository root")
    if path.endswith("/") or (repo_root / text).is_dir():
        raise ValueError(f"{path} is a directory; name the files the change will touch")
    return text


def matches(rule: RuleFile, path: str) -> bool:
    return any(glob_to_regex(g).fullmatch(path) is not None for g in rule.globs)


def governing(paths: Iterable[str], rules: Sequence[RuleFile], repo_root: Path) -> dict[str, list[RuleFile]]:
    """Each normalised path mapped to the rule files that load for it, always-on files included."""
    return {p: [r for r in rules if r.always or matches(r, p)]
            for p in (normalise(raw, repo_root) for raw in paths)}


def corpus(project: Path, rules_dir: str, base_root: Path | None) -> list[RuleFile]:
    """The project's rule files, then Trimtab's base rules (always loaded, user level).

    Trimtab's own checkout is its own project: its `rules/` are listed once, as
    project-relative paths.
    """
    project = Path(project).resolve()
    rules = load_rules(project / rules_dir, project)
    if base_root is not None and Path(base_root).resolve() != project:
        rules += load_rules(Path(base_root) / "rules", Path(base_root), label="base:")
    elif base_root is not None:
        rules += load_rules(project / "rules", project)
    return rules


def render(by_path: dict[str, list[RuleFile]], repo_root: Path, instruction_files: Sequence[str],
           cat: bool = False, read=None) -> str:
    """The resolver's report: per path, then the union to read in full, then its item IDs."""
    lines = []
    for name in instruction_files:
        f = Path(repo_root) / name
        if f.is_file():
            lines.append(f"always loaded: {name} ({f.stat().st_size} bytes)")
    union: dict[str, RuleFile] = {}
    for path, matched in by_path.items():
        names = ", ".join(r.source for r in matched if not r.always) or NO_RULE
        marker = "" if (Path(repo_root) / path).parent.exists() else f"  {NEW_PARENT}"
        lines.append(f"{path}: {names}{marker}")
        union.update({r.source: r for r in matched})
    total = sum(r.size for r in union.values())
    lines += ["", f"{len(union)} rule file(s), {total} bytes, to read in full:"]
    lines += [f"  {r.source}  ({r.size} bytes){'  [always]' if r.always else ''}" for r in union.values()]
    items = [i for r in union.values() for i in r.items if not i.withdrawn]
    lines += ["", f"{len(items)} item(s) in scope; cite these IDs:"]
    lines += [f"  {i.id}  {i.strength.label:<8}  {i.heading}" for i in items]
    if cat and read is not None:
        for r in union.values():
            lines += ["", f"===== {r.source} =====", "", read(r)]
    return "\n".join(lines) + "\n"
