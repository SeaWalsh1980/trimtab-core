"""instance.json: an instance's deployment values (ADR 0008).

The only reader of the file (ARC-3). `parse` is pure; `load` and
`patterns_path` touch the file system. The file holds no routine or
environment ID, and has no field for one (see trimtab/routines.py).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

SCHEMA_VERSION = 1
FILE = "instance.json"
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
PACK = re.compile(r"^[a-z0-9-]+$")
TOP = {"schema_version", "repo", "base", "guards"}
BASE = {"repo", "sha"}
GUARDS = {"secrets_packs", "secrets_patterns"}
NO_ID_FIELDS = {"cloud", "routines"}


class InstanceFileError(Exception):
    """instance.json is missing, unreadable, or breaks its schema."""


@dataclass(frozen=True)
class InstanceFile:
    repo: str
    base_repo: str
    base_sha: str
    secrets_packs: tuple[str, ...] | None  # None: every shipped pack; (): "none"
    secrets_patterns: str | None  # relative to the instance root


def _keys(obj: dict, allowed: set[str], where: str) -> None:
    for key in obj:
        if key in NO_ID_FIELDS:
            raise InstanceFileError(f"{where}{key}: no file holds routine or environment IDs; "
                                    "remove the key")
        if key not in allowed:
            raise InstanceFileError(f"{where}{key}: unknown key")


def _repo(value: object, key: str) -> str:
    if not isinstance(value, str) or not REPO.fullmatch(value) or {".", ".."} & set(value.split("/")):
        raise InstanceFileError(f"{key} must be owner/name, never a URL, and no part may be . or ..")
    return value


def _packs(value: object) -> tuple[str, ...]:
    if value == "none":
        return ()
    if not isinstance(value, list) or not value:
        raise InstanceFileError('guards.secrets_packs must be a non-empty list of pack names, or "none"')
    if not all(isinstance(p, str) and PACK.fullmatch(p) and p != "none" for p in value):
        raise InstanceFileError("guards.secrets_packs: each pack name is lower-case letters, digits and hyphens")
    return tuple(value)


def _patterns(value: object) -> str:
    bad = InstanceFileError("guards.secrets_patterns must be a path inside the instance, "
                            "relative, with no '..' and no leading '~'")
    if not isinstance(value, str) or not value or value.startswith(("/", "~")) or "\\" in value:
        raise bad
    if ".." in PurePosixPath(value).parts:
        raise bad
    return value


def parse(data: object) -> InstanceFile:
    if not isinstance(data, dict):
        raise InstanceFileError(f"{FILE} must hold a JSON object")
    _keys(data, TOP, "")
    version = data.get("schema_version")
    # True == 1 and 1.0 == 1 in Python; the file means the integer.
    if type(version) is not int or version != SCHEMA_VERSION:
        raise InstanceFileError(f"schema_version must be {SCHEMA_VERSION}")
    base = data.get("base")
    if not isinstance(base, dict):
        raise InstanceFileError("base must be an object with repo and sha")
    _keys(base, BASE, "base.")
    sha = base.get("sha")
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise InstanceFileError("base.sha must be a full 40-character lower-case commit SHA, "
                                "never a branch, a tag or a short SHA")
    guards = data.get("guards", {})
    if not isinstance(guards, dict):
        raise InstanceFileError("guards must be an object")
    _keys(guards, GUARDS, "guards.")
    return InstanceFile(
        repo=_repo(data.get("repo"), "repo"),
        base_repo=_repo(base.get("repo"), "base.repo"),
        base_sha=sha,
        secrets_packs=_packs(guards["secrets_packs"]) if "secrets_packs" in guards else None,
        secrets_patterns=_patterns(guards["secrets_patterns"]) if "secrets_patterns" in guards else None,
    )


def load(root: Path) -> InstanceFile:
    path = Path(root) / FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as err:
        raise InstanceFileError(f"the instance has no {FILE}") from err
    except (OSError, UnicodeDecodeError) as err:
        raise InstanceFileError(f"{FILE} cannot be read ({type(err).__name__})") from err
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise InstanceFileError(f"{FILE} is not valid JSON") from err
    return parse(data)


def packs_env(f: InstanceFile) -> str | None:
    """The TRIMTAB_SECRET_PACKS value bootstrap writes, or None to write none (every pack, ADR 0008)."""
    if f.secrets_packs is None:
        return None
    return ",".join(f.secrets_packs) or "none"


def patterns_path(f: InstanceFile, root: Path) -> Path | None:
    """The private pattern file as an absolute path, resolved inside the instance."""
    if f.secrets_patterns is None:
        return None
    base = Path(root).resolve()
    resolved = (base / f.secrets_patterns).resolve()
    if not resolved.is_relative_to(base):
        raise InstanceFileError("guards.secrets_patterns resolves outside the instance")
    if not resolved.is_file():
        raise InstanceFileError("guards.secrets_patterns names a file that does not exist")
    return resolved
