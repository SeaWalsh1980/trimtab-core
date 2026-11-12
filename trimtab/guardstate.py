"""Files the guards keep under `<config dir>/guard-state/` (ADR 0028, ADR 0063 §1).

Each level is a real directory private to this user, and each file is a private regular file,
written atomically and read without following a symlink. guard-paths refuses agent writes to
the config dir by its physical path (ADR 0029); the residual is the one ADR 0028 accepts.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

ROOT = "guard-state"
MAX_BYTES = 1 << 20
TEMP_PREFIX = ".tmp-"
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class GuardStateError(Exception):
    """A guard-state directory or file is missing, planted, or not private to this user."""


def config_dir() -> Path:
    """The Claude config directory: CLAUDE_CONFIG_DIR, else ~/.claude."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude"))


def private(st: os.stat_result) -> bool:
    return st.st_uid == os.geteuid() and not st.st_mode & 0o077


def directory(config: Path, *parts: str, create: bool) -> Path:
    """`<config>/guard-state/<parts…>`, every level a private real directory; made 0700 with `create`."""
    if not parts or any(not p or "/" in p or p in (".", "..") for p in parts):
        raise GuardStateError("a guard-state directory needs plain path parts")
    path = Path(config)
    for part in (ROOT, *parts):
        path = path / part
        if create:
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:
                pass  # checked below like any existing level
            except OSError as err:
                raise GuardStateError(f"guard-state could not be made ({type(err).__name__})") from err
        try:
            st = os.lstat(path)
        except FileNotFoundError as err:
            raise GuardStateError("the guard-state directory is missing") from err
        if not stat.S_ISDIR(st.st_mode) or not private(st):
            raise GuardStateError("the guard-state directory is not private to this user")
    return path


def read(path: Path) -> str | None:
    """The file's text, or None when there is none. A planted or oversized file raises."""
    try:
        fd = os.open(path, READ_FLAGS)
    except FileNotFoundError:
        return None
    except OSError as err:
        raise GuardStateError(f"a guard-state file cannot be opened ({type(err).__name__})") from err
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or not private(st):
            raise GuardStateError("a guard-state file is not a private regular file")
        data = fh.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise GuardStateError("a guard-state file is larger than any this base writes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as err:
        raise GuardStateError("a guard-state file is not UTF-8") from err


def write(path: Path, text: str) -> None:
    """Replace the file atomically: a private temporary file, then a rename over the name."""
    fd, temp = tempfile.mkstemp(prefix=TEMP_PREFIX, dir=path.parent)  # 0600, O_EXCL
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)  # replaces a planted symlink itself, never its target
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass  # renamed already, or never made; the original error is re-raised
        raise
