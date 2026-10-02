"""The package's two roots (docs/plans/0013-trimtab-split-stage2.md, section 3).

The code root is this checkout: agents, commands, templates. The instance root
holds the doctrine (rules/, HARNESS.md), the deployment files and the lock. It
comes only from --instance or TRIMTAB_INSTANCE; there is no fallback (S2-2),
so a missing value is a loud error, never a read of the wrong tree.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from trimtab import config as project_config

ENV = "TRIMTAB_INSTANCE"


class InstanceError(Exception):
    """The instance root is not set, or does not name an instance."""


class InstanceNotSet(InstanceError):
    """Neither --instance nor TRIMTAB_INSTANCE was given."""


class InstanceInvalid(InstanceError):
    """The given path is not a directory holding rules/."""


def code_root() -> Path:
    return Path(__file__).resolve().parents[1]


def instance_root(explicit: str | os.PathLike | None = None,
                  environ: Mapping[str, str] = os.environ) -> Path:
    raw = str(explicit) if explicit not in (None, "") else environ.get(ENV, "")
    if not raw:
        raise InstanceNotSet(f"no instance: pass --instance <dir> or set {ENV} "
                             "(bootstrap writes it into the generated settings)")
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise InstanceInvalid(f"the instance {root} is not a directory")
    if not (root / "rules").is_dir():
        raise InstanceInvalid(f"{root} has no rules/ directory, so it is not an instance")
    return root


def instance_repo(instance: Path) -> str | None:
    """The instance's own repository. Stage S2: its lock's `source`; stage S3: instance.json.

    None only when the instance has no lock; a lock that cannot be read is an error, not "no repository".
    """
    config, problems = project_config.load(instance)
    if problems:
        raise InstanceInvalid(f"the instance's {project_config.PATH} cannot be read ("
                              + ", ".join(p.code for p in problems) + ")")
    return config.source if config else None
