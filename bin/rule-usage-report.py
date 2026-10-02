#!/usr/bin/env python3
"""Weekly report over the rule-usage log written by hooks/rule_usage.py.

    rule-usage-report.py [--days 7] [--log PATH] [--project PATH]... [--out FILE]

Answers, for a window of days:

  * which instruction files actually loaded, in how many sessions, and at what
    cost in bytes — the number the context budget is made of;
  * which rule files in a corpus did not load at all, which is the list a
    reduction pass works from. Two corpora are reported: the **doctrine** under
    the config directory, which every session of every project pays for, and
    each project's own `CLAUDE.md` + `.claude/rules/`;
  * whether every load is accounted for — anything the sections cannot place
    is named under Context cost rather than left as a gap between the totals
    and the tables;
  * what pulled each path-scoped rule in, so an over-broad glob shows up as a
    rule loading on files it does not govern;
  * how often the planning and review subagents ran;
  * which skills and slash commands were used, and — the same question the
    rules get — which of the installed ones were never reached at all;
  * what the plan-citation check found, tallied from the project's own review
    documents.

Projects are discovered from the `cwd` of the observed records, so the report
covers exactly the repositories that were worked in. `--project` adds one
explicitly.

Exit codes are the point of the failure policy:

    0   a report, and nothing about it needs a person
    1   a report, but something in it does — lost records, a schema that
        moved, a required field missing
    2   no report could be produced, and the reason is printed

A zero is never printed without an account of itself. If the window holds no
observed session, the report cross-checks against the session transcripts
Claude Code writes independently of this hook: transcripts in the window with
no records against them means the observer is not firing, which is a failure,
not a quiet week.
"""

from __future__ import annotations

import argparse
import calendar
import glob
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
RULES_SUBDIR = os.path.join(".claude", "rules")
REVIEW_NAME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-.*\.md$")
CITATION_LINE = "PLAN-CITATIONS:"
FRONTMATTER_KEY = re.compile(r"^(paths|globs)\s*:", re.MULTILINE)

# Claude Code reads a rule's scope from a `paths:` key. A `globs:` key is the
# Cursor spelling and means nothing to it, so a file carrying only that one
# loads unconditionally — worth saying in the table rather than guessing.
SCOPE_ALWAYS = "always"
SCOPE_PATHS = "path-scoped"
SCOPE_INERT = "always (globs: is not read)"


# --------------------------------------------------------------------------
# reading


def parse_ts(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return calendar.timegm(time.strptime(value, TS_FORMAT))
    except ValueError:
        return None


def read_log(paths: list[str], since: float) -> tuple[list[dict], dict]:
    """Return the records inside the window, plus counts of what was skipped.

    A line that does not parse is counted, never dropped quietly: a log that
    is half-unreadable must not read as a light week.
    """
    records: list[dict] = []
    stats = {"lines": 0, "malformed": 0, "no_timestamp": 0, "before_window": 0}
    newest = None
    for path in paths:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                stats["lines"] += 1
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError
                except ValueError:
                    stats["malformed"] += 1
                    continue
                when = parse_ts(record.get("ts"))
                if when is None:
                    stats["no_timestamp"] += 1
                    continue
                if newest is None or when > newest:
                    newest = when
                if when < since:
                    stats["before_window"] += 1
                    continue
                record["_ts"] = when
                records.append(record)
    stats["newest"] = newest
    return records, stats


def transcripts_in_window(since: float) -> int:
    """An oracle the observer does not control.

    Claude Code writes a transcript per session whatever the hooks do, so the
    count of transcripts touched in the window is an independent answer to
    "did any session happen at all?". Only mtimes are read; no transcript is
    opened.
    """
    home = config_home()
    root = os.path.join(home, "projects")
    count = 0
    for path in glob.glob(os.path.join(root, "*", "*.jsonl")):
        try:
            if os.path.getmtime(path) >= since:
                count += 1
        except OSError:
            continue
    return count


def hook_registration() -> dict[str, bool]:
    """Which of the five events currently point at this observer."""
    home = config_home()
    events = [
        "InstructionsLoaded",
        "SessionStart",
        "SubagentStart",
        "PreToolUse",
        "UserPromptExpansion",
    ]
    try:
        with open(os.path.join(home, "settings.json"), encoding="utf-8") as handle:
            settings = json.load(handle)
    except (OSError, ValueError):
        return {event: False for event in events}
    hooks = settings.get("hooks") or {}
    found = {}
    for event in events:
        blob = json.dumps(hooks.get(event) or [])
        found[event] = "rule-usage" in blob
    return found


# --------------------------------------------------------------------------
# the static corpus


def config_home() -> str:
    """Claude Code's config directory — the one definition of it in this file."""
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude"
    )


def repo_root(start: str) -> str | None:
    """Walk up from a working directory to the checkout that holds its rules."""
    path = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(path, RULES_SUBDIR)) or os.path.isfile(
            os.path.join(path, "CLAUDE.md")
        ):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def rule_files(rules_dir: str, prefix: str) -> list[dict]:
    """Every ``.md`` under a rules directory, nested ones included.

    Recursive because the loader is: it walks the directory rather than
    listing its top level, so a rule in a subdirectory loads and a report
    that globbed one level deep would call it "never loaded".
    """
    found = []
    for path in sorted(glob.glob(os.path.join(rules_dir, "**", "*.md"), recursive=True)):
        found.append(
            {
                "path": path,
                "name": os.path.join(prefix, os.path.relpath(path, rules_dir)),
                "scope": scope_of(path),
            }
        )
    return found


def sized(files: list[dict]) -> list[dict]:
    for entry in files:
        try:
            entry["bytes"] = os.path.getsize(entry["path"])
        except OSError:
            entry["bytes"] = None
        entry["real"] = os.path.realpath(entry["path"])
    return files


def corpus(root: str) -> list[dict]:
    """Every instruction file this project can load, with size and scope."""
    files = []
    root_md = os.path.join(root, "CLAUDE.md")
    if os.path.isfile(root_md):
        files.append({"path": root_md, "name": "CLAUDE.md", "scope": SCOPE_ALWAYS})
    files += rule_files(os.path.join(root, RULES_SUBDIR), ".claude/rules")
    return sized(files)


def user_corpus() -> list[dict]:
    """The doctrine: `~/.claude/CLAUDE.md` and `~/.claude/rules/`.

    Loaded in every session of every project, so it belongs to no project and
    was invisible to a report organised by project — while its bytes still
    reached the per-session totals. That is the gap this answers: the largest
    resident block, counted but never named.
    """
    home = config_home()
    files = []
    root_md = os.path.join(home, "CLAUDE.md")
    if os.path.isfile(root_md):
        files.append({"path": root_md, "name": "CLAUDE.md", "scope": SCOPE_ALWAYS})
    files += rule_files(os.path.join(home, "rules"), "rules")
    return sized(files)


def scope_of(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            head = handle.read(4096)
    except OSError:
        return SCOPE_ALWAYS
    if not head.startswith("---"):
        return SCOPE_ALWAYS
    end = head.find("\n---", 3)
    front = head[3:end] if end != -1 else head
    keys = {match.group(1) for match in FRONTMATTER_KEY.finditer(front)}
    if "paths" in keys:
        return SCOPE_PATHS
    if "globs" in keys:
        return SCOPE_INERT
    return SCOPE_ALWAYS


# --------------------------------------------------------------------------
# aggregation


def installed_skills(projects: list[str]) -> dict[str, set[str]]:
    """Every skill and command that could have been invoked, by where it lives.

    User skills are directories holding a SKILL.md; project commands are the
    .md files under .claude/commands. Plugin skills are not enumerated here —
    they are installed per marketplace and a missing one is not actionable in
    the same way — so the report says what it counted rather than implying the
    set is complete.
    """
    home = config_home()
    found: dict[str, set[str]] = {"user skills": set(), "project commands": set()}
    for path in glob.glob(os.path.join(home, "skills", "*", "SKILL.md")):
        found["user skills"].add(os.path.basename(os.path.dirname(path)))
    for root in projects:
        for path in glob.glob(os.path.join(root, ".claude", "commands", "*.md")):
            found["project commands"].add(os.path.splitext(os.path.basename(path))[0])
        for path in glob.glob(os.path.join(root, ".claude", "skills", "*", "SKILL.md")):
            found["user skills"].add(os.path.basename(os.path.dirname(path)))
    return found


def summarise(records: list[dict]) -> dict:
    loads = [r for r in records if r.get("event") == "InstructionsLoaded"]
    starts = [r for r in records if r.get("event") == "SessionStart"]
    agents = [r for r in records if r.get("event") == "SubagentStart"]
    skills = [r for r in records if r.get("event") == "PreToolUse"]
    commands = [r for r in records if r.get("event") == "UserPromptExpansion"]
    errors = [r for r in records if r.get("event") == "observer_error"]

    by_file: dict[str, dict] = defaultdict(
        lambda: {"loads": 0, "sessions": set(), "reasons": Counter(), "triggers": Counter(), "bytes": None}
    )
    bytes_per_session: dict[str, int] = defaultdict(int)
    files_per_session: dict[str, set] = defaultdict(set)
    resident_bytes: dict[str, int] = defaultdict(int)

    for record in loads:
        path = record.get("file_path")
        if not path:
            continue
        entry = by_file[path]
        entry["loads"] += 1
        session = record.get("session_id") or "unknown"
        entry["sessions"].add(session)
        entry["reasons"][record.get("load_reason") or "unknown"] += 1
        trigger = record.get("trigger_file_path")
        if trigger:
            entry["triggers"][trigger] += 1
        size = record.get("bytes")
        if isinstance(size, int):
            entry["bytes"] = size
            # One session can load a file once; the hook fires on first read.
            if path not in files_per_session[session]:
                bytes_per_session[session] += size
                if record.get("load_reason") == "session_start":
                    resident_bytes[session] += size
        files_per_session[session].add(path)

    return {
        "loads": loads,
        "starts": starts,
        "agents": agents,
        "skills": skills,
        "commands": commands,
        "errors": errors,
        "by_file": by_file,
        "bytes_per_session": bytes_per_session,
        "files_per_session": files_per_session,
        "resident_bytes": resident_bytes,
        "sessions": {r.get("session_id") for r in records if r.get("session_id")},
        "missing_required": [r for r in loads if r.get("missing_keys")],
        "unknown_keys": Counter(
            key for r in records for key in (r.get("unknown_keys") or [])
        ),
    }


def percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def citations(root: str, since: float) -> tuple[Counter, Counter, int]:
    """Tally the PLAN-CITATIONS: lines the reviewer leaves in docs/reviews/.

    Review documents are named YYYY-MM-DD-<track>.md, so the window comes from
    the filename rather than an mtime a checkout would have rewritten.
    """
    plans: Counter = Counter()
    totals: Counter = Counter()
    scanned = 0
    for path in sorted(glob.glob(os.path.join(root, "docs", "reviews", "*.md"))):
        match = REVIEW_NAME.match(os.path.basename(path))
        if not match:
            continue
        stamp = calendar.timegm(
            (int(match.group(1)), int(match.group(2)), int(match.group(3)), 0, 0, 0, 0, 0, 0)
        )
        if stamp < since:
            continue
        scanned += 1
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        for line in text.splitlines():
            if CITATION_LINE not in line:
                continue
            body = line.split(CITATION_LINE, 1)[1].strip()
            if body.startswith("not-run"):
                plans["not-run"] += 1
            fields = dict(
                part.split("=", 1) for part in body.split() if "=" in part
            )
            if "plan" in fields:
                plans[fields["plan"]] += 1
            for key in ("uncited", "unmatched", "ambiguous", "unparsed"):
                try:
                    totals[key] += int(fields.get(key, 0))
                except ValueError:
                    continue
    return plans, totals, scanned


# --------------------------------------------------------------------------
# rendering


def kb(value: object) -> str:
    if not isinstance(value, int):
        return "?"
    return "%.1f KB" % (value / 1024.0)


def by_realpath(by_file: dict[str, dict]) -> dict[str, dict]:
    """Re-key the load records by resolved path, merging any that collide.

    This whole configuration is symlinks — `~/.claude/rules` points into the
    config repository — and the loader does not promise which spelling of a
    path it reports. Matching on the literal string would make a doctrine file
    that loaded look like one that never loaded, and its bytes unattributable.
    """
    merged: dict[str, dict] = {}
    for path, seen in by_file.items():
        key = os.path.realpath(path)
        cur = merged.get(key)
        if cur is None:
            merged[key] = {
                "loads": seen["loads"],
                "sessions": set(seen["sessions"]),
                "reasons": Counter(seen["reasons"]),
                "triggers": Counter(seen["triggers"]),
                "bytes": seen["bytes"],
            }
        else:
            cur["loads"] += seen["loads"]
            cur["sessions"] |= seen["sessions"]
            cur["reasons"] += Counter(seen["reasons"])
            cur["triggers"] += Counter(seen["triggers"])
            if cur["bytes"] is None:
                cur["bytes"] = seen["bytes"]
    return merged


def render_corpus(
    out: list[str],
    entries: list[dict],
    seen_by_real: dict[str, dict],
    claimed: set[str],
    sessions: int,
) -> None:
    """One corpus table, plus the files in it that never loaded.

    Marks every entry it accounts for in `claimed`, so anything left over at
    the end of the report is reported rather than lost.
    """
    sessions_total = max(sessions, 1)
    out.append("| instruction file | bytes | scope | sessions | loads | why it loaded |")
    out.append("|---|---:|---|---:|---:|---|")
    never = []
    for entry in sorted(entries, key=lambda e: e["name"]):
        seen = seen_by_real.get(entry["real"])
        if not seen:
            never.append(entry)
            continue
        claimed.add(entry["real"])
        reasons = ", ".join("%s %d" % item for item in sorted(seen["reasons"].items()))
        out.append(
            "| `%s` | %s | %s | %d (%d%%) | %d | %s |"
            % (
                entry["name"],
                kb(entry["bytes"]),
                entry["scope"],
                len(seen["sessions"]),
                round(100 * len(seen["sessions"]) / sessions_total),
                seen["loads"],
                reasons,
            )
        )
    out.append("")

    if never:
        out.append(
            "**Did not load at all in this window** — the list a reduction pass works from:"
        )
        out.append("")
        for entry in never:
            out.append("- `%s` (%s, %s)" % (entry["name"], kb(entry["bytes"]), entry["scope"]))
        out.append("")
    else:
        out.append("Every file in this corpus loaded at least once.")
        out.append("")


def render(args, data, stats, projects, warnings, transcripts) -> str:
    since_text = time.strftime("%Y-%m-%d", time.gmtime(args.since))
    until_text = time.strftime("%Y-%m-%d", time.gmtime(time.time()))
    out = []
    out.append("# Rule usage — %s to %s (%d days)" % (since_text, until_text, args.days))
    out.append("")
    out.append("Log: `%s`" % args.log)
    out.append(
        "Records in window: %d (of %d lines; %d malformed, %d without a timestamp)"
        % (
            len(data["loads"]) + len(data["starts"]) + len(data["agents"]) + len(data["errors"]),
            stats["lines"],
            stats["malformed"],
            stats["no_timestamp"],
        )
    )
    sources = Counter(r.get("source") or "unknown" for r in data["starts"])
    out.append(
        "Sessions observed: %d (%s)"
        % (
            len(data["starts"]),
            ", ".join("%s %d" % item for item in sorted(sources.items())) or "none",
        )
    )
    out.append("Session transcripts touched in the window: %d" % transcripts)
    out.append("")

    if warnings:
        out.append("## Needs a person")
        out.append("")
        for line in warnings:
            out.append("- %s" % line)
        out.append("")

    seen_by_real = by_realpath(data["by_file"])
    claimed: set[str] = set()

    # The doctrine first: it is what every session pays before it touches
    # anything, it belongs to no project, and a report organised by project
    # used to drop it while still counting its bytes in the totals below.
    doctrine = user_corpus()
    if doctrine:
        out.append("## Doctrine — loaded in every project")
        out.append("")
        render_corpus(out, doctrine, seen_by_real, claimed, len(data["starts"]))

    for root in projects:
        out.append("## %s" % root)
        out.append("")
        entries = corpus(root)
        known = {entry["real"] for entry in entries}
        sessions_total = max(len(data["starts"]), 1)
        render_corpus(out, entries, seen_by_real, claimed, len(data["starts"]))

        stale = [
            real
            for real in seen_by_real
            if real.startswith(root + os.sep) and real not in known
        ]
        if stale:
            out.append("Loaded but no longer in the corpus (moved or deleted since):")
            out.append("")
            for real in sorted(stale):
                claimed.add(real)
                out.append("- `%s`" % os.path.relpath(real, root))
            out.append("")

        triggers = [
            (entry["name"], seen_by_real[entry["real"]]["triggers"])
            for entry in entries
            if entry["real"] in seen_by_real and seen_by_real[entry["real"]]["triggers"]
        ]
        if triggers:
            out.append("What pulled each path-scoped rule in:")
            out.append("")
            out.append("| rule | top trigger paths |")
            out.append("|---|---|")
            for name, counter in triggers:
                top = ", ".join(
                    "`%s` ×%d" % (os.path.relpath(path, root), count)
                    for path, count in counter.most_common(3)
                )
                out.append("| `%s` | %s |" % (name, top))
            out.append("")

        plans, totals, scanned = citations(root, args.since)
        if scanned:
            out.append(
                "Plan citations, over %d review document(s) dated in the window: %s."
                % (
                    scanned,
                    ", ".join("%s %d" % item for item in sorted(plans.items()))
                    or "none of them carries a PLAN-CITATIONS line",
                )
            )
            if totals:
                out.append("")
                out.append(
                    "Across those reviews: %s."
                    % ", ".join("%s %d" % item for item in sorted(totals.items()))
                )
            out.append("")

    out.append("## Context cost")
    out.append("")
    per_session = list(data["bytes_per_session"].values())
    resident = list(data["resident_bytes"].values())
    if per_session:
        out.append(
            "Instruction bytes loaded per session: median %s, p90 %s, max %s."
            % (
                kb(percentile(per_session, 0.5)),
                kb(percentile(per_session, 0.9)),
                kb(max(per_session)),
            )
        )
        out.append("")
        out.append(
            "Of that, loaded at session start whatever the session went on to touch: median %s."
            % kb(percentile(resident, 0.5) if resident else 0)
        )
        out.append("")
        widest = max(data["files_per_session"].items(), key=lambda kv: len(kv[1]))
        out.append(
            "Widest session loaded %d instruction files." % len(widest[1])
        )

        # The figures above sum every load record whatever its path, so any
        # file the sections could not place would inflate them with nothing
        # to point at. Name it instead.
        unclaimed = sorted(set(seen_by_real) - claimed)
        out.append("")
        if unclaimed:
            total = sum(seen_by_real[real]["bytes"] or 0 for real in unclaimed)
            out.append(
                "**%d instruction file(s) loaded that no section above accounts for** "
                "(%s). They are inside the figures, so they are named here rather than "
                "left as a gap between the totals and the tables:"
                % (len(unclaimed), kb(total))
            )
            out.append("")
            for real in unclaimed:
                seen = seen_by_real[real]
                out.append(
                    "- `%s` (%s, %d load(s), %d session(s))"
                    % (real, kb(seen["bytes"]), seen["loads"], len(seen["sessions"]))
                )
        else:
            out.append(
                "Every instruction file observed loading is accounted for above."
            )
    else:
        out.append("No instruction file was observed loading in this window.")
    out.append("")

    out.append("## Subagents")
    out.append("")
    if data["agents"]:
        by_type = Counter(r.get("agent_type") or "unknown" for r in data["agents"])
        out.append("| agent | calls |")
        out.append("|---|---:|")
        for name, count in by_type.most_common():
            out.append("| `%s` | %d |" % (name, count))
    else:
        out.append("No Agent tool call was observed in this window.")
    out.append("")

    out.append("## Skills and commands")
    out.append("")
    used_skills = Counter(
        r["skill"] for r in data["skills"] if isinstance(r.get("skill"), str)
    )
    used_commands = Counter(
        r["command_name"]
        for r in data["commands"]
        if isinstance(r.get("command_name"), str)
    )
    stray = Counter(
        r.get("tool_name") or "unknown"
        for r in data["skills"]
        if r.get("tool_name") != "Skill"
    )

    if used_skills or used_commands:
        out.append("| invoked | kind | times | sessions |")
        out.append("|---|---|---:|---:|")
        for name, count in used_skills.most_common():
            sessions = {
                r.get("session_id") for r in data["skills"] if r.get("skill") == name
            }
            out.append("| `%s` | skill | %d | %d |" % (name, count, len(sessions)))
        for name, count in used_commands.most_common():
            sessions = {
                r.get("session_id")
                for r in data["commands"]
                if r.get("command_name") == name
            }
            out.append("| `%s` | command | %d | %d |" % (name, count, len(sessions)))
    else:
        out.append("No skill or slash command was observed in this window.")
    out.append("")

    installed = installed_skills(projects)
    for kind, names in sorted(installed.items()):
        if not names:
            continue
        used = used_skills if kind == "user skills" else used_commands
        # A slash command and a skill can share a name; count either as use.
        unused = sorted(
            name for name in names if name not in used_skills and name not in used_commands
        )
        out.append(
            "**%s: %d installed, %d unused in this window.**"
            % (kind, len(names), len(unused))
        )
        out.append("")
        if unused:
            out.append(", ".join("`%s`" % name for name in unused))
            out.append("")
    out.append(
        "Plugin skills are not in those counts — they are installed per "
        "marketplace and are not enumerated here, so an unused one will not "
        "appear above."
    )
    out.append("")

    if stray:
        out.append(
            "The Skill matcher also fired for: %s. It is registered for `Skill` "
            "alone, so anything else here means the matcher has drifted."
            % ", ".join("`%s` ×%d" % item for item in stray.most_common())
        )
        out.append("")

    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report on rule-corpus usage.")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--log", default=None)
    parser.add_argument("--project", action="append", default=[])
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    if args.days < 1:
        parser.error("--days must be at least 1")

    home = config_home()
    args.log = args.log or os.environ.get("CLAUDE_RULE_USAGE_LOG") or os.path.join(
        home, "logs", "rule-usage.jsonl"
    )
    args.since = time.time() - args.days * 86400

    registered = hook_registration()
    if not os.path.exists(args.log):
        sys.stderr.write(
            "rule-usage-report: no log at %s.\n"
            "The observer has never written a record. Registration now: %s.\n"
            "Install it with bootstrap.sh, then start a session.\n"
            % (args.log, registration_text(registered))
        )
        return 2

    records, stats = read_log([args.log + ".1", args.log], args.since)
    data = summarise(records)
    transcripts = transcripts_in_window(args.since)

    warnings: list[str] = []
    for event, present in sorted(registered.items()):
        if not present:
            warnings.append(
                "`%s` is not registered against this observer, so its records are "
                "missing from every number below. Re-run bootstrap.sh." % event
            )
    if stats["malformed"]:
        warnings.append(
            "%d log line(s) did not parse and were counted, not used." % stats["malformed"]
        )
    for record in data["errors"]:
        warnings.append(
            "The observer recorded its own failure: %s." % record.get("reason", "unknown")
        )
    if data["missing_required"]:
        warnings.append(
            "%d load record(s) arrived without a required field (%s). The payload "
            "schema has moved; hooks/rule_usage.py needs updating."
            % (
                len(data["missing_required"]),
                ", ".join(
                    sorted(
                        {
                            key
                            for record in data["missing_required"]
                            for key in record.get("missing_keys", [])
                        }
                    )
                ),
            )
        )
    if data["unknown_keys"]:
        warnings.append(
            "Claude Code sent field(s) the observer does not know: %s. Nothing is "
            "lost — they are recorded — but the schema in hooks/rule_usage.py is "
            "behind the CLI."
            % ", ".join(sorted(data["unknown_keys"]))
        )

    if not data["starts"] and not data["loads"]:
        if transcripts:
            sys.stderr.write(
                "rule-usage-report: %d session transcript(s) were touched in the last "
                "%d days and the observer recorded nothing against them. The hook is "
                "not firing. Registration now: %s.\n"
                % (transcripts, args.days, registration_text(registered))
            )
            return 2
        newest = stats["newest"]
        warnings.append(
            "No session ran in this window. The newest record in the log is %s."
            % (time.strftime(TS_FORMAT, time.gmtime(newest)) if newest else "absent")
        )

    projects = list(dict.fromkeys(os.path.abspath(p) for p in args.project))
    for record in records:
        cwd = record.get("cwd")
        if not isinstance(cwd, str):
            continue
        root = repo_root(cwd)
        if root and root not in projects:
            projects.append(root)

    if not projects:
        warnings.append(
            "No project could be resolved from the observed working directories, so "
            "no corpus table could be built and nothing can be called never-loaded."
        )

    report = render(args, data, stats, projects, warnings, transcripts)

    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(report)
        sys.stdout.write("wrote %s\n" % args.out)
    else:
        sys.stdout.write(report)

    return 1 if warnings else 0


def registration_text(registered: dict[str, bool]) -> str:
    return ", ".join(
        "%s %s" % (event, "yes" if present else "NO")
        for event, present in sorted(registered.items())
    )


if __name__ == "__main__":
    sys.exit(main())
