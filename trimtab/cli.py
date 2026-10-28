"""Command line: argument parsing and output only. The work is in the modules."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from trimtab import bump
from trimtab import config as project_config
from trimtab import dependabot
from trimtab import instance, mergeauthority, planscope, propose, roots, routines, scrub, upstream
from trimtab.capture import citations, ingest
from trimtab.capture.prblock import check_pr_body, parse_applied, run_references
from trimtab.ids import ID_SHAPE
from trimtab.items import ITEM_ID, Problem
from trimtab.capture.sources import (
    FixtureContents, FixtureIssues, FixtureSource, GhBodies, GhContents, GhIssues, GhPullRequests, GhSource,
    SourceError, changed_files, open_feedback_issues, real_day, staged_files,
)
from trimtab.capture.written import SETTLE_SECONDS, Readback, WrittenBodyError, verify_body
from trimtab.lint import overrides, references, structure
from trimtab.propose import render
from trimtab.registry import harness, rules_for
from trimtab.registry.lookup import project_items, registry_for
from trimtab.score import THRESHOLD, WINDOW_WEEKS, candidates, tally

DEFAULT_LIMIT = 100  # every run is bounded; the watermark carries the rest
HISTORY_LIMIT = 500  # bounded: labelled PRs a ledger rebuild reads, newest first (ADR 0006)
INGEST_WEEKS = 2  # bounded: an ingest reads PRs merged this recently (ADR 0021)


def _print_problems(problems) -> int:
    for p in problems:
        print(f"FAIL  {p}", file=sys.stderr)
    return 1 if problems else 0


# ---- registry ------------------------------------------------------------

def cmd_registry(args) -> int:
    if not args.check and not args.instance:
        # A write needs the tree named: in a worktree, TRIMTAB_INSTANCE is the canonical checkout,
        # and regenerating its HARNESS.md would change the live control plane, not the branch.
        print(f"error: registry writes {harness.FILENAME}; name the tree with --instance <dir> "
              f"(the {roots.ENV} default is used only with --check)", file=sys.stderr)
        return 2
    root = _instance(args)
    if not args.check:
        problems = harness.write(root)
        if not problems:
            print(f"wrote {root / harness.FILENAME}")
        return _print_problems(problems)
    previous = None
    if args.against:
        shown = subprocess.run(["git", "-C", str(root), "show", f"{args.against}:{harness.FILENAME}"],
                               capture_output=True, text=True)
        if shown.returncode != 0:
            print(f"note: no {harness.FILENAME} at {args.against}; vanished check uses the committed file only")
        else:
            previous = shown.stdout
    problems = harness.check(root, previous=previous)
    if not problems:
        print(f"{harness.FILENAME} is current")
    return _print_problems(problems)


# ---- check-pr ------------------------------------------------------------

def _pr_body(args) -> str | None:
    """The text check-pr reads: a PR's body with gh, stdin, or a file. None after printing why not."""
    if args.pr is not None:
        if not args.repo:
            print("--pr needs --repo", file=sys.stderr)
            return None
        try:
            return GhSource(args.repo).body(args.pr)
        except SourceError as err:
            print(f"error: {err}", file=sys.stderr)
            return None
    if args.body_file == "-":
        return sys.stdin.read()
    return _text_of(args.body_file)


def _text_of(path: str) -> str | None:
    """A file's text, or None after naming it: a missing or non-UTF-8 file is a refusal, not a traceback."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        print(f"error: {path}: cannot be read as UTF-8 text ({type(err).__name__})", file=sys.stderr)
        return None


def _ids_targets(args) -> list[tuple[int, str, bytes | None]] | None:
    """(position, name, content) per file to check: the files given, by argument position (content read
    later), or what git stages, by position in its listing, with the staged content. None after printing
    why not."""
    if args.staged == bool(args.files):
        print("error: give FILE... or --staged, one or the other", file=sys.stderr)
        return None
    if not args.staged:
        return [(i, name, None) for i, name in enumerate(args.files, 1)]
    try:
        return staged_files(Path.cwd())
    except SourceError as err:
        print(f"error: --staged: {err}", file=sys.stderr)
        return None


def _shown(i: int, name: str, staged: bool) -> str:
    """How a checked file is named in output: by its position in a list the reader can reproduce when its
    name holds an ID (mixed case, as IDs are: people name files, and `env_` plus a long config name is not
    one), quoted and escaped when it holds a newline or an escape sequence (so each finding stays on one
    line and cannot pass for a plain name), and as itself otherwise."""
    if ID_SHAPE.search(name):
        if staged:
            return (f"staged file {i} (counting the paths "
                    f"`git diff --cached --name-only --no-renames --diff-filter=ACMRT` lists)")
        return f"argument {i}"
    return name if name.isprintable() else repr(name)


def cmd_ids(args) -> int:
    """What a run is about to commit, checked for a run reference (ADR 0022). No block, lock or instance.

    A file's bytes are read with undecodable ones replaced, so a binary passes
    unless it holds an ID in plain text. A name that holds one is itself a
    finding, and is shown by its position, never printed.
    """
    targets = _ids_targets(args)
    if targets is None:
        return 2
    if not targets:
        print("nothing staged to read: no run reference to check")
        return 0
    unreadable, problems = False, []
    for i, name, content in targets:
        label = _shown(i, name, args.staged)
        if ID_SHAPE.search(name):
            problems.append(Problem("id-shape", label, "its name holds a routine, environment or session ID; rename it"))
        if content is None:
            try:
                content = Path(name).read_bytes()
            except OSError as err:
                print(f"error: {label}: cannot be read ({type(err).__name__})", file=sys.stderr)
                unreadable = True
                continue
        problems += run_references(content.decode("utf-8", errors="replace"), where=f"{label}: ")
    if problems:
        _print_problems(problems)
    if unreadable:
        return 2
    if problems:
        return 1
    print(f"no run reference in {len(targets)} file(s)")
    return 0


# What verify-body says for each outcome `verify_body` can return; DIFFERS raises instead. A test holds this to
# every other outcome, so a new one cannot fall through to "the body is the one sent".
VERIFY_BODY_REPORTS = {
    Readback.SAME: "the body is the one sent",
    Readback.FOOTER: "removed the platform's session footer; GitHub's edit history still holds it",
    Readback.IN_PLACE: ("restored the attribution line the platform's session footer had replaced; "
                        "GitHub's edit history still holds the session footer"),
    Readback.IN_PLACE_GENERIC: ("restored the attribution line the platform's generic footer, linking no session, "
                                "had replaced"),
    Readback.IN_PLACE_REPLACED: ("restored the attribution line the platform's session footer had replaced; the "
                                 "platform added its generic footer, linking no session, after it; GitHub's edit "
                                 "history still holds the session footer"),
    Readback.IN_PLACE_GENERIC_KEPT: ("restored the attribution line the platform's generic footer, linking no "
                                     "session, had replaced; that footer follows it; it names nothing, so it was "
                                     "left"),
    Readback.IN_PLACE_GENERIC_AGAIN: ("the platform's generic footer, linking no session, replaced the attribution "
                                      "line again when the body sent was written back; it names nothing, and a write "
                                      "would only bring it back, so it was left; the body lacks its attribution line"),
    Readback.REPLACED: ("removed the platform's session footer; the platform added its generic footer, linking no "
                        "session, in its place; GitHub's edit history still holds the session footer"),
    Readback.GENERIC: "the body is the one sent, then the platform's generic footer, linking no session; left as it is",
}


def cmd_verify_body(args) -> int:
    """A body a run created, read back (ADR 0024): 0 as sent or changed only by the platform's footer as the report
    says, 1 changed otherwise, 2 could not tell."""
    sent = args.body if args.body is not None else _text_of(args.body_file)
    if sent is None:
        return 2
    kind, number = next((k, n) for k, n in (("pull", args.pull), ("issue", args.issue), ("comment", args.comment))
                        if n is not None)
    try:
        outcome = verify_body(GhBodies(args.repo), kind, number, sent, settle_seconds=args.settle, sleep=time.sleep)
    except SourceError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    except WrittenBodyError as err:
        print(f"verify-body: {err}. Stop and report it.", file=sys.stderr)
        return 1
    print(f"{kind} {number}: {VERIFY_BODY_REPORTS[outcome]}")
    return 0


def cmd_check_pr(args) -> int:
    prefix = None
    if args.schema_only:
        # A consumer's CI has no instance; resolving one, even from the environment, is not this mode.
        if args.instance:
            print("error: --schema-only reads no instance; it cannot be used with --instance", file=sys.stderr)
            return 2
        registry, prefix, problems = project_items(Path(args.project))
    else:
        registry, problems = registry_for(_instance(args), Path(args.project))
    if problems:
        print("cannot build the registry:", file=sys.stderr)
        return _print_problems(problems) or 1
    body = _pr_body(args)
    if body is None:
        return 2
    result = check_pr_body(body, registry, backfill=args.backfill, schema_only=args.schema_only,
                           project_prefix=prefix)
    if result.ok:
        print("PR block OK" + (" (legacy heading, backfill)" if result.block.legacy else "")
              + (" (schema only: base IDs checked for shape)" if args.schema_only else ""))
        return 0
    return _print_problems(result.problems)


# ---- ingest / propose ------------------------------------------------------

def _source(args):
    return FixtureSource(Path(args.fixture)) if args.fixture else GhSource(args.repo)


def _weeks_before(args, weeks: int) -> str | None:
    """The day `weeks` before --as-of (default today), or None when weeks is 0 or less."""
    if weeks <= 0:
        return None
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    return (as_of - timedelta(weeks=weeks)).isoformat()


def _since(args) -> str | None:
    """The first day whose evidence still counts, or None for no window."""
    return _weeks_before(args, args.window_weeks)


def _floor(args) -> str | None:
    """The first day an ingest reads merged PRs from, or None (a backfill, or --ingest-weeks 0)."""
    return None if args.backfill else _weeks_before(args, args.ingest_weeks)


def _lock(project: Path):
    """The project's lock, with any schema warning printed."""
    config, problems = project_config.load(project)
    for warning in (config.warnings if config else ()):
        print(f"warning: {project / project_config.PATH}: {warning}", file=sys.stderr)
    return config, problems


def _setup(args):
    project = Path(args.project)
    config, problems = _lock(project)
    if config is None:
        problems = problems or [f"{project / project_config.PATH} not found; the project has not adopted Trimtab"]
        for p in problems:
            print(f"FAIL  {p}", file=sys.stderr)
        return None
    registry, problems = registry_for(_instance(args), project)
    if problems:
        _print_problems(problems)
        return None
    return config, registry


def _store(args, source, config, registry):
    """The ledger: a file, or rebuilt from the PRs already labelled within the window (ADR 0006)."""
    if args.ledger:
        return ingest.FileLedgerStore(Path(args.ledger))
    store = ingest.InMemoryLedgerStore()
    if args.ledger_from_labels:
        since = _since(args)
        if since is None:
            raise SourceError("--ledger-from-labels needs a window (--window-weeks > 0) to bound the rebuild")
        pulls = source.labelled_merged(ingest.LABEL, since, args.history_limit)
        store.save(ingest.rebuild(pulls, registry, config.baseline_pr, args.backfill))
        print(f"ledger rebuilt from {len(pulls)} labelled PRs merged since {since}"
              + (f" (limit {args.history_limit} reached: older evidence not read)"
                 if len(pulls) >= args.history_limit else ""))
    return store


def _print_floor(floor) -> None:
    if floor:
        print(f"ingest floor: PRs merged before {floor} are not read (--ingest-weeks)")


def _print_limit(todo, limit) -> None:
    if todo.left != 0:
        # Stated whenever the limit cut PRs off: this run's counts are not the
        # whole window. The PRs stay unlabelled, but only a run that reads them
        # before they pass the floor records them (trimtab-core ADR 0021).
        more = "more PRs may remain" if todo.left is None else f"{todo.left} more PRs not recorded"
        print(f"limit {limit} reached: {more}; a later --apply run reads them while they are inside the "
              "--ingest-weeks floor (--limit)")


def _report(config, todo, records, since, floor, limit) -> None:
    t = tally(records, since=since)
    valid, total = t.coverage
    share = f" ({100 * valid // total}%)" if total else ""
    _print_floor(floor)
    print(f"to record: {len(todo.records)} PRs"
          + (f" ({len(todo.already_recorded)} recorded before an interrupted label step)"
             if todo.already_recorded else ""))
    _print_limit(todo, limit)
    if config.baseline_pr:
        # Stated every run, not only when a baseline PR was seen: a date-bounded
        # query usually never returns them, and the reader still needs to know.
        print(f"adoption baseline: PRs at or below #{config.baseline_pr} (adopted {config.adopted_at}) "
              f"count as ingested and are not labelled; {len(todo.baseline_skipped)} seen this run")
    if todo.newer_version:
        print(f"newer block version: {len(todo.newer_version)} PRs "
              f"({', '.join(f'#{n}' for n in todo.newer_version)}) use a block version this Trimtab does not "
              "read; left unlabelled for a newer Trimtab (trimtab-core ADR 0007)")
    window = f" merged since {since}" if since else ""
    print(f"coverage: {valid}/{total} ingested PRs{window} carry a valid block{share}")
    ids = sorted({i for (i, _) in t.feedback})
    if ids:
        print("feedback per ID (distinct PRs):")
        for item_id in ids:
            scopes = ", ".join(f"{s}: {len(prs)}" for (i, s), prs in sorted(t.feedback.items()) if i == item_id)
            print(f"  {item_id}: {t.feedback_prs(item_id)}  ({scopes})")
    else:
        print("feedback per ID: none")


def _replay(args) -> str:
    """The options that reproduce this run's plan, for the printed apply command."""
    parts = [f"--fixture {args.fixture}" if args.fixture else f"--repo {args.repo}"]
    if args.ledger:
        parts.append(f"--ledger {args.ledger}")
    if getattr(args, "ledger_from_labels", False):
        parts.append("--ledger-from-labels")
    if args.project != ".":
        parts.append(f"--project {args.project}")
    parts += [f"--limit {args.limit}", f"--window-weeks {args.window_weeks}", f"--ingest-weeks {args.ingest_weeks}"]
    if getattr(args, "ledger_from_labels", False):
        parts.append(f"--history-limit {args.history_limit}")
    if args.as_of:
        parts.append(f"--as-of {args.as_of}")
    if args.backfill:
        parts.append("--backfill")
    return " ".join(parts)


def cmd_ingest(args) -> int:
    setup = _setup(args)
    if setup is None:
        return 1
    config, registry = setup
    source = _source(args)
    try:
        store = _store(args, source, config, registry)
        ledger = store.load()
        todo = ingest.plan(source, ledger, config, registry, limit=args.limit, backfill=args.backfill,
                           floor=_floor(args))
    except SourceError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    since = _since(args)
    token = ingest.confirmation(todo)
    if not args.apply:
        print("ingest --dry-run: nothing recorded, nothing labelled")
        _report(config, todo, ingest.all_records(ledger, todo), since, _floor(args), args.limit)
        if todo.records:
            if not (args.ledger or args.ledger_from_labels):
                print("to apply, add --ledger <path> or --ledger-from-labels")
            print(f"confirm: {token}")
            print(f"to apply: trimtab ingest --apply --confirm {token} {_replay(args)}")
        return 0
    if not (args.ledger or args.ledger_from_labels):
        print("--apply needs --ledger or --ledger-from-labels: records must persist before PRs are labelled",
              file=sys.stderr)
        return 2
    try:
        done = ingest.apply(todo, ledger, store, source, confirm=args.confirm)
    except ingest.StalePlan as err:
        print(f"aborted: {err}", file=sys.stderr)
        return 3
    except SourceError as err:
        print(f"error after recording: {err}. Re-run: the record is kept and counts once.", file=sys.stderr)
        return 2
    print(f"recorded and labelled {len(done)} PRs")
    _report(config, todo, store.load().records.values(), since, _floor(args), args.limit)
    return 0


def _issues(args, repo: str):
    if args.issues_fixture:
        return FixtureIssues(Path(args.issues_fixture))
    return GhIssues(repo)


def _opening_stopped(err) -> int:
    """Report an `open_issues` that stopped part-way: what it opened, each failure (a failed create first, then a
    failed read-back after it), and what it did not try (ADR 0024).

    1 when any failure is a WrittenBodyError: a body is wrong, an issue was not read back, or a create may have
    filed its issue; a re-run would skip such an issue and never read it, so stop and report.
    2 only when every failure is a GitHub error that filed nothing (a 4xx refusal): a re-run is safe.
    Any other cause is a fault, and is raised.
    """
    for url in err.opened:
        print(f"  opened {url}")
    if err.not_opened:
        print(f"not opened: {', '.join(err.not_opened)}", file=sys.stderr)
    stops = [s for s in (err.also, err) if s is not None]  # a failed create first, then what followed it
    for stop in stops:
        print(f"{stop.stopped_at}: {stop.cause}", file=sys.stderr)
    causes = [s.cause for s in stops]
    if any(isinstance(c, WrittenBodyError) for c in causes):
        print("verify-body: a body is wrong, or an issue may exist unread. Stop and report it.", file=sys.stderr)
        return 1
    unknown = next((c for c in causes if not isinstance(c, SourceError)), None)
    if unknown is not None:
        raise unknown
    print("Re-run: open issues are skipped, so nothing is filed twice.", file=sys.stderr)
    return 2


def _check_ids(text: str) -> dict[str, tuple[str, ...]]:
    """`ID=YYYY-MM-DD+YYYY-MM-DD,...` -> {ID: report days}; ValueError naming the first entry that is not of that
    form: an ID not shaped like an item ID, a day that is not a real date, or an ID given twice."""
    found: dict[str, tuple[str, ...]] = {}
    for entry in filter(None, (e.strip() for e in text.split(","))):
        item_id, _, days = (part.strip() for part in entry.partition("="))
        parts = tuple(day.strip() for day in days.split("+"))
        if not ITEM_ID.fullmatch(item_id) or not all(real_day(day) for day in parts):
            raise ValueError(f"--check-ids: {entry!r} is not ID=YYYY-MM-DD[+YYYY-MM-DD...]")
        if item_id in found:
            raise ValueError(f"--check-ids: {item_id} is given twice; list all its report days in one entry")
        found[item_id] = parts
    return found


def cmd_propose(args) -> int:
    try:
        check_ids = _check_ids(args.check_ids)
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1  # the command is wrong, not a listing: 2 means one could not be read
    if check_ids and (args.apply or not args.check_open):
        print("error: --check-ids goes with a --check-open dry run", file=sys.stderr)
        return 1
    setup = _setup(args)
    if setup is None:
        return 1
    config, registry = setup
    source = _source(args)
    issues_repo = args.issues_repo or roots.instance_repo(_instance(args))  # read once (ENG-6.2)
    try:
        store = _store(args, source, config, registry)
        ledger = store.load()
        todo = ingest.plan(source, ledger, config, registry, limit=args.limit, backfill=args.backfill,
                           floor=_floor(args))
    except SourceError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    _print_floor(_floor(args))
    _print_limit(todo, args.limit)  # a cut-off run's tally is short, so a proposal can be missing
    proposals = candidates(tally(ingest.all_records(ledger, todo), since=_since(args)), threshold=args.threshold)
    project = args.repo or config.id_prefix
    drafts = [render(p, registry, project=project) for p in proposals]
    if not args.apply:
        print(f"propose --dry-run: {len(drafts)} proposal(s) at threshold {args.threshold}; nothing opened")
    # The issues and the project's PRs are read apart: a listing that cannot be read marks its own drafts NOT
    # CHECKED and ends the run 2, but never hides what the other listing found. Every mark is keyed by the draft's
    # kind as well as its ID: one ID can reach the threshold in both scopes, and each draft takes only its own marks.
    marks: dict[tuple[str, str], str] = {}
    unreadable: list[SourceError] = []
    try:
        plan = (propose.plan_issues(drafts, _issues(args, issues_repo), closed_since=_since(args))
                if (args.apply or args.check_open) else None)
    except SourceError as err:
        if args.apply:
            print(f"error: {err}", file=sys.stderr)
            return 2
        plan = None
        unreadable.append(err)
        marks.update({("issue", d.item_id): f"NOT CHECKED: the issue listing could not be read ({err})"
                      for d in drafts if d.kind == "issue"})
    if plan is not None:
        marks.update({("issue", i): "SKIPPED: an issue for this ID is open or was closed within the window"
                      for i in plan.already_open})
    pr_error = None
    try:
        held = (propose.plan_project_prs(drafts, source, closed_since=_since(args), threshold=args.threshold,
                                         check_ids=check_ids)
                if args.check_open and not args.apply else {})
    except SourceError as err:
        pr_error = err
        unreadable.append(err)
        unchecked = f"NOT CHECKED: the PR listing could not be read ({err})"
        held = {d.item_id: unchecked for d in drafts if d.kind == "pr"} | {i: unchecked for i in check_ids}
    drafted = {d.item_id for d in drafts if d.kind == "pr"}
    marks.update({("pr", i): mark for i, mark in held.items() if i in drafted})
    if not args.apply:
        if (args.check_open and _since(args) is None and pr_error is None
                and (any(d.kind == "pr" for d in drafts) or check_ids)):
            print("no window: merged and closed [<ID>] PRs were not checked; only open ones")
        for d in drafts:
            where = f"issue on {issues_repo}" if d.kind == "issue" else "draft PR in this project (session step)"
            if (d.kind, d.item_id) in marks:
                where += f", {marks[(d.kind, d.item_id)]}"
            labels = f" [labels: {', '.join(d.labels)}]" if d.labels else ""
            print(f"\n--- would open: {where}{labels}\n# {d.title}\n\n{d.body}")
        for item_id in check_ids:
            if item_id not in drafted:
                print(f"\n--- also checked: {item_id}" + (f", {held[item_id]}" if item_id in held else ", no mark"))
        if plan is not None and plan.to_open:
            token = propose.confirmation(plan)
            print(f"\nconfirm: {token}")
            print(f"to open {len(plan.to_open)} issue(s): trimtab propose --apply --confirm {token} {_replay(args)}"
                  f" --threshold {args.threshold}"
                  + (f" --issues-fixture {args.issues_fixture}" if args.issues_fixture else "")
                  + (f" --issues-repo {args.issues_repo}" if args.issues_repo else ""))
        for err in unreadable:
            print(f"error: {err}", file=sys.stderr)
        if unreadable:
            return 2
        return 0
    try:
        opened = propose.open_issues(plan, _issues(args, issues_repo), confirm=args.confirm)
    except propose.StaleProposals as err:
        print(f"aborted: {err}", file=sys.stderr)
        return 3
    except propose.OpeningStopped as err:
        return _opening_stopped(err)
    print(f"opened {len(opened)} issue(s); skipped {len(plan.already_open)} open or recently closed")
    for url in opened:
        print(f"  {url}")
    return 0


# ---- rules-for / citations -------------------------------------------------

def _rules_dir(project: Path) -> str:
    config, _ = _lock(project)
    return config.rules_dir if config else project_config.ProjectConfig.rules_dir


def cmd_rules_for(args) -> int:
    project = Path(args.project).resolve()
    base = _instance(args)
    try:
        rules = rules_for.corpus(project, _rules_dir(project), base)
        by_path = rules_for.governing(args.paths, rules, project)
    except (OSError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    def read(rule):
        where = base / rule.source[len("base:"):] if rule.source.startswith("base:") else project / rule.source
        return where.read_text(encoding="utf-8")

    sys.stdout.write(rules_for.render(by_path, project, structure.INSTRUCTION_FILES, cat=args.cat, read=read))
    return 0


def cmd_citations(args) -> int:
    project = Path(args.project).resolve()
    base = _instance(args)
    registry, problems = registry_for(base, project)
    if problems:
        print("cannot build the registry:", file=sys.stderr)
        return _print_problems(problems) or 1
    try:
        files = args.files if args.files else changed_files(project, args.base)
        rules = rules_for.corpus(project, _rules_dir(project), base)
        by_path = rules_for.governing(files, rules, project)
    except (OSError, ValueError, SourceError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    present = args.plan_file is not None
    applied, bad = parse_applied(Path(args.plan_file).read_text(encoding="utf-8"), registry) if present else ((), ())
    report = citations.check((a.id for a in applied), by_path, rules,
                             unknown=sum(1 for p in bad if p.code == "unknown-id"))
    print(f"scope: {len(by_path)} changed file(s)")
    for label, ids in (("uncited binding (path-scoped, in scope)", report.uncited_binding),
                       ("cited out of scope (path-scoped, reaches no changed file)", report.out_of_scope),
                       ("cited always-loaded (judge by subject)", report.cited_always)):
        print(f"{label}: {', '.join(ids) if ids else 'none'}")
    for p in bad:
        print(f"PLAN  {p}")
    print(report.line(plan_present=present))
    return 0


# ---- bump --------------------------------------------------------------------

def cmd_bump(args) -> int:
    project = Path(args.project)
    base = _instance(args)
    to = subprocess.run(["git", "-C", str(base), "rev-parse", "--verify", f"{args.to}^{{commit}}"],
                        capture_output=True, text=True)
    if to.returncode != 0:
        print(f"error: {args.to} is not a commit in the instance {base}", file=sys.stderr)
        return 2
    _lock(project)  # for its warnings; bump.plan reads and checks the lock itself
    try:
        todo = bump.plan(project, base, to.stdout.strip())
    except bump.BumpError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    if todo.is_current:
        print(f"the lock is already at {todo.new[:12]}; nothing to bump")
        return 0
    if not args.apply:
        print(f"bump --dry-run: {todo.old[:12]} -> {todo.new[:12]}; nothing written")
        print(f"base_sha: {(todo.base_old or 'none')[:12]} -> {todo.base_new[:12]}")
        print(f"changed items: {len(todo.changes)}")
        for c in todo.changes:
            print(f"  {c.id:<10} {c.what:<10} {c.detail}")
        print(f"overrides to re-confirm or drop: {len(todo.flagged)}")
        for item_id, n in todo.flagged:
            print(f"  {item_id} ({bump.OVERRIDES} entry {n})")
        if args.body_file:
            Path(args.body_file).write_text(bump.pr_body(todo, roots.instance_repo(base)), encoding="utf-8")
            print(f"PR body written to {args.body_file}")
        token = bump.confirmation(todo)
        print(f"confirm: {token}")
        print(f"to apply: trimtab bump --apply --confirm {token} --to {todo.new}"
              + (f" --project {args.project}" if args.project != "." else ""))
        return 0
    try:
        bump.apply(project, todo, args.confirm)
    except bump.BumpError as err:
        print(f"aborted: {err}", file=sys.stderr)
        return 3
    print(f"lock moved to {todo.new[:12]} in {project / project_config.PATH}")
    return 0


# ---- dependabot-block --------------------------------------------------------

def _print_block_plan(todo: dependabot.BlockPlan) -> None:
    # Dependabot's text carries third-party release notes: shown by size and digest, never verbatim.
    print(f"PR #{todo.number} in {todo.repo}, opened by {dependabot.DEPENDABOT_LOGIN}, head {todo.head_sha[:12]}")
    print(f"existing body: {todo.old_length} chars, sha256 {todo.old_digest[:12]}, kept unchanged as the prefix")
    if todo.new_body is None:
        print("body: a valid harness block is already there; nothing to write")
    else:
        print("body: append this block:")
        print(dependabot.BLOCK, end="")
    newest = todo.newest_run
    if todo.rerun_run_id is not None:
        print(f"re-run: the failed jobs of {todo.workflow} run {todo.rerun_run_id}")
    elif newest is None:
        print(f"re-run: none; no {todo.workflow} run found at this head (check --workflow, or whether CI started)")
    elif todo.run_in_progress:
        print(f"re-run: none; the newest {todo.workflow} run at this head is still going")
        if todo.run_predates_block:
            print(PREDATES_NOTE)
    elif newest.conclusion == "success":
        print(f"re-run: none; the newest {todo.workflow} run at this head passed")
    else:
        print(f"re-run: none; the newest {todo.workflow} run at this head ended {todo.shown_conclusion}")


PREDATES_NOTE = "note: this run started before the block was added; if it fails, run the command again"

# dependabot-block's own exit code for "done, but CI at the head needs a look": 1 stays "refused".
EXIT_CI_ATTENTION = 4
DEPENDABOT_EXIT_CODES = ("exit codes: 0 done, or nothing to do; 1 refused, the registry could not be built, "
                         "or the written body did not read back; 2 a GitHub or usage error; 3 the PR or its "
                         f"runs changed since the dry run; {EXIT_CI_ATTENTION} done (or planned), but CI at the "
                         "head has no run, or its newest run ended neither passed nor failed")


def _attention(needs_attention: bool, workflow: str, number: int) -> int:
    """EXIT_CI_ATTENTION, with the reason, when CI at the head is neither green, going, nor about to be re-run."""
    if not needs_attention:
        return 0
    print(f"attention: CI is not fixed by this command; look at {workflow} for PR #{number} by hand",
          file=sys.stderr)
    return EXIT_CI_ATTENTION


def _dependabot_apply_command(args, token: str) -> str:
    """The apply command for this dry run, each value shell-quoted so it pastes as one argument."""
    argv = ["trimtab", "dependabot-block", str(args.pr), "--repo", args.repo, "--workflow", args.workflow,
            "--apply", "--confirm", token]
    if args.project != ".":
        argv += ["--project", args.project]
    if args.instance:
        argv += ["--instance", args.instance]
    return shlex.join(argv)


def cmd_dependabot_block(args, host_for=GhPullRequests, registry=None) -> int:
    if registry is None:
        registry, problems = registry_for(_instance(args), Path(args.project))
        if problems:
            print("cannot build the registry:", file=sys.stderr)
            return _print_problems(problems) or 1
    host = host_for(args.repo)
    try:
        if not args.apply:
            todo = dependabot.read_plan(host, registry, args.repo, args.workflow, args.pr)
            print("dependabot-block --dry-run: nothing written")
            _print_block_plan(todo)
            if not todo.nothing_to_do:
                token = dependabot.confirmation(todo)
                print(f"confirm: {token}")
                print(f"to apply: {_dependabot_apply_command(args, token)}")
            return _attention(todo.ci_needs_attention, args.workflow, args.pr)
        done = dependabot.apply(host, registry, args.repo, args.workflow, args.pr, args.confirm)
    except (dependabot.NotDependabot, dependabot.BlockRefused) as err:
        print(f"refused: {err}", file=sys.stderr)
        _print_problems(getattr(err, "problems", ()))
        return 1
    except dependabot.StaleBlockPlan as err:
        print(f"aborted: {err}", file=sys.stderr)
        return 3
    except dependabot.ReadBackFailed as err:
        cause = f": {err.__cause__}" if err.__cause__ else ""
        print(f"error: {err}{cause}; running the dry run again is safe", file=sys.stderr)
        return 1
    except dependabot.RerunFailed as err:
        written = "the body was written; " if err.wrote_body else ""
        print(f"error: {err}: {err.__cause__}; {written}running the dry run again is safe", file=sys.stderr)
        return 2
    except dependabot.HostError as err:
        print(f"error: {err}; running the dry run again is safe", file=sys.stderr)
        return 2
    print(f"PR #{args.pr}: " + ("block added" if done.wrote_body else "block already there")
          + (f"; re-run of run {done.reran} requested" if done.reran is not None else "; no re-run"))
    if done.run_predates_block:
        print(PREDATES_NOTE)
    return _attention(done.ci_needs_attention, args.workflow, args.pr)


# ---- upstream ----------------------------------------------------------------

def _behind(sha: str | None, base: Path) -> str:
    """Commits between a consumer's lock and the instance's HEAD. The SHA is untrusted data."""
    if not project_config.is_commit_sha(sha):
        return "?"
    done = subprocess.run(["git", "-C", str(base), "rev-list", "--count", "--end-of-options", f"{sha}..HEAD"],
                          capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else "unknown SHA"


def cmd_upstream(args) -> int:
    base = _instance(args)
    consumers = Path(args.consumers) if args.consumers else base / upstream.CONSUMERS
    repo = args.repo or roots.instance_repo(base)
    try:
        repos = upstream.load_consumers(consumers.read_text(encoding="utf-8"))
        files = FixtureContents(Path(args.files_fixture)) if args.files_fixture else GhContents()
        if args.issues_fixture:
            rows = json.loads(Path(args.issues_fixture).read_text(encoding="utf-8"))
            issues = [upstream.Issue(r["number"], r["title"], r.get("body", "")) for r in rows
                      if propose.ISSUE_LABEL in r.get("labels", []) and r.get("state", "open") == "open"]
        else:
            issues = open_feedback_issues(repo, propose.ISSUE_LABEL)
        ev = upstream.gather(repos, files, issues)
    except (OSError, upstream.ConsumersError, SourceError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    print(f"upstream: {len(ev.consumers)} consumer(s), {sum(len(v) for v in ev.issues.values())} open "
          f"{propose.ISSUE_LABEL} issue(s); read only, nothing opened")
    print("consumers (lock: commits behind this Trimtab's HEAD):")
    for c in ev.consumers:
        lock = f"{c.trimtab_sha[:12]} ({_behind(c.trimtab_sha, base)} behind)" if c.trimtab_sha else "-"
        print(f"  {c.repo:<40} {c.status:<14} {lock}  overrides: {', '.join(c.overridden) or 'none'}")
    for title, table in (("overridden, by consumers", ev.overrides), ("reported upstream, by project", ev.reports)):
        print(f"{title}:")
        for item_id in sorted(table):
            print(f"  {item_id}: {len(table[item_id])}  ({', '.join(sorted(table[item_id]))})")
        if not table:
            print("  none")
    if ev.unattributed:
        print(f"note: {ev.unattributed} issue(s) name an ID but no project; they cannot count toward 2+ consumers")
    found = ev.candidates(args.threshold)
    print(f"candidates at {args.threshold}+ consumers: {len(found)}")
    for item_id, why, n in found:
        print(f"  {item_id}: {why} by {n}; issues {', '.join(f'#{i}' for i in ev.issues.get(item_id, [])) or '-'}")
    return 0


# ---- instance / scrub ------------------------------------------------------

def cmd_instance(args) -> int:
    base = _instance(args)
    if args.root:  # for the agents: the doctrine root, which a lock it cannot read must not block
        print(base)
        return 0
    if args.check:  # for the instance's CI: its own files load
        try:
            instance.load(base)
        except instance.InstanceFileError as err:  # its messages name the key, not the file
            print(f"error: instance.json: {err}", file=sys.stderr)
            return 2
        try:
            path = base / upstream.CONSUMERS
            if not path.is_file():
                raise upstream.ConsumersError(f"{upstream.CONSUMERS} is missing")
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as err:
                raise upstream.ConsumersError(
                    f"{upstream.CONSUMERS} cannot be read ({type(err).__name__})") from err
            n = len(upstream.load_consumers(text))
        except upstream.ConsumersError as err:  # its messages already name consumers.json
            print(f"error: {err}", file=sys.stderr)
            return 2
        print(f"instance: ok ({n} consumer(s))")
        return 0
    if args.json:  # for bootstrap: the validated values it writes
        try:
            found = instance.load(base)
            patterns = instance.patterns_path(found, base)
        except instance.InstanceFileError as err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        print(json.dumps({
            "schema_version": instance.SCHEMA_VERSION, "root": str(base), "repo": found.repo,
            "base": {"repo": found.base_repo, "sha": found.base_sha},
            "env": {"TRIMTAB_SECRET_PACKS": instance.packs_env(found),
                    "TRIMTAB_SECRET_PATTERNS": str(patterns) if patterns else None},
        }))
        return 0
    repo = roots.instance_repo(base)
    if args.repo:  # for commands that need the instance's repository, bare
        print(repo)
        return 0
    print(f"root: {base}")
    print(f"repo: {repo}")
    return 0


def cmd_scrub(args) -> int:
    tree = Path(args.tree)
    try:
        terms = (scrub.public_terms(args.self_repo) if args.public
                 else scrub.private_terms(_instance(args), environ=os.environ))
        hits = scrub.scan(tree, terms)
    except scrub.ScrubError as err:
        print(f"error: {err}; nothing is vouched for", file=sys.stderr)
        return 2
    for h in hits:
        print(f"HIT   {h.path}:{h.line}: {h.kind}")
    print(f"scrub: {len(hits)} hit(s) in {tree}")
    return 1 if hits else 0


def cmd_check_plan_scope(args) -> int:
    try:
        result = planscope.check(args.repo, base=args.base, head=args.head, head_ref=args.head_ref,
                                 base_ref=args.base_ref, default_branch=args.default_branch)
    except planscope.PlanScopeError as err:
        print(f"error: {err}; nothing is vouched for", file=sys.stderr)
        return 2
    if result.ref is planscope.Ref.NOT_PLAN:
        print(f"plan-scope: skipped; {args.head_ref!r} is not a plan branch")
        return 0
    if result.ref is planscope.Ref.MALFORMED:
        print(f"FAIL  {args.head_ref!r} is under plan/ but is not plan/<plan>/item-NN-partK", file=sys.stderr)
        return 1
    _print_problems(result.problems)
    print(f"plan-scope: {len(result.problems)} problem(s) for plan {result.plan}")
    return 1 if result.problems else 0


def cmd_check_merge_authority(args) -> int:
    pr = mergeauthority.Pr(repository=args.repository, head_repo=args.head_repo, head_ref=args.head_ref,
                           head=args.head, base_ref=args.base_ref, default_branch=args.default_branch)
    try:
        who = mergeauthority.accounts(args.admin, args.approver)
        result = mergeauthority.check(args.repo, pr, args.pr, args.base, who)
    except mergeauthority.MergeAuthorityError as err:
        print(f"error: {err}; nothing is vouched for", file=sys.stderr)
        return 2
    if result.problems:
        _print_problems(result.problems)
        print(f"merge-authority: {len(result.problems)} problem(s) for PR {args.pr}")
        return 1
    print(f"merge-authority: PR {args.pr} approved by {result.approved_by} on {args.head[:12]}")
    return 0


def cmd_routine_render(args) -> int:
    pairs = {}
    for item in args.set or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            print(f"error: --set wants KEY=VALUE, got {item!r}", file=sys.stderr)
            return 2
        pairs[key] = value
    try:
        spec = routines.render(args.name, routines.values(_instance(args), pairs))
    except routines.RenderError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    print(json.dumps(spec, indent=2))
    return 0


# ---- lint ------------------------------------------------------------------

def _plugin_cache() -> Path | None:
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    cache = home / "plugins" / "cache"
    return cache if cache.is_dir() else None


def cmd_lint(args) -> int:
    project = Path(args.project)
    config, problems = _lock(project)
    if problems:
        return _print_problems(problems)
    which = [args.check] if args.check != "all" else ["overrides", "references", "structure"]
    base = _instance(args)
    failed = 0
    if "overrides" in which:
        found = overrides.lint(project, base)
        print(f"overrides: {len(found)} problem(s)")
        failed |= _print_problems(found)
    if "references" in which:
        try:
            enabled = references.enabled_plugins([project, base])  # settings are instance content
        except references.SettingsUnreadable as err:
            print(f"error: {err}", file=sys.stderr)
            failed = 1
        else:
            found = references.lint(project, roots.code_root(), enabled, plugin_cache=_plugin_cache())
            print(f"references: {len(found)} unresolved")
            failed |= _print_problems(found)
    if "structure" in which:
        rules_dirs = [config.rules_dir if config else project_config.ProjectConfig.rules_dir]
        if project.resolve() == base.resolve():
            rules_dirs.append("rules")  # the instance's base rules load in every session
        previous = json.loads(Path(args.previous).read_text(encoding="utf-8")) if args.previous else None
        budget = config.always_loaded_budget_kb if config else project_config.ProjectConfig.always_loaded_budget_kb
        if args.against:
            try:
                previous = structure.measure_at(project, args.against, rules_dirs, budget).to_dict()
            except structure.TreeError as err:
                print(f"error: {err}", file=sys.stderr)
                return 2
            print(f"growth baseline: the tree at {args.against}, {previous['total_bytes']} bytes")
        report = structure.measure(project, rules_dirs, budget, previous)
        print(f"structure: {report.total_bytes} bytes always loaded (budget {report.budget_bytes})")
        for f in report.files:
            print(f"  {f.bytes:>7}  {f.lines:>4} lines  {f.path}")
        for p in report.findings:
            print(f"PROPOSE  {p}")
        if args.sections:
            print("largest sections (a proposal moves one; binding ones stay always-loaded):")
            for sec in structure.sections(project, report, top=args.sections):
                keep = "  [REQUIRE/PROHIBIT: stays]" if sec.binding else ""
                print(f"  {sec.bytes:>7}  {sec.path}: {sec.heading}{keep}")
        if args.json:
            Path(args.json).write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
        if args.strict and report.findings:
            failed = 1
    return failed


# ---- parser ----------------------------------------------------------------

def _positive_int(text: str) -> int:
    """A count of at least 1: a run that records nothing, or slices from the end, is refused."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"{value} is below 1")
    return value


def _ingest_options(p) -> None:
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--repo", help="owner/name of the project's GitHub repository")
    src.add_argument("--fixture", help="JSON file of PRs, for scratch runs and tests")
    p.add_argument("--project", default=".", help="project root holding .claude/trimtab.json (default: .)")
    led = p.add_mutually_exclusive_group()
    led.add_argument("--ledger", help="ledger JSON file")
    led.add_argument("--ledger-from-labels", action="store_true",
                     help="rebuild the ledger from PRs already labelled, within the window (routines: trimtab-core ADR 0006)")
    p.add_argument("--limit", type=_positive_int, default=DEFAULT_LIMIT, help=f"PRs per run (default {DEFAULT_LIMIT})")
    p.add_argument("--history-limit", type=int, default=HISTORY_LIMIT,
                   help=f"labelled PRs read by --ledger-from-labels (default {HISTORY_LIMIT})")
    p.add_argument("--window-weeks", type=int, default=WINDOW_WEEKS,
                   help=f"evidence older than this has decayed; 0 for none (default {WINDOW_WEEKS})")
    p.add_argument("--ingest-weeks", type=int, default=INGEST_WEEKS,
                   help=f"read PRs merged within this many weeks, never before the adoption; 0 for since the "
                        f"adoption (default {INGEST_WEEKS})")
    p.add_argument("--as-of", metavar="YYYY-MM-DD", help="the date the window ends (default: today)")
    p.add_argument("--backfill", action="store_true", help="include PRs below the baseline; read the legacy heading")


def _instance(args) -> Path:
    """The instance root for this run: --instance, then TRIMTAB_INSTANCE (ADR 0008)."""
    return roots.instance_root(getattr(args, "instance", None))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trimtab", description="Trimtab harness tooling.")
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--instance", metavar="DIR",
                        help=f"the instance (doctrine) root; default: ${roots.ENV}")

    reg = sub.add_parser("registry", parents=[common], help="generate or check HARNESS.md")
    reg.add_argument("--root", dest="instance", metavar="DIR", help="alias of --instance")
    reg.add_argument("--check", action="store_true", help="fail if stale, duplicated or vanished; write nothing")
    reg.add_argument("--against", metavar="REF", help="also fail on IDs listed in HARNESS.md at REF but gone now")
    reg.set_defaults(func=cmd_registry)

    chk = sub.add_parser("check-pr", parents=[common],
                         help="validate a PR body's harness block, and refuse a routine, environment or session "
                              "ID anywhere in the body")
    body = chk.add_mutually_exclusive_group(required=True)
    body.add_argument("--body-file", help="file holding the PR body, or - for stdin")
    body.add_argument("--pr", type=int, help="read the body of this PR with gh (needs --repo)")
    chk.add_argument("--repo", help="owner/name, with --pr")
    chk.add_argument("--project", default=".", help="project root, for its own IDs (default: .)")
    chk.add_argument("--backfill", action="store_true", help="accept the legacy `## Rules applied` heading")
    chk.add_argument("--schema-only", action="store_true",
                     help="read no instance: check the block's form, resolve only the project's own IDs, "
                          "and check every other ID for shape (a consumer's CI)")
    chk.set_defaults(func=cmd_check_pr)

    ids = sub.add_parser("ids", help="check files a run is about to commit for a routine, environment or "
                                     "session ID; no block, lock or instance is read")
    ids.add_argument("files", nargs="*", metavar="FILE", help="the files to check")
    ids.add_argument("--staged", action="store_true",
                     help="check what the git index stages instead (added, copied, modified or renamed files)")
    ids.set_defaults(func=cmd_ids)

    vb = sub.add_parser("verify-body", help="read back a PR, issue or comment a run created; take the platform's "
                                            "footer off or restore the attribution line it replaced, and fail on any "
                                            "other change (ADR 0024)")
    vb.add_argument("--repo", required=True, help="owner/name")
    target = vb.add_mutually_exclusive_group(required=True)
    target.add_argument("--pull", type=int, metavar="N", help="PR number (the POST's `--jq .number`)")
    target.add_argument("--issue", type=int, metavar="N", help="issue number")
    target.add_argument("--comment", type=int, metavar="ID", help="issue or PR comment ID")
    sent = vb.add_mutually_exclusive_group(required=True)
    sent.add_argument("--body-file", help="the file the body was sent from")
    sent.add_argument("--body", help="the body as sent, inline")
    vb.add_argument("--settle", type=float, default=SETTLE_SECONDS, metavar="SECONDS",
                    help=f"wait before each read, for a footer added after the write (default: {SETTLE_SECONDS:g})")
    vb.set_defaults(func=cmd_verify_body)

    ing = sub.add_parser("ingest", parents=[common], help="record merged PRs by watermark (dry run unless --apply)")
    _ingest_options(ing)
    mode = ing.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report only (the default)")
    mode.add_argument("--apply", action="store_true", help="record and label; needs --confirm and --ledger")
    ing.add_argument("--confirm", help="the token a dry run printed")
    ing.set_defaults(func=cmd_ingest)

    pro = sub.add_parser("propose", parents=[common], help="print the drafts the loop would open; open upstream issues with --apply")
    _ingest_options(pro)
    pmode = pro.add_mutually_exclusive_group()
    pmode.add_argument("--dry-run", action="store_true", help="print only (the default)")
    pmode.add_argument("--apply", action="store_true", help="open the upstream issues; needs --confirm")
    pro.add_argument("--confirm", help="the token a dry run printed")
    pro.add_argument("--check-ids", default="",
                     help="with --check-open: also mark IDs with no draft, as ID=YYYY-MM-DD+YYYY-MM-DD,... giving "
                          "the day of every conformance report naming the ID")
    pro.add_argument("--check-open", action="store_true",
                     help="in a dry run, also read existing issues and this project's PRs to show what would be "
                          "skipped or is already resolved, and print a token")
    iss = pro.add_mutually_exclusive_group()
    iss.add_argument("--issues-repo", help="where upstream issues go (default: the instance's repository)")
    iss.add_argument("--issues-fixture", help="JSON file of issues, for scratch runs and tests")
    pro.add_argument("--threshold", type=int, default=THRESHOLD, help=f"distinct PRs per ID (default {THRESHOLD})")
    pro.set_defaults(func=cmd_propose)

    rf = sub.add_parser("rules-for", parents=[common], help="the rule files and item IDs that govern the given paths")
    rf.add_argument("paths", nargs="+", help="project-relative files the change will touch (need not exist)")
    rf.add_argument("--project", default=".", help="project root (default: .)")
    rf.add_argument("--cat", action="store_true", help="also print every matched rule file")
    rf.set_defaults(func=cmd_rules_for)

    cit = sub.add_parser("citations", parents=[common], help="a plan's cited IDs against the rules a diff's paths pull in")
    cit.add_argument("--plan-file", help="plan or PR body holding `## Harness items applied`; omit if unplanned")
    scope = cit.add_mutually_exclusive_group(required=True)
    scope.add_argument("--base", help="diff HEAD against its merge base with this ref")
    scope.add_argument("--files", nargs="+", help="the changed files, e.g. a PR's file list")
    cit.add_argument("--project", default=".", help="project root (default: .)")
    cit.set_defaults(func=cmd_citations)

    bmp = sub.add_parser("bump", parents=[common], help="move the project's lock to a newer Trimtab (dry run unless --apply)")
    bmp.add_argument("--to", default="HEAD", help="instance ref to move to (default: the instance's HEAD)")
    bmp.add_argument("--project", default=".", help="project root (default: .)")
    bmode = bmp.add_mutually_exclusive_group()
    bmode.add_argument("--dry-run", action="store_true", help="report only (the default)")
    bmode.add_argument("--apply", action="store_true", help="write the new SHA; needs --confirm")
    bmp.add_argument("--confirm", help="the token a dry run printed")
    bmp.add_argument("--body-file", help="in a dry run, write the draft PR's body here")
    bmp.set_defaults(func=cmd_bump)

    dep = sub.add_parser("dependabot-block", parents=[common],
                         help="add the harness block to a Dependabot PR and re-run its failed CI (dry run unless --apply)",
                         epilog=DEPENDABOT_EXIT_CODES)
    dep.add_argument("pr", type=int, help="the PR number")
    dep.add_argument("--repo", required=True, help="owner/name")
    dep.add_argument("--workflow", required=True, help="the workflow whose failed run to re-run, by file name, e.g. ci.yml")
    dep.add_argument("--project", default=".", help="project root, for its own IDs (default: .)")
    dmode = dep.add_mutually_exclusive_group()
    dmode.add_argument("--dry-run", action="store_true", help="report only (the default)")
    dmode.add_argument("--apply", action="store_true", help="write the body and re-run; needs --confirm")
    dep.add_argument("--confirm", help="the token a dry run printed")
    dep.set_defaults(func=cmd_dependabot_block)

    up = sub.add_parser("upstream", parents=[common], help="the config loop's evidence: overrides and reports per base ID (read only)")
    up.add_argument("--consumers", help=f"consumers.json (default: the instance's {upstream.CONSUMERS})")
    up.add_argument("--repo", help="where harness-feedback issues live (default: the instance's repository)")
    up.add_argument("--files-fixture", help="directory of consumer files (<owner>/<name>/<path>), for tests")
    up.add_argument("--issues-fixture", help="JSON file of issues, for tests")
    up.add_argument("--threshold", type=int, default=upstream.THRESHOLD, help="distinct consumers per ID (default 2)")
    up.set_defaults(func=cmd_upstream)

    lnt = sub.add_parser("lint", parents=[common], help="static checks: overrides, references, structure")
    lnt.add_argument("check", nargs="?", default="all", choices=["all", "overrides", "references", "structure"])
    lnt.add_argument("--project", default=".", help="project root (default: .)")
    base = lnt.add_mutually_exclusive_group()
    base.add_argument("--previous", help="a previous structure report (--json) for the growth check")
    base.add_argument("--against", metavar="REF", help="measure growth against the tree at this git ref")
    lnt.add_argument("--sections", type=int, nargs="?", const=10, default=0, metavar="N",
                     help="also list the N largest sections of the always-loaded files (default 10)")
    lnt.add_argument("--json", help="write the structure report here")
    lnt.add_argument("--strict", action="store_true", help="exit 1 on structure findings too")
    lnt.set_defaults(func=cmd_lint)

    ins = sub.add_parser("instance", parents=[common],
                         help="print the instance root and repository, from instance.json; --check checks its files load")
    only = ins.add_mutually_exclusive_group()
    only.add_argument("--repo", action="store_true", help="print only the instance's repository (owner/name)")
    only.add_argument("--root", action="store_true",
                      help="print only the instance root (the doctrine); never reads instance.json")
    only.add_argument("--json", action="store_true",
                      help="print instance.json's validated values and the env bootstrap writes, as JSON (schema 1)")
    only.add_argument("--check", action="store_true",
                      help="check that the instance's instance.json and consumers.json load (for its CI)")
    ins.set_defaults(func=cmd_instance)

    scr = sub.add_parser("scrub", parents=[common], help="check a tree before it is pushed to the public base")
    scr.add_argument("--tree", required=True, help="the tree to check (a trimtab-core clone)")
    scr.add_argument("--public", action="store_true", help="generic shapes only; needs no instance")
    scr.add_argument("--self", dest="self_repo", metavar="OWNER/NAME", help="with --public: this repository")
    scr.set_defaults(func=cmd_scrub)

    ps = sub.add_parser("check-plan-scope",
                        help="fail a plan run's PR that changes a path outside its plan's scope, read from the "
                             "base commit (ADR 0042)")
    ps.add_argument("--base", required=True, metavar="SHA", help="the PR's base commit, from the platform's event")
    ps.add_argument("--head-ref", required=True, metavar="BRANCH",
                    help="the PR's head branch name, from the platform's event; a non-plan branch is skipped")
    ps.add_argument("--head", required=True, metavar="SHA",
                    help="the PR's head commit; required, since the job runs from a base checkout")
    ps.add_argument("--base-ref", required=True, metavar="BRANCH", help="the branch the PR targets, from the event")
    ps.add_argument("--default-branch", required=True, metavar="BRANCH",
                    help="the repository's default branch, from the event; a plan PR targeting another fails")
    ps.add_argument("--repo", default=".", metavar="DIR", help="the repository (default: .)")
    ps.set_defaults(func=cmd_check_plan_scope)

    ma = sub.add_parser("check-merge-authority",
                        help="fail a PR that lacks the approval its branch and paths need: the admin's, or for a "
                             "plan run's PR the approver's with a passing plan-scope check (ADR 0043)")
    ma.add_argument("--repository", required=True, metavar="OWNER/NAME", help="the repository, from the event")
    ma.add_argument("--pr", required=True, type=int, metavar="N", help="the PR's number, from the event")
    ma.add_argument("--base", required=True, metavar="SHA", help="the PR's base commit, from the event")
    ma.add_argument("--head", required=True, metavar="SHA", help="the PR's head commit, from the event")
    ma.add_argument("--head-ref", required=True, metavar="BRANCH", help="the PR's head branch, from the event")
    ma.add_argument("--head-repo", required=True, metavar="OWNER/NAME",
                    help="the repository the head branch is in, from the event; a fork's PR needs the admin")
    ma.add_argument("--base-ref", required=True, metavar="BRANCH", help="the branch the PR targets, from the event")
    ma.add_argument("--default-branch", required=True, metavar="BRANCH",
                    help="the repository's default branch, from the event")
    ma.add_argument("--admin", required=True, metavar="LOGIN",
                    help="the admin login, from configuration the PR cannot change")
    ma.add_argument("--approver", metavar="LOGIN",
                    help="the approver login, from the same configuration; without it no approver review counts")
    ma.add_argument("--repo", default=".", metavar="DIR", help="the repository checkout (default: .)")
    ma.set_defaults(func=cmd_check_merge_authority)

    rt = sub.add_parser("routine", help="routine specs")
    rts = rt.add_subparsers(dest="routine_command", required=True)
    rr = rts.add_parser("render", parents=[common], help="print a routine spec rendered with the instance's values")
    rr.add_argument("name", help="the routine, e.g. upstream-retro")
    rr.add_argument("--set", action="append", metavar="KEY=VALUE", help="a value the instance does not hold")
    rr.set_defaults(func=cmd_routine_render)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "apply", False) and not args.confirm:
        print("--apply needs --confirm <token>; run --dry-run first to get it", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except roots.InstanceError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
