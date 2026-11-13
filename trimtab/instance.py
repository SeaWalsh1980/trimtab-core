"""instance.json: an instance's deployment values (ADR 0008).

The only reader of the file (ARC-3). `parse` is pure; `load`, `at_commit`
and `patterns_path` touch the file system. The file holds no routine or
environment ID, and has no field for one (see trimtab/routines.py).
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType

SCHEMA_VERSION = 1
FILE = "instance.json"
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
PACK = re.compile(r"^[a-z0-9-]+$")
LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,38}$")  # a user's: an App's `[bot]` can't match
APP_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,33}\[bot\]$")  # an App's name stops at 34 characters
TOP = {"schema_version", "repo", "base", "guards", "accounts", "enforcement", "secret_scan", "review", "fix_limits"}
BASE = {"repo", "sha"}
GUARDS = {"secrets_packs", "secrets_patterns"}
ACCOUNTS = {"admin", "agent", "approver"}
NO_ID_FIELDS = {"cloud", "routines"}
ENFORCEMENT = {"record", "projects"}
JOB = {"workflow", "job"}
REVIEW = {"run", "facts", "login", "rerequest_label", "tags", "review_minutes", "watch_hours"}
RUN_JOB = {"workflow", "job", "event"}
TAGS = {"fixed", "wrong", "intentional", "moot", "deferred"}
TAG = re.compile(r"^\[[a-z-]{1,20}\]$")
LABEL = re.compile(r"^[^\x00-\x1f]{1,50}$")
EVENTS = {"pull_request", "pull_request_target", "push", "workflow_run"}
ROW_ID = re.compile(r"^E([1-9]|1[0-9]|2[0-3])$")  # E1-E23, the labels AGW-1 names
WORKFLOW = re.compile(r"^\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml$")
JOB_NAME = re.compile(r"^[^\x00-\x1f]{1,100}$")
RECORD = re.compile(r"^docs/adr/[0-9]{4}-[a-z0-9-]+\.md$")


@dataclass(frozen=True)
class JobRef:
    """The Actions job whose records show a consuming project's enforcement row (ADR 0063 §2)."""

    workflow: str
    job: str


@dataclass(frozen=True)
class ReviewConfig:
    """The consuming project's review rows E2-E4 (ADR 0063 §4): the jobs, the review's login, the label and the tags."""

    run: JobRef
    run_event: str
    facts: JobRef
    login: str
    rerequest_label: str
    tags: MappingProxyType
    review_minutes: int = 60
    watch_hours: int = 24
FIX_LIMITS = {"markers", "thresholds"}
FIX_LIST_LIMIT = 200
FIX_ITEM_LIMIT = 300
CONTROL = re.compile(r"[\x00-\x1f]")
# E18's markers when the key is present and names none: the common Python and Actions skip and suppression markers.
DEFAULT_FIX_MARKERS = ("# noqa", "# type: ignore", "# pragma: no cover", "# nosec", "# pylint: disable",
                       "# fmt: off", "# fmt: skip", "@pytest.mark.skip", "@unittest.skip",
                       "continue-on-error: true")


@dataclass(frozen=True)
class FixLimits:
    """E18: literal markers a `plan/*` change may not add, and patterns for lines whose numbers it may not lower."""

    markers: tuple[str, ...]
    thresholds: tuple[str, ...]


class InstanceFileError(Exception):
    """instance.json is missing, unreadable, or breaks its schema."""


@dataclass(frozen=True)
class InstanceFile:
    repo: str
    base_repo: str
    base_sha: str
    secrets_packs: tuple[str, ...] | None  # None: every shipped pack; (): "none"
    secrets_patterns: str | None  # relative to the instance root
    admin: str | None = None  # None: the file names none, and whatever needs one fails (ADR 0056)
    agent: str | None = None  # the same
    approver: str | None = None  # the same, and an App's login
    # ADR 0063: the doctrine record holding the §Enforcement table, and each project's row jobs.
    enforcement_record: str | None = None
    enforcement_jobs: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    # ADR 0063 §5: E19's secret-scan Actions job, and the review rows' jobs, login, label and tags.
    secret_scan: JobRef | None = None
    review: ReviewConfig | None = None
    # ADR 0063 §5 E18: what a `plan/*` push may not add or lower.
    fix_limits: FixLimits | None = None


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


def _login(value: object, key: str) -> str:
    if not isinstance(value, str) or not LOGIN.fullmatch(value):
        raise InstanceFileError(f"{key} must be a GitHub user's login, never an App's")
    return value


def _app_login(value: object, key: str) -> str:
    if not isinstance(value, str) or not APP_LOGIN.fullmatch(value):
        raise InstanceFileError(f"{key} must be a GitHub App's login, ending [bot]")
    return value


def _accounts(accounts: object) -> tuple[str, str | None, str | None]:
    if not isinstance(accounts, dict):
        raise InstanceFileError("accounts must be an object")
    _keys(accounts, ACCOUNTS, "accounts.")
    admin = _login(accounts.get("admin"), "accounts.admin")
    agent = _login(accounts["agent"], "accounts.agent") if "agent" in accounts else None
    # GitHub compares logins without case, so this does.
    if agent is not None and agent.casefold() == admin.casefold():
        raise InstanceFileError("accounts.agent and accounts.admin must be different logins")
    # A user's login can't match an App's, so the approver can't collide with either.
    approver = _app_login(accounts["approver"], "accounts.approver") if "approver" in accounts else None
    return admin, agent, approver


def _job_ref(ref: object, where: str) -> JobRef:
    if not isinstance(ref, dict):
        raise InstanceFileError(f"{where} must be an object")
    _keys(ref, JOB, where + ".")
    workflow, job = ref.get("workflow"), ref.get("job")
    if not isinstance(workflow, str) or not WORKFLOW.fullmatch(workflow):
        raise InstanceFileError(f"{where}.workflow must be a workflow file under the Actions workflows folder")
    if not isinstance(job, str) or not JOB_NAME.fullmatch(job):
        raise InstanceFileError(f"{where}.job must be a job name of 1-100 printable characters")
    return JobRef(workflow, job)


def _bound(value: object, key: str, default: int, top: int) -> int:
    if value is None:
        return default
    if type(value) is not int or not 1 <= value <= top:
        raise InstanceFileError(f"{key} must be a whole number from 1 to {top}")
    return value


def _review(obj: object) -> ReviewConfig:
    if not isinstance(obj, dict):
        raise InstanceFileError("review must be an object")
    _keys(obj, REVIEW, "review.")
    run = obj.get("run")
    if not isinstance(run, dict):
        raise InstanceFileError("review.run must be an object")
    _keys(run, RUN_JOB, "review.run.")
    event = run.get("event")
    if event not in EVENTS:
        raise InstanceFileError(f"review.run.event must be one of {sorted(EVENTS)}")
    run_ref = _job_ref({k: v for k, v in run.items() if k in JOB}, "review.run")
    facts = _job_ref(obj.get("facts"), "review.facts")
    if facts == run_ref:  # E3: the facts come from a separate job, with no model step
        raise InstanceFileError("review.facts must be a different job from review.run")
    login = obj.get("login")  # the review's login may be an App's ([bot]) or a user's
    if not isinstance(login, str) or not (LOGIN.fullmatch(login) or APP_LOGIN.fullmatch(login)):
        raise InstanceFileError("review.login must be a GitHub login")
    label = obj.get("rerequest_label")
    if not isinstance(label, str) or not LABEL.fullmatch(label):
        raise InstanceFileError("review.rerequest_label must be a label name")
    tags = obj.get("tags")
    if not isinstance(tags, dict) or set(tags) != TAGS:
        raise InstanceFileError(f"review.tags must name exactly {sorted(TAGS)}")
    if not all(isinstance(v, str) and TAG.fullmatch(v) for v in tags.values()) or len(set(tags.values())) != len(TAGS):
        raise InstanceFileError("review.tags: each tag is a distinct word in square brackets, such as [fixed]")
    return ReviewConfig(run_ref, event, facts, login, label, MappingProxyType(dict(tags)),
                        _bound(obj.get("review_minutes"), "review.review_minutes", 60, 24 * 60),
                        _bound(obj.get("watch_hours"), "review.watch_hours", 24, 24 * 14))


def _strings(value: object, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > FIX_LIST_LIMIT:
        raise InstanceFileError(f"{key} must be a list of at most {FIX_LIST_LIMIT} strings")
    for item in value:
        if not isinstance(item, str) or not 1 <= len(item) <= FIX_ITEM_LIMIT or CONTROL.search(item):
            raise InstanceFileError(f"{key}: each entry is 1-{FIX_ITEM_LIMIT} printable characters")
    if len(set(value)) != len(value):
        raise InstanceFileError(f"{key} names an entry twice")
    return tuple(value)


def _fix_limits(obj: object) -> FixLimits:
    if not isinstance(obj, dict):
        raise InstanceFileError("fix_limits must be an object")
    _keys(obj, FIX_LIMITS, "fix_limits.")
    markers = _strings(obj["markers"], "fix_limits.markers") if "markers" in obj else DEFAULT_FIX_MARKERS
    thresholds = _strings(obj["thresholds"], "fix_limits.thresholds") if "thresholds" in obj else ()
    for pattern in thresholds:
        try:
            re.compile(pattern)
        except re.error as err:
            raise InstanceFileError("fix_limits.thresholds: a pattern does not compile") from err
    return FixLimits(markers, thresholds)


def _enforcement(obj: object) -> tuple[str, MappingProxyType]:
    if not isinstance(obj, dict):
        raise InstanceFileError("enforcement must be an object")
    _keys(obj, ENFORCEMENT, "enforcement.")
    record = obj.get("record")
    if not isinstance(record, str) or not RECORD.fullmatch(record):
        raise InstanceFileError("enforcement.record must be docs/adr/NNNN-name.md")
    projects = obj.get("projects", {})
    if not isinstance(projects, dict):
        raise InstanceFileError("enforcement.projects must be an object")
    out = {}
    seen: set[str] = set()
    for repo, rows in projects.items():
        _repo(repo, "enforcement.projects key")
        if repo.casefold() in seen:  # GitHub compares repository names without case
            raise InstanceFileError(f"enforcement.projects names {repo} twice, in different cases")
        seen.add(repo.casefold())
        if not isinstance(rows, dict):
            raise InstanceFileError(f"enforcement.projects.{repo} must be an object")
        jobs = {}
        for rid, ref in rows.items():
            where = f"enforcement.projects.{repo}.{rid}"
            if not ROW_ID.fullmatch(rid):
                raise InstanceFileError(f"{where}: not a row id E1-E23")
            if not isinstance(ref, dict):
                raise InstanceFileError(f"{where} must be an object")
            _keys(ref, JOB, where + ".")
            workflow, job = ref.get("workflow"), ref.get("job")
            if not isinstance(workflow, str) or not WORKFLOW.fullmatch(workflow):
                raise InstanceFileError(f"{where}.workflow must be .github/workflows/<file>.yml")
            if not isinstance(job, str) or not JOB_NAME.fullmatch(job):
                raise InstanceFileError(f"{where}.job must be a job name of 1-100 printable characters")
            jobs[rid] = JobRef(workflow, job)
        out[repo] = MappingProxyType(jobs)
    return record, MappingProxyType(out)


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
    admin, agent, approver = _accounts(data["accounts"]) if "accounts" in data else (None, None, None)
    record, jobs = _enforcement(data["enforcement"]) if "enforcement" in data else (None, MappingProxyType({}))
    return InstanceFile(
        repo=_repo(data.get("repo"), "repo"),
        base_repo=_repo(base.get("repo"), "base.repo"),
        base_sha=sha,
        secrets_packs=_packs(guards["secrets_packs"]) if "secrets_packs" in guards else None,
        secrets_patterns=_patterns(guards["secrets_patterns"]) if "secrets_patterns" in guards else None,
        admin=admin,
        agent=agent,
        approver=approver,
        enforcement_record=record,
        enforcement_jobs=jobs,
        secret_scan=_job_ref(data["secret_scan"], "secret_scan") if "secret_scan" in data else None,
        review=_review(data["review"]) if "review" in data else None,
        fix_limits=_fix_limits(data["fix_limits"]) if "fix_limits" in data else None,
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


def at_commit(root: Path, sha: str, timeout: float = 30) -> InstanceFile:
    """The instance.json committed at `sha` in the instance checkout at `root`.

    `sha` may come from a project's lock, which is untrusted, so anything but a
    hex object name is refused before git sees it.
    """
    if not isinstance(sha, str) or not COMMIT.fullmatch(sha):
        raise InstanceFileError(f"{sha!r} is not a commit name")
    try:
        done = subprocess.run(["git", "-C", str(root), "show", f"{sha}:{FILE}"],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise InstanceFileError(f"{FILE} at {sha[:12]} cannot be read ({type(err).__name__})") from err
    if done.returncode != 0:
        raise InstanceFileError(f"the instance has no {FILE} at {sha[:12]}")
    try:
        data = json.loads(done.stdout)
    except json.JSONDecodeError as err:
        raise InstanceFileError(f"{FILE} at {sha[:12]} is not valid JSON") from err
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
