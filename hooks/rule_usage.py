#!/usr/bin/env python3
"""Rule-usage observer: records which instruction files a session loads.

Invoked by hooks/rule-usage.sh, registered in settings.base.json for three
of Claude Code's observability hooks:

    InstructionsLoaded   once per instruction file a session pulls in
    SessionStart         once per session — the denominator
    SubagentStart        once per Agent tool call, by agent type
    PreToolUse (Skill)   once per skill the model invokes
    UserPromptExpansion  once per slash command typed by the operator

It answers the one question a rule corpus cannot answer about itself: of the
rules that exist, which ones actually reach a session, how often, and at what
cost in bytes. A rule file that never loads is a candidate for retirement; one
that loads in every session is resident by accident rather than by decision.
The last two events extend the same question to skills and commands, which
have the same problem for the same reason — a large installed set, no record
of which of it is ever reached.

Payload shapes below are Claude Code's own, read from the CLI's schema
(version 2.1.280), not inferred:

    every event     session_id, transcript_path, cwd, scratchpad_dir,
                    prompt_id?, permission_mode?, agent_id?, agent_type?,
                    served_call?, caller_session_id?, effort?
                    (BASE_KEYS below is the CLI's own list of these, copied
                    rather than inferred — see the note on it)
    InstructionsLoaded
                    file_path, memory_type (User|Project|Local|Managed),
                    load_reason (session_start|nested_traversal|
                    path_glob_match|include|compact), globs?,
                    trigger_file_path?, parent_file_path?
    SessionStart    source (startup|resume|clear|compact|fork), agent_type?,
                    model?, session_title?, seconds_since_last_response?,
                    context_tokens?, prompt_cache_likely_expired?,
                    estimated_cache_write_usd?
    SubagentStart   agent_id, agent_type
    PreToolUse      tool_name, tool_input, tool_use_id, mcp_server?
    UserPromptExpansion
                    expansion_type (slash_command|mcp_prompt), command_name,
                    command_args, command_source?, prompt

What is deliberately not recorded: the prompt text — UserPromptSubmit is not
registered at all, for this reason — `session_title`, which is derived from a
prompt, `transcript_path`, which points at the whole conversation, and, from
the two events above, `command_args`, `prompt` and `tool_input` as a whole.
A slash command's arguments and a skill's arguments are both things a person
typed; only the NAME of the skill or command is recorded. This log holds
paths, names, byte counts and timestamps, so it needs no redaction pass and
can be read by anyone who can read the repository it describes.

⚠️ Two of the five events can block. `PreToolUse` treats exit 2 as "refuse
this tool call" and `UserPromptExpansion` as "refuse this expansion", so an
observer that ever returned 2 would start silently eating the operator's
skills and slash commands. This one returns only 0 or 1, and a test pins that
for every payload shape including garbage.

Failure policy. Beyond never blocking, the failure that matters is the silent
one — a hook that runs, records nothing, and leaves a weekly report full of
confident zeros. Every branch here therefore writes a record, including the
branches that failed:

    unparseable payload   -> an observer_error record, exit 0
    event not registered  -> an observer_error record, exit 0
    a key the schema does not name -> recorded in unknown_keys
    a required key absent -> recorded in missing_keys
    the log cannot be written -> one line on stderr, exit 1

The report refuses to print a zero it cannot account for; see
bin/rule-usage-report.py.
"""

from __future__ import annotations

import json
import os
import sys
import time

SCHEMA = 1

# Rotate rather than grow without bound. One session writes on the order of a
# dozen records, so this holds years of history; the report only ever reads a
# window of it, and keeping one generation back means a rotation never loses a
# week that has not been reported yet.
MAX_LOG_BYTES = 8 * 1024 * 1024

# A record is one write() syscall, which is atomic against other appenders
# only while it stays small. Long glob lists are the one field that can grow,
# so they are capped rather than trusted.
MAX_GLOBS = 20

# Every key Claude Code puts on a hook payload, whatever the event. This is
# the CLI's own list, copied — it carries the same array internally — rather
# than inferred field by field. After a CLI upgrade, re-derive the whole thing
# instead of adding keys one at a time as the weekly report flags them; the
# report names them precisely so this can be kept current:
#
#   python3 -c "d=open('<home>/.local/share/claude/versions/<v>','rb') \
#     .read().decode('utf-8','replace'); \
#     i=d.find('\"hook_event_name\",\"session_id\"'); print(d[i:i+320])"
#
# Everything here is *known*. What is *recorded* is BASE_RECORDED, which is
# deliberately much shorter — being known is only what stops a field reading
# as drift.
BASE_KEYS = frozenset(
    {
        "hook_event_name",
        "session_id",
        "transcript_path",
        "cwd",
        "scratchpad_dir",
        "prompt_id",
        "permission_mode",
        "agent_id",
        "agent_type",
        "served_call",
        "caller_session_id",
        "effort",
    }
)

# Base fields worth keeping. `transcript_path` is knowingly dropped, and so are
# `scratchpad_dir` (a per-session temp path), `effort` and the two call-routing
# keys: being *known* is what stops them reading as drift, and nothing in the
# report asks about them. `agent_type` is recorded because it is what makes a
# load attributable to the subagent that caused it — the question `/ct-plan`
# exists to answer.
BASE_RECORDED = (
    "session_id",
    "cwd",
    "prompt_id",
    "agent_id",
    "agent_type",
    "permission_mode",
)

# event -> (required keys, other keys the schema names, keys to record)
EVENTS = {
    "InstructionsLoaded": (
        ("file_path", "memory_type", "load_reason"),
        ("globs", "trigger_file_path", "parent_file_path"),
        (
            "file_path",
            "memory_type",
            "load_reason",
            "globs",
            "trigger_file_path",
            "parent_file_path",
        ),
    ),
    "SessionStart": (
        ("source",),
        (
            "agent_type",
            "model",
            "session_title",
            "seconds_since_last_response",
            "context_tokens",
            "prompt_cache_likely_expired",
            "estimated_cache_write_usd",
        ),
        ("source", "agent_type", "model", "context_tokens"),
    ),
    "SubagentStart": (("agent_id", "agent_type"), (), ("agent_type",)),
    # tool_input is named but never recorded: for the Skill tool it holds the
    # arguments, which are the operator's words. `skill` is lifted out of it
    # below, by name, so nothing else can ride along.
    "PreToolUse": (
        ("tool_name",),
        ("tool_input", "tool_use_id", "mcp_server"),
        ("tool_name",),
    ),
    # Same shape of care: command_args and prompt are named so they do not
    # register as schema drift, and recorded nowhere.
    "UserPromptExpansion": (
        ("expansion_type", "command_name"),
        ("command_args", "command_source", "prompt"),
        ("expansion_type", "command_name", "command_source"),
    ),
}


def log_path() -> str:
    """Where records go. The env var exists so the tests can point elsewhere."""
    override = os.environ.get("CLAUDE_RULE_USAGE_LOG")
    if override:
        return override
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude"
    )
    return os.path.join(home, "logs", "rule-usage.jsonl")


def file_size(path: object) -> int | None:
    """Size of a loaded instruction file, or None when it cannot be measured.

    This is the only derived field in a record, and it is the one the context
    budget is made of. Measured at load time because a file read later may not
    be the file that was loaded.
    """
    if not isinstance(path, str) or not path:
        return None
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def build_record(payload: dict) -> dict:
    event = payload.get("hook_event_name")
    if event not in EVENTS:
        return {
            "v": SCHEMA,
            "ts": timestamp(),
            "event": "observer_error",
            "reason": "unregistered-event",
            "hook_event_name": str(event)[:120],
        }

    required, optional, recorded = EVENTS[event]
    record: dict[str, object] = {"v": SCHEMA, "ts": timestamp(), "event": event}

    for key in BASE_RECORDED:
        value = payload.get(key)
        if value is not None:
            record[key] = value

    for key in recorded:
        value = payload.get(key)
        if value is None:
            continue
        if key == "globs" and isinstance(value, list):
            value = value[:MAX_GLOBS]
        record[key] = value

    if event == "InstructionsLoaded":
        # Recorded even when None: a rule file that loaded but could not be
        # measured is a different fact from one that loaded at zero bytes, and
        # the report has to be able to tell them apart.
        record["bytes"] = file_size(payload.get("file_path"))

    if event == "PreToolUse":
        # Registered with a matcher of `Skill`, so this should only ever fire
        # for that tool. Recording the name it did fire for means a matcher
        # that drifts shows up in the report as a tool nobody asked about,
        # rather than as a pile of records the report silently discards.
        skill = payload.get("tool_input")
        if isinstance(skill, dict) and isinstance(skill.get("skill"), str):
            record["skill"] = skill["skill"]

    missing = [k for k in required if payload.get(k) is None]
    if missing:
        record["missing_keys"] = missing

    known = BASE_KEYS | set(required) | set(optional)
    unknown = sorted(set(payload) - known)
    if unknown:
        # Claude Code gained a field. Recorded rather than dropped, so the
        # report can say the schema moved instead of quietly under-counting.
        record["unknown_keys"] = unknown[:MAX_GLOBS]

    return record


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def rotate(path: str) -> None:
    try:
        if os.path.getsize(path) < MAX_LOG_BYTES:
            return
    except OSError:
        return
    try:
        os.replace(path, path + ".1")
    except OSError:
        # Not fatal: appending to an oversized log beats losing the record.
        pass


def append(path: str, record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    rotate(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
    except (ValueError, TypeError):
        payload = None

    if payload is None:
        record = {
            "v": SCHEMA,
            "ts": timestamp(),
            "event": "observer_error",
            "reason": "unparseable-payload",
            "input_bytes": len(raw),
        }
    else:
        record = build_record(payload)

    path = log_path()
    try:
        append(path, record)
    except OSError as err:
        # The only branch that is allowed to be noisy. An observer that cannot
        # write is indistinguishable from one that has nothing to say, and the
        # whole point of this log is that the difference is visible.
        sys.stderr.write("rule-usage: cannot write %s: %s\n" % (path, err))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
