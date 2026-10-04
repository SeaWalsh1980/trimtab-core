"""Scrub checks for anything pushed to the public base.

The private check builds its list at run time from the instance's own files, so
the list is never committed anywhere public. The public check looks only for
generic shapes and needs no instance. Both report a file, a line and a class,
never the matched text: the scrub must not itself leak what it found.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from trimtab import roots

ID_SHAPE = re.compile(r"\b(?:trig|env|session)_[0-9A-Za-z]{16,}\b")
NUMBERED_DOC = re.compile(r"^\d{4}-.+\.md$")
SKIP_DIRS = {".git", "__pycache__"}
TEST_PARTS = {"tests", "test", "fixtures"}
PATTERNS_ENV = "TRIMTAB_SECRET_PATTERNS"
GIT_TIMEOUT_SECONDS = 5


class ScrubError(Exception):
    """The deny-list cannot be built as declared, so the check cannot vouch for a tree."""


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    kind: str


@dataclass(frozen=True)
class Terms:
    literals: dict[str, tuple[str, ...]] = field(default_factory=dict)
    patterns: dict[str, tuple[re.Pattern, ...]] = field(default_factory=dict)
    exempt_tests: frozenset[str] = frozenset()


def _git(root: Path, *args: str) -> str:
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise ScrubError(f"git could not be run in {root} ({type(err).__name__})") from err
    return done.stdout.strip() if done.returncode == 0 else ""


def _private_patterns(environ: Mapping[str, str]) -> tuple[re.Pattern, ...]:
    """The declared private pattern file: `<ERE>` tab `<label>` per line, as the guard reads it."""
    declared = environ.get(PATTERNS_ENV, "")
    if not declared:
        return ()
    if not declared.startswith("/"):
        # Relative to what? The scrub's working directory is not the instance. Never echo the value.
        raise ScrubError(f"{PATTERNS_ENV} is not an absolute path")
    try:
        lines = Path(declared).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as err:
        raise ScrubError(f"{PATTERNS_ENV} names a file that cannot be read ({type(err).__name__})") from err
    compiled = []
    for n, line in enumerate(lines, 1):
        if not line.strip() or line.startswith("#"):
            continue
        regex = line.split("\t", 1)[0] if "\t" in line else ""
        if not regex:
            raise ScrubError(f"{PATTERNS_ENV} line {n} has no tab-separated pattern")
        try:
            compiled.append(re.compile(regex))
        except re.error as err:
            raise ScrubError(f"{PATTERNS_ENV} line {n} is not a valid pattern") from err
    return tuple(compiled)


def _consumers(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise ScrubError(f"{path.name} cannot be read ({type(err).__name__})") from err
    entries = data.get("consumers", []) if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ScrubError(f"{path.name} has no consumers list")
    return [e["repo"] for e in entries if isinstance(e, dict) and isinstance(e.get("repo"), str)]


def private_terms(instance: Path, environ: Mapping[str, str]) -> Terms:
    instance = Path(instance)
    try:
        # Its own repository is the likeliest private name to leak; never build the list without it.
        repos = {roots.instance_repo(instance)}
    except roots.InstanceInvalid as err:
        raise ScrubError(str(err)) from err
    repos.update(_consumers(instance / "consumers.json"))
    ids: set[str] = set()
    for path in (instance / "routines").rglob("*"):
        if path.is_file():
            try:
                ids.update(ID_SHAPE.findall(path.read_bytes().decode("utf-8", errors="replace")))
            except OSError as err:
                raise ScrubError(f"routines/{path.name} cannot be read ({type(err).__name__})") from err
    doctrine = {p.name for p in (instance / "rules").glob("*.md")}
    # The instance's own decision records and plans, by full numbered name: these are what
    # leaked into the base before. Unnumbered names (README.md) would match everywhere, and a
    # name this base's own series also carries is public, so neither is a term.
    docs = {p.name for folder in ("adr", "plans") for p in (instance / "docs" / folder).glob("*.md")
            if NUMBERED_DOC.match(p.name)}
    docs -= {p.name for p in (roots.code_root() / "docs" / "adr").glob("*.md")}
    home = environ.get("HOME", "")
    email = _git(instance, "config", "user.email")
    literals = {"repo": tuple(sorted(repos)), "id": tuple(sorted(ids)),
                "doctrine-file": tuple(sorted(doctrine)), "private-doc": tuple(sorted(docs)),
                "home": (home,) if home else (), "email": (email,) if email else ()}
    private = _private_patterns(environ)
    patterns = {"pattern": private} if private else {}
    return Terms(literals={k: v for k, v in literals.items() if v}, patterns=patterns)


def public_terms(self_repo: str | None) -> Terms:
    # The repo-url pattern below skips this repository's own URL with a negative
    # lookahead, (?!<own>\b). With no --self given there is nothing to skip, so
    # `own` becomes "(?!)", a pattern that never matches: the lookahead then
    # always succeeds and every GitHub repository URL is reported.
    own = re.escape(self_repo) if self_repo else r"(?!)"
    return Terms(
        patterns={
            "id-shape": (ID_SHAPE,),
            "home-path": (re.compile(r"/home/[a-z_][a-z0-9_-]*/|/Users/[A-Za-z0-9_.-]+/"),),
            "repo-url": (re.compile(rf"github\.com/(?!{own}\b)[A-Za-z0-9-]+/[A-Za-z0-9._-]+"),),
            "repo-field": (re.compile(r'"repo"\s*:\s*"[^"/]+/[^"]+"'),),
        },
        # Test files legitimately hold fake home paths, fixture repositories and
        # example URLs (e.g. tests/guard-tests.sh). ID shapes are never exempt.
        exempt_tests=frozenset({"repo-field", "repo-url", "home-path"}),
    )


def _in_tests(rel: Path) -> bool:
    return bool(TEST_PARTS & set(rel.parts))


def scan(tree: Path, terms: Terms) -> list[Hit]:
    tree = Path(tree)
    if not tree.is_dir():
        # rglob over a missing path or a file yields nothing, which would read as a clean tree.
        raise ScrubError(f"{tree} is not a directory")
    # Names are matched without regard to case: GitHub's are case-insensitive, and prose lowercases them.
    literals = {k: tuple(w.casefold() for w in words) for k, words in terms.literals.items()}
    hits: list[Hit] = []
    for path in sorted(tree.rglob("*")):
        rel = path.relative_to(tree)
        if not path.is_file() or SKIP_DIRS & set(rel.parts):
            continue
        try:
            # Undecodable bytes are replaced, not skipped: a binary file can still leak a name.
            lines = path.read_bytes().decode("utf-8", errors="replace").splitlines()
        except OSError as err:
            raise ScrubError(f"{rel.as_posix()} cannot be read ({type(err).__name__})") from err
        for n, text in enumerate(lines, 1):
            folded = text.casefold()
            kinds = [k for k, words in literals.items() if any(w in folded for w in words)]
            kinds += [k for k, pats in terms.patterns.items() if any(p.search(text) for p in pats)]
            for kind in kinds:
                if kind in terms.exempt_tests and _in_tests(rel):
                    continue
                hits.append(Hit(rel.as_posix(), n, kind))
    return hits
