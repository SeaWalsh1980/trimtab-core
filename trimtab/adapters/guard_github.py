"""PreToolUse adapter for hooks/guard-github.sh: refuse the GitHub writes that only the operator makes.

Run by the hook through hooks/run_module.py (ADR 0035), with a Bash payload on stdin. It exits 0
to allow and 2 to block, with one line on stderr naming the rule and never quoting the command.
The decisions and the residuals are in docs/adr/0037-guard-github-refuses-the-github-writes-the-operator-owns.md.

Refused, in every spelling read below:

- a pull request opened as anything but a draft: `gh pr create` without `--draft`, or with
  `--web`; a REST create without a typed `draft` of true; a GraphQL `createPullRequest` without
  `draft: true`;
- an approving review: `gh pr review --approve`; a REST review or review event whose `event` is
  APPROVE; a GraphQL `addPullRequestReview` / `submitPullRequestReview` that names APPROVE;
- resolving a review thread: GraphQL `resolveReviewThread`;
- a write to the default branch (named `main` or `master`) through the API: a ref update or
  delete, a merge, a contents write, a fork sync, and the GraphQL ref and commit mutations;
  and `gh repo sync --force`;
- deleting a workflow run or its logs, and dispatching a workflow, by `gh run delete`, `gh workflow run`
  or the API (ADR 0055);
- a write to a github.com host through `curl` or `wget`, which this guard cannot read;
- for plan runs (docs/adr/0063-the-plan-run-enforcement-gate.md §5): a write to a `plan/*` ref
  outside a plan run, a forced one, a delete, a push to a ready PR, a push while the secret scan
  fails, and a fourth CI-fix round, in the spellings of `git push`, the REST ref, contents, merge
  and rename routes, `gh pr close|merge|update-branch` and GraphQL. The rows are in
  trimtab/planrows.py, the facts in trimtab/planfacts.py, the wiring in trimtab/planguard.py.

How it reads the command. Two passes, and either may refuse:

1. quote-aware: one regex tokeniser (the pattern guard-bash uses, ADR 0036) splits the text
   into commands at `; & | ( )`, backquotes and newlines, joins quoted pieces into words, and
   marks a word *dynamic* when the shell would expand it (`$`, a backquote, a glob or a brace
   outside single quotes). A dynamic word where the decision depends on it is refused. The
   commands a double-quoted `$(…)` or backquote substitution runs are read the same way.
2. naive: the same text with every quote and backslash removed, split the same way, every word
   taken literally. It sees a command hidden inside a quoted string that something then runs
   (`echo '…' | bash`), and runs only when the command holds such a runner (a shell, `eval`,
   `xargs`, `ssh`, …). It refuses only on evidence; what it cannot read, it leaves to pass 1.

Fails closed: any error, or a value the decision needs and cannot read (stdin, an unreadable or
non-JSON file, a shell expansion, an option it does not know), refuses. Standard library only.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import signal
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from trimtab import planacts, planguard, planrows
from trimtab.enforcement import guardgate

ALLOW, BLOCK = 0, 2
DEFAULT_BRANCHES = {"main", "master"}
TRUE = {"1", "t", "T", "true", "TRUE", "True"}  # pflag's ParseBool
FALSE = {"0", "f", "F", "false", "FALSE", "False"}

# The tokeniser guard-bash uses (ADR 0036): W whitespace, C comment, R redirect, O operator,
# S single-quoted, D double-quoted, B backslash escape, L literal, A anything else.
_TOK = re.compile(
    r"(?P<W>[ \t]+)|(?P<C>(?<![^ \t\n;&|()`])#[^\n]*)|(?P<R>&>>?|[0-9]*[<>]+[&|]?)"
    r"|(?P<O>&&|\|\||\|&|;;|[;&|()`\n])|(?P<S>'[^']*'?)|(?P<D>\"(?:[^\"\\]|\\[\s\S])*\"?)"
    r"|(?P<B>\\[\s\S]?)|(?P<L>[^ \t\n;&|()`<>'\"\\]+)|(?P<A>[\s\S])")
# A `$` the shell expands: a name, a special parameter, `${`, `$(`, `$'`, `$"`, or `$` before
# a quote or the end of an unquoted piece (it joins whatever follows).
_EXPANSION = re.compile(r"\$(?:[A-Za-z_0-9{(@*#?$!'\"-]|$)")
_GLOB = re.compile(r"[*?\[]|\{[^}]*,[^}]*\}|\{[^}]*\.\.[^}]*\}")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
_NAIVE_SPLIT = re.compile(r"\$\(|[;&|()`\n]")
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "mksh", "busybox"}
# Commands that run text, from an argument or their input, as a command.
RUNS_TEXT = SHELLS | {"eval", "source", ".", "xargs", "ssh", "su", "parallel", "watch", "script", "flock"}
# Commands that append arguments read from stdin to the command they run.
APPENDERS = {"xargs", "parallel"}
DIR_CHANGERS = {"cd", "pushd", "popd"}
# Stands for an expansion inside a word, so a path keeps its literal structure.
HOLE = "\x00"
# The most a request file may hold; a larger one is refused, not read.
MAX_FILE = 1 << 20
# Seconds before the adapter gives up and blocks, inside the hook's 10 s timeout (a timeout
# fails open). The hook also runs it under `timeout`, which kills it shortly after.
DEADLINE = 6


class Refused(Exception):
    """The command does one of the refused things, or cannot be read where the decision needs it."""


@dataclass(frozen=True)
class Word:
    text: str          # quotes removed; HOLE where the shell would substitute
    dynamic: bool      # the shell expands something in it


# ---- reading the command -----------------------------------------------------------------

def _dq_parts(body: str) -> tuple[str, bool, list[str]]:
    """(value, dynamic, the commands its `$(…)` and backquote substitutions run).

    A substitution's end is found by counting parentheses (or the next backquote), which is
    what the tokeniser can know without running a parser; a quote inside one ends the
    double-quoted piece early, as it does for guard-bash's tokeniser.
    """
    out, dynamic, subs, i = [], False, [], 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            out.append(nxt if nxt in '$`"\\' else c + nxt)
            i += 2
            continue
        if body.startswith("$(", i):
            depth, j = 1, i + 2
            while j < len(body) and depth:
                depth += {"(": 1, ")": -1}.get(body[j], 0)
                j += 1
            subs.append(body[i + 2:j - 1 if depth == 0 else j])
            dynamic = True
            out.append(HOLE)
            i = j
            continue
        if c == "`":
            end = body.find("`", i + 1)
            end = len(body) if end < 0 else end
            subs.append(body[i + 1:end])
            dynamic = True
            out.append(HOLE)
            i = end + 1
            continue
        if c == "$" and _EXPANSION.match(body, i):
            dynamic = True
            out.append(HOLE)
        else:
            out.append(c)
        i += 1
    return "".join(out), dynamic, subs


def segments(command: str) -> list[list[Word]]:
    """The command's simple commands as words, by the quote-aware pass."""
    segs: list[list[Word]] = []
    nested: list[list[Word]] = []
    words: list[Word] = []
    text: list[str] = []
    dynamic = in_word = skip_target = False

    def flush():
        nonlocal text, dynamic, in_word, skip_target
        if in_word:
            if skip_target:
                skip_target = False
            else:
                words.append(Word("".join(text), dynamic))
        text, dynamic, in_word = [], False, False

    for m in _TOK.finditer(command.replace("\\\n", "")):
        kind, value = m.lastgroup, m.group()
        if kind in ("W", "C"):
            flush()
        elif kind == "R":
            flush()
            skip_target = True  # the redirect's target is not a word of the command
        elif kind == "O":
            flush()
            skip_target = False
            segs.append(words)
            words = []
        else:
            in_word = True
            if kind == "S":
                text.append(value[1:-1] if len(value) > 1 and value.endswith("'") else value[1:])
            elif kind == "D":
                inner = value[1:-1] if len(value) > 1 and value.endswith('"') else value[1:]
                val, dyn, subs = _dq_parts(inner)
                text.append(val)
                dynamic |= dyn
                for sub in subs:  # a quoted substitution runs its own commands
                    nested.extend(segments(sub))
            elif kind == "B":
                text.append(value[1:])
            else:  # L, A
                if HOLE in value:
                    dynamic = True  # re-read text (eval, an alias) that held an expansion
                if _EXPANSION.search(value) or (value.endswith("$") and m.end() < len(command)):
                    dynamic = True
                    text.append(_EXPANSION.sub(HOLE, value))
                else:
                    text.append(value)
                if _GLOB.search(value):
                    dynamic = True
    flush()
    segs.append(words)
    return [s for s in segs + nested if s]


def naive_segments(command: str) -> list[list[Word]]:
    """The naive pass: quotes and backslashes dropped, every word literal."""
    flat = re.sub(r"[\"'\\]", "", command)
    return [[Word(w, False) for w in part.split()] for part in _NAIVE_SPLIT.split(flat) if part.split()]


# ---- options -----------------------------------------------------------------------------

@dataclass
class Opt:
    name: str                   # "--draft", "-d"
    value: Word | None          # None for a boolean given without a value


def parse_options(args: list[Word], valued: set[str], boolean: set[str], *, strict: bool,
                  holes_positional: bool = False) -> tuple[list[Opt], list[Word]]:
    """(options, positionals), the way pflag reads them; cobra takes no abbreviations.

    `strict` (the quote-aware pass) refuses an unknown option, since whether it takes a value
    decides what the next word is, and a dynamic word where an option may stand. The naive pass
    skips both. With `holes_positional`, a word that starts with an expansion is kept as a
    positional, for the caller to judge.
    """
    opts: list[Opt] = []
    positional: list[Word] = []
    i = 0
    while i < len(args):
        w = args[i]
        t = w.text
        if t == "--":
            positional.extend(args[i + 1:])
            break
        if w.dynamic and t.startswith(HOLE) and holes_positional:
            positional.append(w)
            i += 1
            continue
        if w.dynamic and (t.startswith("-") or t.startswith(HOLE)):
            if strict:
                raise Refused("a shell expansion stands where an option may be")
            i += 1
            continue
        if t.startswith("--") and len(t) > 2:
            name, eq, val = t.partition("=")
            if name in valued:
                if eq:
                    opts.append(Opt(name, Word(val, w.dynamic)))
                elif i + 1 < len(args):
                    i += 1
                    opts.append(Opt(name, args[i]))
                else:
                    opts.append(Opt(name, None))
            elif name in boolean:
                opts.append(Opt(name, Word(val, w.dynamic) if eq else None))
            elif strict:
                raise Refused(f"an option this guard does not know ({_safe(name)})")
        elif t.startswith("-") and len(t) > 1:
            j = 1
            while j < len(t):
                flag = "-" + t[j]
                rest = t[j + 1:]
                if flag in valued:
                    if rest:
                        opts.append(Opt(flag, Word(rest.removeprefix("="), w.dynamic)))
                    elif i + 1 < len(args):
                        i += 1
                        opts.append(Opt(flag, args[i]))
                    else:
                        opts.append(Opt(flag, None))
                    break
                if flag in boolean:
                    if rest.startswith("="):
                        opts.append(Opt(flag, Word(rest[1:], w.dynamic)))
                        break
                    opts.append(Opt(flag, None))
                    j += 1
                    continue
                if strict:
                    raise Refused(f"an option this guard does not know ({_safe(flag)})")
                break
        else:
            positional.append(w)
        i += 1
    return opts, positional


def _safe(name: str) -> str:
    """An option name fit for the message: letters, digits and dashes only, else a placeholder."""
    return name if re.fullmatch(r"-{1,2}[A-Za-z0-9][A-Za-z0-9-]{0,30}", name) else "an unreadable option"


def _bool(opt: Opt) -> bool:
    """A boolean option's value; pflag rejects anything else, so that reads as not set."""
    if opt.value is None:
        return True
    if opt.value.dynamic:
        raise Refused("a shell expansion sets an option's value")
    return opt.value.text in TRUE


# ---- the checks --------------------------------------------------------------------------

@dataclass
class Ctx:
    cwd: Path
    cwd_known: bool       # no cd / pushd / popd earlier in the command
    strict: bool          # quote-aware pass
    command: str = ""     # the whole command the hook was given
    plan: planguard.PlanSupport | None = None  # the plan-run rows' view (ADR 0063 §5)
    stability: planguard.Stability = planguard.STABLE  # what git commands earlier in the command change

    def unknown(self, what: str) -> None:
        """Something the decision needs cannot be read: refused by the quote-aware pass only."""
        if self.strict:
            raise Refused(what)

    def read_file(self, word: Word) -> str | None:
        t = word.text
        if word.dynamic or t in ("", "-"):
            self.unknown("a value comes from stdin or a shell expansion")
            return None
        # The file is read now, but gh reads it after the rest of the command has run, which
        # may have rewritten it. So a file whose name appears anywhere else in the command
        # (a redirect, cp, tee, sed -i, ln) is unreadable.
        name = re.escape(posixpath.basename(t.rstrip("/")))
        if len(re.findall(rf"(?<![A-Za-z0-9_.-]){name}(?![A-Za-z0-9_.-])", self.command)) > 1:
            self.unknown("a file the request reads is also named elsewhere in the command")
            return None
        path = Path(t).expanduser()
        if not path.is_absolute():
            if not self.cwd_known:
                self.unknown("a relative file is read after the directory changed")
                return None
            path = self.cwd / path
        try:
            return _read_regular(path)
        except (OSError, UnicodeDecodeError, ValueError):
            self.unknown("a file the request reads cannot be read")
            return None


def _read_regular(path: Path) -> str:
    """A regular file's text, at most MAX_FILE bytes.

    Opened non-blocking and checked with fstat, so a FIFO, a device or an oversized file is
    refused at once rather than read: a read that outlasts the hook's timeout fails open.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
            raise ValueError("not a regular file within the size limit")
        data = os.read(fd, MAX_FILE + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_FILE:
        raise ValueError("over the size limit")
    return data.decode("utf-8")


PR_CREATE_VALUED = {"-a", "--assignee", "-B", "--base", "-b", "--body", "-F", "--body-file", "-H", "--head",
                    "-l", "--label", "-m", "--milestone", "-p", "--project", "-r", "--reviewer", "-t", "--title",
                    "-T", "--template", "-R", "--repo", "--recover"}
PR_CREATE_BOOL = {"-d", "--draft", "-f", "--fill", "--fill-first", "--fill-verbose", "-w", "--web", "-e",
                  "--editor", "--dry-run", "--no-maintainer-edit"}
PR_REVIEW_VALUED = {"-b", "--body", "-F", "--body-file", "-R", "--repo"}
PR_REVIEW_BOOL = {"-a", "--approve", "-c", "--comment", "-r", "--request-changes"}
REPO_SYNC_VALUED = {"-b", "--branch", "-s", "--source", "-R", "--repo"}
REPO_SYNC_BOOL = {"--force"}
API_VALUED = {"-X", "--method", "-H", "--header", "-f", "--raw-field", "-F", "--field", "--input", "-q", "--jq",
              "-t", "--template", "--hostname", "-p", "--preview", "--cache"}
API_BOOL = {"-i", "--include", "--paginate", "--silent", "--verbose", "--slurp"}
GH_GLOBAL_VALUED = {"-R", "--repo"}
RUN_DELETE = "deleting a workflow run or its logs is the owner's: it erases what a merge check reads"
DISPATCH = "dispatching a workflow is the owner's: it can start a production deploy or rollback"
# The group, the subcommand this guard refuses in it, and why.
GROUP_REFUSALS = {"run": ("delete", RUN_DELETE), "workflow": ("run", DISPATCH)}


def _after_options(words: list[Word], i: int) -> int:
    """The index of the first word at or after `i` that isn't a literal option (and its value, for a repository)."""
    while i < len(words) and words[i].text.startswith("-") and not words[i].dynamic:
        i += 2 if words[i].text in GH_GLOBAL_VALUED else 1
    return i


def check_pr_create(args: list[Word], ctx: Ctx) -> None:
    opts, _ = parse_options(args, PR_CREATE_VALUED, PR_CREATE_BOOL, strict=ctx.strict)
    draft = web = False
    for o in opts:
        if o.name in ("-d", "--draft"):
            draft = _bool(o)
        elif o.name in ("-w", "--web"):
            web = _bool(o)
    if web:
        raise Refused("`gh pr create --web` opens a form that can create a non-draft PR")
    if not draft:
        raise Refused("a PR must be opened as a draft (`gh pr create --draft`)")
    _plan_draft(opts, ctx)


def _plan_draft(opts: list[Opt], ctx: Ctx) -> None:
    """E23: a PR opened from `plan/*` needs a complete in-session round on the exact head (ADR 0063 §5)."""
    plan = ctx.plan
    if plan is None:
        return
    named = [o for o in opts if o.name in ("-H", "--head")]
    if any(o.value is None or o.value.dynamic for o in named):
        ctx.unknown("the branch a PR is opened from is a shell expansion")
        return
    if named:
        branch = named[-1].value.text.rpartition(":")[2]
    elif ctx.cwd_known:
        branch = _plan_ref(plan.pr_branch, ctx.cwd)  # as gh resolves it: the branch's merge ref, else it
    else:
        branch = None
    if branch is None:
        if plan.in_run(ctx.cwd):
            raise planrows.Refusal("the branch a PR is opened from cannot be read here, so E23 cannot be "
                                   "judged; refusing in a plan run (ADR 0063 §5)")
        return  # outside a run, E23 governs nothing
    if not planrows.plan_branch(branch):
        return
    _plan_draft_branch(plan, branch, ctx)


def _plan_draft_branch(plan: planguard.PlanSupport, branch: str, ctx: Ctx) -> None:
    """E23 for a PR opened from `branch`, however the command spells the opening."""
    head = _act_head(plan, lambda: _plan_ref(plan.facts.branch_head, branch))
    if head is None and not plan.in_run(ctx.cwd):
        return
    if head is None:
        raise planrows.Refusal("a PR from plan/* is opened on the branch's remote head, which is not there "
                               "yet; refusing (ADR 0063 §5)")
    planacts.draft_opened(plan, branch, head)


def check_pr_review(args: list[Word], ctx: Ctx) -> None:
    opts, _ = parse_options(args, PR_REVIEW_VALUED, PR_REVIEW_BOOL, strict=ctx.strict)
    if any(o.name in ("-a", "--approve") and _bool(o) for o in opts):
        raise Refused("an approving review is the code owner's, never the session's")


def check_repo_sync(args: list[Word], ctx: Ctx) -> None:
    opts, _ = parse_options(args, REPO_SYNC_VALUED, REPO_SYNC_BOOL, strict=ctx.strict)
    if any(o.name == "--force" and _bool(o) for o in opts):
        raise Refused("`gh repo sync --force` can discard commits on the default branch")


def check_gh(args: list[Word], ctx: Ctx) -> None:
    """Judge the arguments after a `gh` command word."""
    i = _after_options(args, 0)
    if i >= len(args):
        return
    group = args[i]
    if group.dynamic:
        ctx.unknown("the gh command is a shell expansion")
        return
    rest = args[i + 1:]
    # Persistent options may sit between the group and its command (`gh pr -R o/r create`).
    j = _after_options(rest, 0)
    sub = rest[j] if j < len(rest) else None
    tail = rest[j + 1:]
    if group.text == "api":
        check_api(rest, ctx)
        return
    if sub is None:
        return
    if sub.dynamic:
        if group.text in ("pr", "issue", "repo", "alias", "run", "workflow"):
            ctx.unknown("the gh command is a shell expansion")
        return
    if group.text in ("pr", "issue"):
        check_plan_gh(group.text, sub.text, args[:i] + rest[:j], tail, ctx)
    if group.text == "pr" and sub.text in ("create", "new"):  # cobra registers `new` as create's alias
        check_pr_create(tail, ctx)
    elif group.text == "pr" and sub.text == "review":
        check_pr_review(tail, ctx)
    elif group.text == "repo" and sub.text == "sync":
        check_repo_sync(tail, ctx)
    elif group.text in GROUP_REFUSALS and sub.text == GROUP_REFUSALS[group.text][0]:
        raise Refused(GROUP_REFUSALS[group.text][1])
    elif group.text == "alias" and sub.text == "set":
        check_alias_set(tail, ctx)
    elif group.text == "alias" and sub.text == "import":
        ctx.unknown("`gh alias import` defines commands this guard cannot read")


def check_alias_set(args: list[Word], ctx: Ctx) -> None:
    """An alias runs later under its own name, so its expansion is judged when it is defined."""
    # A literal word with a space is an expansion that starts with an option, not an option.
    if any(not w.dynamic and w.text.startswith("-") and " " in w.text for w in args):
        ctx.unknown("an alias's expansion starts with an option")
    _, positional = parse_options(args, set(), {"--clobber", "-s", "--shell"}, strict=False)
    if len(positional) < 2:
        return
    expansion = positional[1]
    if expansion.dynamic or expansion.text == "-":
        ctx.unknown("an alias's expansion is a shell expansion or comes from stdin")
        return
    text = expansion.text
    shell = text.startswith("!") or any(w.text in ("-s", "--shell") for w in args)
    if not shell and _alias_stops_short(text.split()):
        ctx.unknown("an alias takes more words when it runs, and this one leaves what it does to them")
    judge(text.removeprefix("!") if shell else "gh " + text, ctx)


def _alias_stops_short(words: list[str]) -> bool:
    """What the words a later call appends would decide: the expansion names a group or `api` and not what it
    runs, or a command this guard judges by its options. `pr create --draft` stays open (ADR 0055)."""
    named = []
    i = 0
    while i < len(words):
        if words[i] in GH_GLOBAL_VALUED or words[i] in API_VALUED:
            i += 2
            continue
        if not words[i].startswith("-"):
            named.append(words[i])
        i += 1
    # A plan act is decided by the PR the later call selects, or by the label it names, so an alias that
    # stops at the subcommand hides it (ADR 0063 §5, §1a).
    plan_acts = (["pr", "ready"], ["pr", "edit"], ["pr", "close"], ["pr", "merge"],
                 ["pr", "update-branch"], ["pr", "comment"], ["issue", "edit"], ["issue", "close"],
                 ["issue", "comment"])
    if named[:2] in (["pr", "review"], ["repo", "sync"], *plan_acts):
        return True
    return len(named) == 1 and named[0] in ("api", "pr", "issue", "repo", "run", "workflow")


# ---- gh api ------------------------------------------------------------------------------

@dataclass
class Request:
    method: str | None             # None: a shell expansion
    path: str                      # normalised, lower case; HOLE where the shell substitutes
    fields: list[tuple[str, Word]]  # (option, "key=value" word), in order
    input: Word | None
    orig: str = ""                 # the same path with its case kept: branch names are case-sensitive
    query: bool = False            # the path carried a query string, which a write may take its fields from


_ID_PARENTS = {"repos", "pulls", "issues", "comments", "reviews", "replies", "check-runs", "check-suites",
               "releases", "labels", "milestones", "runs", "jobs", "assets", "commits", "statuses",
               "deployments", "events"}


def _norm_path(raw: str, lower: bool = True) -> str:
    path = re.sub(r"^[a-z][a-z0-9+.-]*://[^/]*", "", raw, flags=re.I)
    path = path.split("?", 1)[0].split("#", 1)[0]
    path = unquote(path).lstrip("/")
    path = re.sub(r"^api/v3/", "", path, flags=re.I)
    path = posixpath.normpath("/" + path).lstrip("/") if path else ""
    # GitHub also serves a repository by its id (`repositories/<id>/…`); the id fills both slots.
    return re.sub(r"^repositories/([^/]+)", r"repos/\1/\1", path.lower() if lower else path, flags=re.I)


def _readable_path(path: str) -> bool:
    """Every expansion in the path stands for an owner/repository or an ID, not a route word."""
    parts = path.split("/")
    for k, part in enumerate(parts):
        if HOLE not in part:
            continue
        if "contents" in parts[:k]:
            continue
        if k == 2 and parts[0] == "repos":
            continue  # the repository, after an owner that is itself an expansion
        if k == 0 or parts[k - 1] not in _ID_PARENTS:
            return False
    return True


def _may_hide_route(path: str, method: str | None) -> bool:
    """An expansion can hold slashes, so `repos/$P/123` can be a run's route.

    A named route after the repository (`repos/$REPO/issues/1/comments`) shows what it is. Only nothing,
    an id, or `logs` after the slots leaves the route to the expansion. GitHub also resolves `..`, so one
    at the end of a delete or a create (`labels/$X`, `labels/$X/logs`) can hold a route too; PATCH and PUT
    stay open there, since a run's and a dispatch's routes take neither (ADR 0055).
    """
    parts = path.split("/")
    ending = parts[:-1] if parts[-1] == "logs" else parts
    if ending and HOLE in ending[-1] and method not in ("PATCH", "PUT"):
        return True
    held = [k for k in (1, 2) if k < len(parts) and HOLE in parts[k]]
    if parts[0] != "repos" or not held:
        return False
    tail = parts[held[-1] + 1:]
    if tail and tail[-1] == "logs":
        tail.pop()
    return not tail or (len(tail) == 1 and (tail[0].isdigit() or HOLE in tail[0]))


def parse_api(args: list[Word], ctx: Ctx) -> Request | None:
    opts, positional = parse_options(args, API_VALUED, API_BOOL, strict=ctx.strict, holes_positional=True)
    method: str | None = ""
    fields: list[tuple[str, Word]] = []
    source = None
    for o in opts:
        if o.value is None:
            continue
        if o.name in ("-X", "--method"):
            method = None if o.value.dynamic else o.value.text.upper()
        elif o.name in ("-f", "--raw-field", "-F", "--field"):
            fields.append((o.name, o.value))
        elif o.name == "--input":
            source = o.value
    if not positional:
        return None
    # A word that starts with an expansion may be an option once expanded. With a single
    # positional that does no harm: an option there leaves gh no path, and it refuses to run.
    if len(positional) > 1 and any(w.dynamic and w.text.startswith(HOLE) for w in positional):
        ctx.unknown("a shell expansion stands where an option may be")
        return None
    if method == "":
        method = "POST" if fields or source is not None else "GET"
    raw = positional[0]
    return Request(method, _norm_path(raw.text), fields, source, _norm_path(raw.text, lower=False), "?" in raw.text)


def _typed(value: str):
    """A `-F` value as gh types it: true, false, null and integers become JSON values."""
    if value in ("true", "false"):
        return value == "true"
    if value == "null":
        return None
    if re.fullmatch(r"-?[0-9]+", value):
        return int(value)
    return value


class Unknown:
    """A value the request carries that this guard cannot read."""


UNKNOWN = Unknown()


def body_of(req: Request, ctx: Ctx) -> dict | Unknown:
    """The JSON body gh sends: the --input file, or else the fields."""
    if req.input is not None:
        text = ctx.read_file(req.input)
        if text is None:
            return UNKNOWN
        try:
            body = json.loads(text)
        except ValueError:
            ctx.unknown("the --input file is not JSON")
            return UNKNOWN
        if not isinstance(body, dict):
            ctx.unknown("the --input file is not a JSON object")
            return UNKNOWN
        return body
    body: dict = {}
    for name, word in req.fields:
        key, eq, val = word.text.partition("=")
        if word.dynamic and (HOLE in key or not eq):
            ctx.unknown("a field's name is a shell expansion")
            return UNKNOWN
        if not eq:
            continue
        if HOLE in val:
            value = UNKNOWN
        elif name in ("-F", "--field") and val.startswith("@"):
            text = ctx.read_file(Word(val[1:], False))
            value = UNKNOWN if text is None else text
        elif name in ("-F", "--field"):
            value = _typed(val)
        else:
            value = val
        _set_field(body, key, value)
    return body


def _set_field(body: dict, key: str, value) -> None:
    """Place a field as gh nests it: `a[b]=v` is {"a": {"b": v}}, `a[]=v` appends to a list.

    A key given twice with different values: which one gh sends is gh's merge rule, not this
    guard's to assume, so the value counts as unreadable. A key whose shape this does not read
    makes its top-level name unreadable.
    """
    m = re.fullmatch(r"([^\[\]]+)((?:\[[^\[\]]*\])*)", key)
    if not m:
        body[re.sub(r"\[.*$", "", key)] = UNKNOWN
        return
    parts = [m.group(1)] + re.findall(r"\[([^\[\]]*)\]", m.group(2))
    array = parts[-1] == ""
    path = parts[:-1] if array else parts
    if "" in path:
        body[parts[0]] = UNKNOWN  # an array of objects: not a shape any check here needs
        return
    node = body
    for part in path[:-1]:
        child = node.get(part)
        if child is None:
            child = node[part] = {}
        if not isinstance(child, dict):
            node[part] = UNKNOWN
            return
        node = child
    last = path[-1]
    if array:
        current = node.get(last)
        if current is None:
            node[last] = [value]
        elif isinstance(current, list):
            current.append(value)
        else:
            node[last] = UNKNOWN
        return
    if last in node and node[last] != value:
        value = UNKNOWN
    node[last] = value


def _get(body: dict | Unknown, key: str, ctx: Ctx, what: str):
    if isinstance(body, Unknown):
        return UNKNOWN
    value = body.get(key)
    if isinstance(value, Unknown):
        ctx.unknown(what)
    return value


def _branch_is_default(value, ctx: Ctx, what: str) -> bool:
    """True when `value` names the default branch, is missing (the API's default), or cannot be read."""
    if isinstance(value, Unknown):
        ctx.unknown(what)
        return False
    if value is None:
        return True
    name = str(value).strip().lower().removeprefix("refs/").removeprefix("heads/")
    return name in DEFAULT_BRANCHES


R_PULLS = re.compile(r"repos/.+/pulls")
R_REVIEW = re.compile(r"repos/.+/pulls/[^/]+/reviews(?:/[^/]+/events)?")
R_REF = re.compile(r"repos/.+/git/refs/heads/(.+)")
R_REFS = re.compile(r"repos/.+/git/refs")
R_MERGES = re.compile(r"repos/.+/merges")
R_CONTENTS = re.compile(r"repos/.+/contents(?:/.*)?")
R_UPSTREAM = re.compile(r"repos/.+/merge-upstream")
R_RUN = re.compile(r"repos/.+/actions/runs/[^/]+(?:/logs)?")
R_DISPATCH = re.compile(r"repos/.+/(?:actions/workflows/[^/]+/)?dispatches")


def check_api(args: list[Word], ctx: Ctx) -> None:
    req = parse_api(args, ctx)
    if req is None:
        return
    # GitHub Enterprise serves GraphQL at api/graphql; any path ending in it is read as GraphQL.
    if req.path.split("/")[-1] == "graphql" and not req.path.startswith("repos/"):
        check_graphql(req, ctx)
        return
    if req.method == "GET" or req.method == "HEAD":
        return
    if not _readable_path(req.path):
        ctx.unknown("a write to an API path built by the shell")
        return
    if _may_hide_route(req.path, req.method):
        ctx.unknown("a write to an API path whose route a shell expansion may hold")
        return
    path = req.path
    check_plan_api(req, ctx)
    if R_PULLS.fullmatch(path):
        draft = _get(body_of(req, ctx), "draft", ctx, "the PR's draft field is a shell expansion")
        if draft is not True and not (isinstance(draft, Unknown) and not ctx.strict):
            raise Refused("a PR opened through the API must send `-F draft=true`")
    elif R_REVIEW.fullmatch(path):
        event = _get(body_of(req, ctx), "event", ctx, "the review's event is a shell expansion or unreadable")
        if _has_unknown(event):
            ctx.unknown("the review's event is a shell expansion or unreadable")
        # A string, or one nested in a list or object (`-f event[]=APPROVE`).
        if any(s.strip().upper() == "APPROVE" for s in _strings(event)):
            raise Refused("an approving review is the code owner's, never the session's")
    elif m := R_REF.fullmatch(path):
        if m.group(1).strip("/") in DEFAULT_BRANCHES or HOLE in m.group(1):
            raise Refused("a write to the default branch's ref through the API")
    elif R_REFS.fullmatch(path):
        ref = _get(body_of(req, ctx), "ref", ctx, "the ref is a shell expansion or unreadable")
        if not isinstance(ref, Unknown) and ref is not None and _branch_is_default(ref, ctx, ""):
            raise Refused("a write to the default branch's ref through the API")
    elif R_MERGES.fullmatch(path):
        base = _get(body_of(req, ctx), "base", ctx, "the merge's base is a shell expansion or unreadable")
        if _branch_is_default(base, ctx, "the merge's base is unreadable"):
            raise Refused("a merge into the default branch through the API")
    elif R_CONTENTS.fullmatch(path):
        branch = _get(body_of(req, ctx), "branch", ctx, "the contents write's branch is unreadable")
        if _branch_is_default(branch, ctx, "the contents write's branch is unreadable"):
            raise Refused("a contents write to the default branch (or with no branch) through the API")
    elif R_UPSTREAM.fullmatch(path):
        branch = _get(body_of(req, ctx), "branch", ctx, "the sync's branch is unreadable")
        if _branch_is_default(branch, ctx, "the sync's branch is unreadable"):
            raise Refused("a fork sync of the default branch through the API")
    elif R_RUN.fullmatch(path):
        raise Refused(RUN_DELETE)
    elif R_DISPATCH.fullmatch(path):
        raise Refused(DISPATCH)


_RESOLVE = re.compile(r"(?<![A-Za-z0-9_])resolveReviewThread(?![A-Za-z0-9_])", re.I)
_REVIEW_MUTATION = re.compile(r"(?<![A-Za-z0-9_])(?:addPullRequestReview|submitPullRequestReview)"
                              r"(?![A-Za-z0-9_])", re.I)
_EVENT_APPROVE = re.compile(r"(?<![A-Za-z0-9_])event\s*:\s*\"?APPROVE(?![A-Za-z0-9_])", re.I)
_CREATE_PR = re.compile(r"(?<![A-Za-z0-9_])createPullRequest(?![A-Za-z0-9_])", re.I)
_REF_MUTATION = re.compile(r"(?<![A-Za-z0-9_])(?:updateRefs?|deleteRef)(?![A-Za-z0-9_])", re.I)
_BRANCH_MUTATION = re.compile(r"(?<![A-Za-z0-9_])(?:createCommitOnBranch|mergeBranch)(?![A-Za-z0-9_])", re.I)
_DEFAULT_NAME = re.compile(r"(?<![A-Za-z0-9_./-])(?:refs/heads/)?(?:main|master)(?![A-Za-z0-9_./-])", re.I)
# GraphQL strings (block and plain) and comments: what is said there sets nothing.
_GQL_NOISE = re.compile(r'"""[\s\S]*?(?:"""|$)|"(?:[^"\\\n]|\\.)*"?|#[^\n]*')


def _skeleton(query: str) -> str:
    """The query with its strings and comments blanked, for a check that something is set."""
    return _GQL_NOISE.sub(" ", query)


def _strings(value) -> list[str]:
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in [str(k), *_strings(v)]]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    if isinstance(value, Unknown):
        return []
    return [json.dumps(value) if not isinstance(value, str) else value]


def _has_unknown(value) -> bool:
    if isinstance(value, Unknown):
        return True
    if isinstance(value, dict):
        return any(_has_unknown(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_unknown(v) for v in value)
    return False


def check_graphql(req: Request, ctx: Ctx) -> None:
    body = body_of(req, ctx)
    if isinstance(body, Unknown):
        return
    query = body.get("query")
    if isinstance(query, Unknown) or (query is not None and not isinstance(query, str)):
        ctx.unknown("the GraphQL query is a shell expansion or unreadable")
        return
    if not query:
        return
    variables = body.get("variables") if req.input is not None else {k: v for k, v in body.items() if k != "query"}
    variables = variables if variables is not None else {}
    unknown = _has_unknown(variables)
    text = "\n".join([query, *_strings(variables)]) if not unknown else query
    check_plan_graphql(query, text, unknown, ctx, variables)
    if _RESOLVE.search(query):
        raise Refused("resolving a review thread is the code owner's, never the session's")
    if _REVIEW_MUTATION.search(query):
        if _EVENT_APPROVE.search(query) or any(s.strip().upper() == "APPROVE" for s in _strings(variables)
                                                if isinstance(s, str)):
            raise Refused("an approving review is the code owner's, never the session's")
        if unknown:
            ctx.unknown("the review mutation's variables are unreadable")
    if _CREATE_PR.search(query):
        draft = _graphql_draft(_skeleton(query), variables if isinstance(variables, dict) else {})
        if not draft and not (unknown and not ctx.strict):
            raise Refused("a PR opened through GraphQL must set `draft: true`")
    if _REF_MUTATION.search(query):
        raise Refused("a ref update through GraphQL names the ref by an ID this guard cannot read")
    if _BRANCH_MUTATION.search(query):
        if unknown:
            ctx.unknown("the branch mutation's variables are unreadable")
        elif _DEFAULT_NAME.search(text) or ("createcommitonbranch" in query.lower()
                                             and "branchname" not in text.lower()):
            raise Refused("a commit or merge onto the default branch through GraphQL")


# Not after `$`: `$draft: Boolean!` defines a variable, it sets nothing.
_DRAFT_ARG = re.compile(r"(?<![A-Za-z0-9_$])draft\s*:\s*(\$?[A-Za-z0-9_]+)", re.I)
_INPUT_VAR = re.compile(r"(?<![A-Za-z0-9_])input\s*:\s*\$([A-Za-z0-9_]+)", re.I)


def _graphql_draft(skeleton: str, variables: dict) -> bool:
    """Every draft the query sets is true, and it sets at least one.

    A draft is read where the query passes it: a literal (`draft: true`), a variable the query
    names (`draft: $d`), or a whole input object passed as a variable (`input: $i`). A variable
    the query does not use sets nothing.
    """
    settings = []
    for value in _DRAFT_ARG.findall(skeleton):
        settings.append(variables.get(value[1:]) if value.startswith("$") else value.lower() == "true")
    for name in _INPUT_VAR.findall(skeleton):
        obj = variables.get(name)
        if isinstance(obj, dict) and "draft" in obj:
            settings.append(obj["draft"])
    return bool(settings) and all(s is True for s in settings)


# ---- curl and wget -----------------------------------------------------------------------

_GITHUB_HOST = re.compile(r"(?:^|[/@.])github\.com(?:[:/]|$)|api\.github", re.I)
_CURL_WRITE_LONG = re.compile(r"^--(?:data(?:-[a-z]+)?|json|form(?:-string)?|upload-file|post30[123])(?:=|$)")
_CURL_REQUEST = re.compile(r"^(?:-X(.*)|--request(?:=(.*))?)$")
_WGET_WRITE = re.compile(r"^--(?:post-data|post-file|body-data|body-file|method)(?:=|$)")


def check_http(tool: str, args: list[Word], ctx: Ctx) -> None:
    if not any(_GITHUB_HOST.search(w.text) for w in args):
        return
    write = False
    for k, w in enumerate(args):
        t = w.text
        if tool == "wget":
            write |= bool(_WGET_WRITE.match(t))
            # -e / --execute runs a wgetrc command (`method=POST`); --config reads a wgetrc.
            if re.match(r"^(?:--execute|--config)(?:=|$)", t) or re.match(r"^-[A-Za-z]*e", t):
                ctx.unknown("wget takes commands from an option or a file")
            continue
        if re.match(r"^--config(?:=|$)", t) or re.match(r"^-[A-Za-z]*K", t):
            ctx.unknown("curl reads its options from a file")
        if _CURL_WRITE_LONG.match(t):
            write = True
        elif m := _CURL_REQUEST.match(t):
            method = m.group(1) or m.group(2) or (args[k + 1].text if k + 1 < len(args) else "")
            write |= method.upper() not in ("GET", "HEAD")
        elif re.match(r"^-[A-Za-z]+", t) and not t.startswith("--"):
            letters = t[1:]
            for idx, ch in enumerate(letters):
                if ch in "dFT":
                    write = True
                    break
                if ch == "X":
                    method = letters[idx + 1:] or (args[k + 1].text if k + 1 < len(args) else "")
                    write |= method.upper() not in ("GET", "HEAD")
                    break
    if write:
        raise Refused(f"a write to GitHub through {tool}; use `gh api`, which this guard reads")


# ---- the plan-run rows: git push and the REST, CLI and GraphQL spellings of a plan/* write ----------
# ADR 0063 §5 (E12, E14-E17, E19). planrows decides; this reads the command's spellings.

GIT_OPTION_VALUED = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix",
                     "--config-env", "--attr-source"}
# Configuration that changes what a push writes or where: remotes, push settings, URL rewrites, aliases.
GIT_STEERING_KEY = re.compile(r"^(?:remote\.|push\.|url\.|alias\.|include|branch\.|core\.(?:sshcommand|gitproxy))", re.I)
# Variables that steer git the same way, set on the command (`GIT_CONFIG_COUNT=1 … git push`).
GIT_STEERING_ENV = re.compile(r"^GIT_(?:CONFIG|DIR|WORK_TREE|COMMON_DIR|OBJECT_DIRECTORY|ALTERNATE|NAMESPACE|EXEC_PATH"
                              r"|SSH|PROXY)", re.I)
PR_CLOSE_VALUED, PR_CLOSE_BOOL = {"-c", "--comment", "-R", "--repo"}, {"-d", "--delete-branch"}
PR_MERGE_VALUED = {"-b", "--body", "-F", "--body-file", "-t", "--subject", "-A", "--author-email",
                   "--match-head-commit", "-R", "--repo"}
PR_MERGE_BOOL = {"-d", "--delete-branch", "-m", "--merge", "-r", "--rebase", "-s", "--squash", "--admin",
                 "--auto", "--disable-auto"}
PR_UPDATE_VALUED, PR_UPDATE_BOOL = {"-R", "--repo"}, {"--rebase"}
ISSUE_CLOSE_VALUED = {"-c", "--comment", "-r", "--reason", "-R", "--repo"}
PR_READY_VALUED, PR_READY_BOOL = {"-R", "--repo"}, {"--undo"}
# `gh pr edit` / `gh issue edit`: only the label options decide a plan row, but every valued option
# must be named so the selector is not read out of one's value.
EDIT_VALUED = {"-R", "--repo", "--add-assignee", "--remove-assignee", "--add-label", "--remove-label",
               "--add-project", "--remove-project", "--add-reviewer", "--remove-reviewer", "-b", "--body",
               "-F", "--body-file", "-B", "--base", "-m", "--milestone", "-t", "--title"}
EDIT_BOOL = {"--remove-milestone"}
_PR_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([0-9]+)(?:[/?#].*)?", re.I)
GH_ENV_REPO = re.compile(r"(?<![A-Za-z0-9_])GH_(?:REPO|HOST)=")
GH_PLACEHOLDER = re.compile(r"\{(?:owner|repo|branch)\}")  # gh expands these from the current checkout
_REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_BRANCH_WRITE_MUTATION = re.compile(r"(?<![A-Za-z0-9_])(?:createRef|createLinkedBranch|createCommitOnBranch|mergeBranch)(?![A-Za-z0-9_])",
                                    re.I)
# A mutation that writes a plan branch's head or closes its PR by an ID this guard cannot read.
_PR_ID_MUTATION = re.compile(r"(?<![A-Za-z0-9_])(?:closePullRequest|updatePullRequestBranch|mergePullRequest|enablePullRequestAutoMerge)(?![A-Za-z0-9_])", re.I)
_UPDATE_PR_MUTATION = re.compile(r"(?<![A-Za-z0-9_])updatePullRequest(?![A-Za-z0-9_])", re.I)
# A mutation that marks a PR ready, returns it to draft, writes a label or replies to a review thread. Each
# names the PR or the thread by an ID this guard cannot read, so in a run session it is refused: a plan row
# (E5-E10, §1a) rests on every one of them. Outside a run it is no plan row's business and passes.
_CREATE_PR_MUTATION = re.compile(r"(?<![A-Za-z0-9_])createPullRequest(?![A-Za-z0-9_])", re.I)
_RUN_ID_MUTATION = re.compile(r"(?<![A-Za-z0-9_])(?:markPullRequestReadyForReview|convertPullRequestToDraft"
                              r"|addLabelsToLabelable|removeLabelsFromLabelable|addPullRequestReviewThread"
                              r"(?:Reply)?|addPullRequestReview)(?![A-Za-z0-9_])", re.I)
_STATE_ARG = re.compile(r"(?<![A-Za-z0-9_])state\s*:|\$state(?![A-Za-z0-9_])", re.I)
# The routes check_plan_api reads: a ref, contents, a merge, a rename, a PR's branch update or merge, a PR close.
_PLAN_ROUTE = re.compile(r"(?:contents(?:/|$)|git/refs|merges$|branches/.+/rename$|pulls/[^/]+/(?:update-branch|merge)$"
                         r"|(?:pulls|issues)/[^/]+$)", re.I)
# The acts 2b added: a label write, a reply, a review, a PR opened through the API. Their placeholder
# and query-string refusals apply in a plan run, where the rows govern; outside one they are no plan
# row's business, as every other act's lookup is (ADR 0063 §5, §1a).
_ACT_ROUTE = re.compile(r"(?:issues/[^/]+/labels(?:/|$)|pulls$|pulls/comments/[^/]+$"
                        r"|pulls/[^/]+/(?:comments(?:/[^/]+/replies)?|reviews)$)", re.I)
# A directory change or an environment that steers git, anywhere in the command text (nested strings, earlier
# segments, `export`, `env -C`): the push's checkout and configuration are then not the ones the guard reads.
_DIR_CHANGE_TEXT = re.compile(r"(?<![A-Za-z0-9_./=-])(?:cd|pushd|popd)(?![A-Za-z0-9_-])|--chdir"
                              r"|(?<![A-Za-z0-9_-])env\b[^;|&\n]*[ \t]-[A-Za-z]*C", re.I)
STEER_ANYWHERE = re.compile(r"(?<![A-Za-z0-9_])(?:GIT_(?:CONFIG|DIR|WORK_TREE|COMMON_DIR|OBJECT_DIRECTORY|ALTERNATE"
                            r"|NAMESPACE|EXEC_PATH|SSH|PROXY)[A-Za-z0-9_]*|HOME|XDG_CONFIG_HOME)=", re.I)
# A variable assigned in the command: a command word the shell builds from it can be anything.
_DEFINES_VARS = re.compile(r"(?:^|[;&|(\s])[A-Za-z_][A-Za-z0-9_]*=")
# A command word assembled from a substitution and other characters (`$(printf gi)t`, `g$(echo i)t`): it can
# spell any command. A substitution followed by `/`, `.` or `-` (`$(dirname "$0")/x.sh`) is a path, not this.
_BUILT_COMMAND_WORD = re.compile(r"(?:^|[;&|(]\s*)(?:(?:\$\([^()]*\)|`[^`]*`)[A-Za-z0-9_]"
                                 r"|[A-Za-z0-9_]+(?:\$\(|`))")
# A command that names a push or a plan branch: where an unreadable command word may hide one.
_PUSHY = re.compile(r"(?<![A-Za-z0-9_.-])push(?![A-Za-z0-9_.-])|plan/|(?<![A-Za-z0-9_.-])git(?![A-Za-z0-9_-])", re.I)


def _refusal(err: planrows.FactsError) -> planrows.Refusal:
    return planrows.Refusal(f"a fact the plan rows need is unreadable ({err}); refusing (ADR 0063 §5)")


def _plan_ref(fetch, *args):
    try:
        return fetch(*args)
    except planrows.FactsError as err:
        raise _refusal(err) from err


def _act_head(plan: planguard.PlanSupport, read):
    """The branch an act writes, looked up for the plan rows. Outside a plan run a lookup that fails is no
    plan row's business — E23 and §1a govern runs, and E22 re-checks every server-side row at merge — so it
    passes; inside one it refuses (ADR 0063 §5, SEC-2.5)."""
    try:
        return read()
    except planrows.Refusal:
        if plan.in_run():
            raise
        return None


def _plan_head(plan: planguard.PlanSupport, number: int, repo: str | None) -> str | None:
    return _plan_ref(plan.facts.pr_head, number, repo)


def check_git(args: list[Word], before: list[Word], ctx: Ctx) -> None:
    """Judge the arguments after a `git` command word: a push is a ref write (E12, E14-E17, E19).

    `before` are the words ahead of `git` in its command (variable assignments, wrappers)."""
    plan = ctx.plan
    if plan is None:
        return
    i, dirs, steered, aliased = 0, [], False, False
    while i < len(args) and args[i].text.startswith("-") and not args[i].dynamic:
        name = args[i].text.partition("=")[0]
        value = args[i + 1] if i + 1 < len(args) else None
        if name == "-C" and value is not None:
            dirs.append(value)
        if name == "--config-env" or (name == "-c" and (value is None or value.dynamic
                                                       or GIT_STEERING_KEY.match(value.text.partition("=")[0]))):
            steered = True
            aliased |= name == "--config-env" or value is None or value.dynamic \
                or value.text.lower().startswith("alias.")
        steered |= name in ("--git-dir", "--work-tree")
        i += 2 if name in GIT_OPTION_VALUED and "=" not in args[i].text else 1
    if i >= len(args):
        return
    sub = args[i]
    if aliased:
        ctx.unknown("configuration on the command defines a git alias, which may stand for a push")
        return
    if sub.dynamic:
        ctx.unknown("the git command is a shell expansion")
        return
    if sub.text in ("send-pack", "http-push", "subtree"):
        ctx.unknown(f"`git {sub.text}` writes remote refs this guard does not read")
        return
    if sub.text != "push":
        return
    unquoted = re.sub(r"[\"'\\]", "", ctx.command)  # G"IT_CONFIG_COUNT"=1 spells GIT_CONFIG_COUNT=1
    if steered or any(d.dynamic or d.text.startswith("~") for d in dirs) \
            or STEER_ANYWHERE.search(ctx.command) or STEER_ANYWHERE.search(unquoted):
        ctx.unknown("a push run with configuration, a repository or a directory chosen by an option, a variable "
                    "or a shell expansion")
        return
    cwd = ctx.cwd if ctx.cwd_known else None
    for d in dirs:
        cwd = (cwd / d.text) if cwd is not None else None
    why = planguard.judge_push([(w.text, w.dynamic) for w in args[i + 1:]], plan, cwd, ctx.stability,
                               record=planacts.pushed)
    if why:
        ctx.unknown(f"a push this guard cannot read: {why}")


def _selected(group: list[Word], tail: list[Word], valued: set[str], boolean: set[str], ctx: Ctx,
              want_head: bool = True):
    """(head branch of the PR a gh command selects or None, the command's options, (number, repo) or None)."""
    plan = ctx.plan
    opts, positional = parse_options(tail, valued, boolean, strict=ctx.strict)
    repo = None
    pairs = [(group[k].text, group[k + 1]) for k in range(len(group) - 1) if group[k].text in GH_GLOBAL_VALUED]
    pairs += [(o.name, o.value) for o in opts if o.name in GH_GLOBAL_VALUED and o.value is not None]
    pairs += [("-R", Word(w.text.partition("=")[2] if w.text.startswith("--repo=") else w.text[2:], w.dynamic))
              for w in group if w.text.startswith("--repo=") or (w.text.startswith("-R") and len(w.text) > 2)]
    if GH_ENV_REPO.search(ctx.command):
        ctx.unknown("the repository is chosen by GH_REPO or GH_HOST")
        return None, opts, None
    for _, value in pairs:
        if value.dynamic or not _REPO_NAME.fullmatch(value.text):
            ctx.unknown("the repository option is a shell expansion or not owner/name")
            return None, opts, None
        repo = value.text
    if repo is None and ctx.cwd_known and _plan_ref(plan.gh_default_configured, ctx.cwd):
        ctx.unknown("gh acts on a repository set with `gh repo set-default`, which may not be origin's")
        return None, opts, None
    if not positional:
        head = _plan_ref(plan.pr_branch, ctx.cwd) if ctx.cwd_known else None
        if head is None:
            ctx.unknown("the PR is the current branch's, which cannot be read here")
        return head, opts, ((None, repo) if repo else None)
    sel = positional[0]
    if sel.dynamic:
        ctx.unknown("the PR is selected by a shell expansion")
        return None, opts, None
    text = sel.text
    if repo is None and not ctx.cwd_known and not _PR_URL.fullmatch(text):
        ctx.unknown("the repository is the one in a directory the guard cannot tell")
        return None, opts, None
    if "://" in text and not _PR_URL.fullmatch(text) or "/pull/" in text and not _PR_URL.fullmatch(text) \
            or "/issues/" in text:
        ctx.unknown("a PR selector that looks like a URL this guard cannot read")
        return None, opts, None
    if (m := _PR_URL.fullmatch(text)):
        return (_plan_head(plan, int(m.group(2)), m.group(1)) if want_head else None), opts, \
            (int(m.group(2)), m.group(1))
    if text.lstrip("#").isascii() and text.lstrip("#").isdigit():
        number = int(text.lstrip("#"))
        return (_plan_head(plan, number, repo) if want_head else None), opts, (number, repo)
    return text.rpartition(":")[2], opts, ((None, repo) if repo else None)  # a branch, or owner:branch


def _base_of(plan: planguard.PlanSupport, head: str | None, ref) -> str | None:
    """The branch the selected PR merges into."""
    if ref is not None and ref[0] is not None:
        return _plan_ref(plan.facts.pr_base, ref[0], ref[1])
    if ref is not None:  # a branch in a repository other than origin: its PR is not one the facts can read
        raise planrows.Refusal("a PR selected by branch in another repository cannot be read and is refused "
                               "(ADR 0063 §5)")
    if head is None:
        return None
    pr = _plan_ref(plan.facts.open_pr, head)
    return pr.base if pr is not None else None


def check_plan_gh(group: str, sub: str, pre: list[Word], tail: list[Word], ctx: Ctx) -> None:
    """`gh pr close|merge|update-branch` and `gh issue close` against a PR whose head is plan/* (E14-E17, E16),
    and a merge into a plan/* base."""
    plan = ctx.plan
    if plan is None:
        return
    if group == "pr" and sub == "close":
        head, _, _ = _selected(pre, tail, PR_CLOSE_VALUED, PR_CLOSE_BOOL, ctx)
        planrows.judge_close(head)
    elif group == "pr" and sub == "merge":
        opts, _ = parse_options(tail, PR_MERGE_VALUED, PR_MERGE_BOOL, strict=ctx.strict)
        delete = any(o.name in ("-d", "--delete-branch") and _bool(o) for o in opts)
        head, _, ref = _selected(pre, tail, PR_MERGE_VALUED, PR_MERGE_BOOL, ctx, want_head=delete)
        if delete:  # the head is read only to judge the delete; a merge reads the base
            planrows.judge_close(head)
        base = _base_of(plan, head, ref)
        if base is not None and planrows.plan_branch(base):
            _ref_write(plan, base, repo=ref[1] if ref else None)  # the merge writes the base branch
    elif group == "pr" and sub == "update-branch":
        head, _, ref = _selected(pre, tail, PR_UPDATE_VALUED, PR_UPDATE_BOOL, ctx)
        if head is not None and planrows.plan_branch(head):
            _ref_write(plan, head, repo=ref[1] if ref else None)
    elif group == "issue" and sub == "close":
        head, _, _ = _selected(pre, tail, ISSUE_CLOSE_VALUED, set(), ctx)
        planrows.judge_close(head)
    elif group == "pr" and sub == "ready":
        found = _act_head(plan, lambda: _selected(pre, tail, PR_READY_VALUED, PR_READY_BOOL, ctx))
        if found is None:
            return
        head, opts, _ = found
        if head is None:
            return  # _selected has already refused what it could not read
        if any(o.name == "--undo" and _bool(o) for o in opts):
            planacts.undraft(plan, head)
        else:
            planacts.ready_mark(plan, head)
    elif group in ("pr", "issue") and sub == "edit":
        found = _act_head(plan, lambda: _selected(pre, tail, EDIT_VALUED, EDIT_BOOL, ctx))
        if found is None:
            return
        head, opts, _ = found
        if head is None:
            return
        for o in opts:
            if o.name not in ("--add-label", "--remove-label"):
                continue
            if o.value is None or o.value.dynamic:
                _label_unreadable(plan, head, ctx)
                continue
            for name in o.value.text.split(","):  # gh takes a comma-separated list
                if not name.strip():
                    continue
                if o.name == "--add-label":
                    planacts.label_add(plan, head, name.strip())
                else:
                    planacts.label_remove(plan, head, name.strip())


def _ref_write(plan: planguard.PlanSupport, branch: str, source=None, force=False, delete=False,
               repo: str | None = None) -> None:
    """Judge one ref write; a write to another repository than the facts are read for cannot be judged."""
    write = planrows.Write(branch, source if isinstance(source, str) else None, force, delete)
    if planrows.plan_branch(branch):  # the rows that read nothing refuse first, wherever the write goes
        if delete:
            planrows.check_e16(branch)
        planrows.check_e15(write)
        planrows.check_e14(plan.plan_run)
    if repo is not None:
        ours = _plan_ref(plan.facts.repository)
        if ours.casefold() != repo.casefold():
            raise planrows.Refusal(f"a plan/* write to another repository than {ours}, which the facts are read "
                                   "for, cannot be judged and is refused (ADR 0063 §5)")
    planrows.judge_write(write, plan_run=plan.plan_run, facts=plan.facts)
    if planrows.plan_branch(branch):  # E18, E12's ledger conflicts, and the ledger's record (§1, §5)
        planacts.pushed(plan, branch, write.source)


def _label_unreadable(plan: planguard.PlanSupport, head: str, ctx: Ctx) -> None:
    """A label this guard cannot read on a plan/* PR: it may be the re-request label (fail closed)."""
    if planrows.plan_branch(head):
        raise planrows.Refusal("a label on a plan/* PR that this guard cannot read may be the re-request "
                               "label; refusing (ADR 0063 §5)")
    ctx.unknown("a label is a shell expansion")


def _forces(value) -> bool:
    return value not in (None, False, "false", "False", "FALSE", "0", "f", "F")


def check_plan_api(req: Request, ctx: Ctx) -> None:
    """A REST write to a `plan/*` ref, or a close of a PR whose head is one (E12, E14-E17, E19), and a merge into one."""
    plan = ctx.plan
    if plan is None or req.method in ("GET", "HEAD"):
        return
    orig = req.orig or req.path
    if req.method is None:
        ctx.unknown("the method of an API call is a shell expansion, so a write may be hidden")
        return
    method = req.method
    if (hidden := re.search(r"/(?:pulls|issues)/[^/]*" + HOLE + r"[^/]*(/(?:update-branch|merge))?$", orig, re.I)):
        if hidden.group(1):  # a branch update or a merge: the number decides which branch is written
            ctx.unknown("a PR number is a shell expansion, so a merge or branch update may be hidden")
        elif method == "PATCH":  # an edit closes the PR only with a state of closed
            state = _get(body_of(req, ctx), "state", ctx, "the PR's state is unreadable")
            if isinstance(state, str) and state.strip().lower() == "closed":
                ctx.unknown("a PR number is a shell expansion, so a close may be hidden")
        return
    found = re.fullmatch(r"repos/([^/]+)/([^/]+)/(.+)", orig, re.I)
    if found is None:
        return
    if HOLE in found[1] + found[2]:
        if _PLAN_ROUTE.match(found[3]):  # only the routes that write a branch or close a PR need the repository
            ctx.unknown("the repository of an API write is a shell expansion")
        return
    repo, rest = f"{found[1]}/{found[2]}", found[3]
    acts = _ACT_ROUTE.match(rest) and plan.in_run(ctx.cwd)
    if (_PLAN_ROUTE.match(rest) or acts) and GH_PLACEHOLDER.search(ctx.command):
        ctx.unknown("gh fills {owner}, {repo} or {branch} in this call, which the guard cannot read")
        return
    if req.query and (_PLAN_ROUTE.match(rest) or acts):
        ctx.unknown("a write to a ref, contents, merge, PR or review route with a query string, which may "
                    "carry its fields")
        return
    if re.fullmatch(r"contents(?:/.*)?", rest, re.I):
        branch = planrows.normalise(_get(body_of(req, ctx), "branch", ctx, "the contents write's branch is unreadable"))
        if branch and planrows.plan_branch(branch):
            _ref_write(plan, branch, repo=repo)
    elif (m := re.fullmatch(r"git/refs/heads/(.+)", rest, re.I)):
        branch = m.group(1)
        if HOLE in branch or not planrows.plan_branch(branch):
            return
        if method == "DELETE":
            _ref_write(plan, branch, delete=True, repo=repo)
            return
        body = body_of(req, ctx)
        sha = _get(body, "sha", ctx, "the ref update's sha is unreadable")
        force = _get(body, "force", ctx, "the ref update's force flag is unreadable")
        _ref_write(plan, branch, sha, _forces(force) and not isinstance(force, Unknown), repo=repo)
    elif rest.lower() == "git/refs":
        body = body_of(req, ctx)
        ref = planrows.normalise(_get(body, "ref", ctx, "the new ref's name is unreadable"))
        if ref and planrows.plan_branch(ref):
            _ref_write(plan, ref, _get(body, "sha", ctx, "the sha is unreadable"), repo=repo)
    elif rest.lower() == "merges":
        base = planrows.normalise(_get(body_of(req, ctx), "base", ctx, "the merge's base is unreadable"))
        if base and planrows.plan_branch(base):
            _ref_write(plan, base, repo=repo)
    elif (m := re.fullmatch(r"branches/(.+)/rename", rest, re.I)):
        planrows.check_e16(m.group(1))  # a rename deletes the old name
        name = planrows.normalise(_get(body_of(req, ctx), "new_name", ctx, "the new branch name is unreadable"))
        if name and planrows.plan_branch(name):
            _ref_write(plan, name, repo=repo)
    elif (m := re.fullmatch(r"pulls/([0-9]+)/update-branch", rest, re.I)):
        head = _plan_head(plan, int(m.group(1)), repo)
        if head is not None and planrows.plan_branch(head):
            _ref_write(plan, head, repo=repo)
    elif (m := re.fullmatch(r"pulls/([0-9]+)/merge", rest, re.I)):
        base = _plan_ref(plan.facts.pr_base, int(m.group(1)), repo)
        if base is not None and planrows.plan_branch(base):
            _ref_write(plan, base, repo=repo)
    elif (m := re.fullmatch(r"issues/([0-9]+)/labels", rest, re.I)) and method in ("POST", "PUT", "DELETE"):
        head = _act_head(plan, lambda: _plan_head(plan, int(m.group(1)), repo))
        if head is not None and planrows.plan_branch(head):
            if method == "DELETE":  # no label named: it removes every one of them
                planacts.label_remove(plan, head, planacts.ALL_LABELS)
            else:
                if method == "PUT":
                    # A replacement removes every label it does not name, which §1a allows for
                    # plan:continue only; the removal is judged before the additions.
                    planacts.label_remove(plan, head, planacts.ALL_LABELS)
                _labels_of(body_of(req, ctx), ctx, head, plan)
    elif (m := re.fullmatch(r"issues/([0-9]+)/labels/(.+)", rest, re.I)) and method == "DELETE":
        head = _act_head(plan, lambda: _plan_head(plan, int(m.group(1)), repo))
        if head is not None and planrows.plan_branch(head):
            if HOLE in m.group(2):
                _label_unreadable(plan, head, ctx)
            else:
                planacts.label_remove(plan, head, unquote(m.group(2)))
    elif rest.lower() == "pulls" and method == "POST":
        body = body_of(req, ctx)
        head = planrows.normalise(_get(body, "head", ctx, "the new PR's head branch is unreadable"))
        if isinstance(head, Unknown) or (head and not isinstance(head, str)):
            raise planrows.Refusal("a PR opened through the API whose head branch this guard cannot read "
                                   "may be a plan/* branch, which E23 governs; refusing (ADR 0063 §5)")
        if head and planrows.plan_branch(head.rpartition(":")[2]):
            _plan_draft_branch(plan, head.rpartition(":")[2], ctx)
    elif (m := re.fullmatch(r"pulls/([0-9]+)/reviews", rest, re.I)) and method == "POST":
        head = _act_head(plan, lambda: _plan_head(plan, int(m.group(1)), repo))
        if head is not None and planrows.plan_branch(head):
            # A review carries comment bodies of its own, in a shape this guard does not read, so in a
            # run it cannot be told from a tagged reply (ADR 0063 §1a, E8).
            raise planrows.Refusal("a review posted on a plan/* PR carries comment bodies this guard "
                                   "cannot read as outcome tags; reply to each thread instead "
                                   "(ADR 0063 §1a)")
    elif re.fullmatch(r"pulls/comments/[0-9]+", rest, re.I) and method in ("PATCH", "PUT"):
        raise planrows.Refusal("editing a review comment on a plan/* PR can turn one outcome tag into "
                               "another, which this guard cannot judge; reply instead (ADR 0063 §1a)")
    elif (m := re.fullmatch(r"pulls/([0-9]+)/comments(?:/[0-9]+/replies)?", rest, re.I)) and method == "POST":
        head = _act_head(plan, lambda: _plan_head(plan, int(m.group(1)), repo))
        if head is not None and planrows.plan_branch(head):
            body = _get(body_of(req, ctx), "body", ctx, "a reply's body is unreadable")
            if isinstance(body, Unknown) or body is None:
                raise planrows.Refusal("a reply on a plan/* PR whose body this guard cannot read may carry "
                                       "an outcome tag; refusing (ADR 0063 §1a)")
            planacts.reply(plan, head, body if isinstance(body, str) else str(body))
    elif method == "PATCH" and (m := re.fullmatch(r"(?:pulls|issues)/([0-9]+)", rest, re.I)):
        body = body_of(req, ctx)
        state = _get(body, "state", ctx, "the PR's state is unreadable")
        if isinstance(state, str) and state.strip().lower() == "closed":
            planrows.judge_close(_plan_head(plan, int(m.group(1)), repo))
        if _has_key(body if isinstance(body, dict) else {}, "draft") or isinstance(body, Unknown):
            head = _act_head(plan, lambda: _plan_head(plan, int(m.group(1)), repo))
            if head is not None and planrows.plan_branch(head):
                draft = _get(body, "draft", ctx, "the PR's draft flag is unreadable")
                if isinstance(draft, Unknown):
                    raise planrows.Refusal("a PR edit on a plan/* PR whose draft flag this guard cannot read "
                                           "may mark it ready; refusing (ADR 0063 §5)")
                if draft is False:
                    planacts.ready_mark(plan, head)
                else:
                    planacts.undraft(plan, head)


def _labels_of(body, ctx: Ctx, head: str, plan: planguard.PlanSupport) -> None:
    """Every label a `labels` write adds, from the body's list or its bare string form."""
    if isinstance(body, Unknown):
        _label_unreadable(plan, head, ctx)
        return
    found = body.get("labels") if isinstance(body, dict) else None
    names = found if isinstance(found, list) else [found]
    if not names or any(not isinstance(n, str) for n in names):
        _label_unreadable(plan, head, ctx)
        return
    for name in names:
        planacts.label_add(plan, head, name)


def _has_key(value, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_has_key(v, key) for v in value.values())
    return isinstance(value, list) and any(_has_key(v, key) for v in value)


def check_plan_graphql(query: str, text: str, unknown: bool, ctx: Ctx, variables=None) -> None:
    if ctx.plan is None:
        return
    if _CREATE_PR_MUTATION.search(query) and (unknown or "plan/" in text.casefold()):
        raise planrows.Refusal("a GraphQL mutation that opens a PR from plan/* cannot be judged for E23; "
                               "open it with `gh pr create --draft` (ADR 0063 §5)")
    if ctx.plan.in_run(ctx.cwd) and _RUN_ID_MUTATION.search(query):
        raise planrows.Refusal("a GraphQL mutation that marks a PR ready, returns it to draft, writes a label "
                               "or replies to a review thread names them by an ID this guard cannot read; in a "
                               "plan run it is refused. Use gh's own commands (ADR 0063 §5, §1a)")
    if _PR_ID_MUTATION.search(query):  # the PR is named by an ID this guard cannot read
        planrows.check_e16(planrows.PLAN_PREFIX)
    if _UPDATE_PR_MUTATION.search(query):
        skeleton = _skeleton(query)  # strings and comments removed: a word in a PR body is not an argument
        if unknown or _STATE_ARG.search(skeleton) or _has_key(variables, "state"):
            planrows.check_e16(planrows.PLAN_PREFIX)  # a state argument can close the PR
    if _BRANCH_WRITE_MUTATION.search(query):
        if unknown:
            ctx.unknown("the ref or commit mutation's variables are unreadable")
        elif "\\" in query:  # a string escape can spell any letter of a branch name
            raise planrows.Refusal("E14: a GraphQL ref or commit mutation with string escapes cannot be read and "
                                   "is refused; push with git (ADR 0063 §5)")
        elif "plan/" in text.casefold():
            raise planrows.Refusal("E14: a GraphQL ref or commit mutation naming plan/* cannot be judged and "
                                   "is refused; push with git (ADR 0063 §5)")


# ---- one command -------------------------------------------------------------------------

def _repo_option(word: Word) -> bool:
    """A repository option; a short form needs its owner/name, since another tool may have its own `-R`."""
    return word.text in GH_GLOBAL_VALUED or word.text.startswith("--repo=") or (
        word.text.startswith("-R") and "/" in word.text)


def _may_be_gh(rest: list[Word]) -> bool:
    """What follows a command word the shell builds says it may be gh.

    `run` is a common second word for other tools (`$NPM run $SCRIPT`), so for `run` and `workflow`
    a repository option before it or the refused subcommand after it has to show it. `${G}h run $SUB` is
    read as another tool and passes (ADR 0055).
    """
    k = _after_options(rest, 0)
    if k >= len(rest) or rest[k].dynamic:
        return False
    group = rest[k].text
    if group in ("pr", "issue", "api", "repo", "alias"):
        return True
    if group not in GROUP_REFUSALS:
        return False
    if any(_repo_option(w) for w in rest[:k] if not w.dynamic):
        return True
    # An expansion here may be an option, so the refused subcommand can stand after one.
    j = _after_options(rest, k + 1)
    while j < len(rest) and rest[j].dynamic:
        j = _after_options(rest, j + 1)
    return j < len(rest) and rest[j].text == GROUP_REFUSALS[group][0]


def check_segment(words: list[Word], ctx: Ctx) -> None:
    appends = False  # an earlier word (xargs, parallel) adds arguments the guard never sees
    first = next((j for j, x in enumerate(words) if not _ASSIGNMENT.match(x.text)), len(words))
    for k, w in enumerate(words):
        name = _ASSIGNMENT.sub("", w.text) if not ctx.strict else w.text
        base = posixpath.basename(name)
        rest = words[k + 1:]
        # An expansion may be gh, anywhere a wrapper (sudo, env, nice) could put it.
        is_gh = (not w.dynamic and base == "gh") or (w.dynamic and _may_be_gh(rest))
        if is_gh:
            if appends:
                ctx.unknown("xargs or parallel appends arguments to gh that this guard cannot read")
            check_gh(rest, ctx)
        elif not w.dynamic and base == "git":
            if appends:
                ctx.unknown("xargs or parallel appends arguments to git that this guard cannot read")
            check_git(rest, words[:k], ctx)
        elif not w.dynamic and base == "git-push":  # the binary behind the push subcommand
            check_git([Word("push", False), *rest], words[:k], ctx)
        elif not w.dynamic and base in ("git-send-pack", "git-http-push", "git-subtree"):
            ctx.unknown(f"`{base}` writes remote refs this guard does not read")
        elif w.dynamic and k == first and (_PUSHY.search(w.text) or any(x.dynamic or x.text == "push" for x in rest)) \
                and (_PUSHY.search(ctx.command) or _DEFINES_VARS.search(ctx.command)):
            ctx.unknown("a command word the shell builds is followed by a push or by words it builds")
        elif not w.dynamic and base in APPENDERS:
            appends = True
        elif not w.dynamic and base == "env":
            for j, x in enumerate(rest):
                t = x.text
                if re.fullmatch(r"-[A-Za-z]*S|--split-string", t) and j + 1 < len(rest):
                    split = rest[j + 1]  # -S, a cluster ending in S, or --split-string: the next word
                elif m := re.fullmatch(r"(?:-[A-Za-z]*S|--split-string=)(.+)", t, re.S):
                    split = Word(m.group(1), x.dynamic)
                else:
                    continue
                if split.dynamic:
                    ctx.unknown("env -S runs a command built by the shell")
                _runs_script(appends, ctx)
                judge(split.text, ctx)  # env -S splits its argument into a command
                break
        elif not w.dynamic and base in ("curl", "wget"):
            check_http(base, rest, ctx)
        elif not w.dynamic and base == "eval":
            _runs_script(appends, ctx)
            judge(" ".join(x.text for x in rest), ctx)
        elif not w.dynamic and base in SHELLS:
            for j, x in enumerate(rest):
                if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", x.text) and j + 1 < len(rest):
                    if rest[j + 1].dynamic:
                        ctx.unknown("a shell runs a command built by the shell")
                    _runs_script(appends, ctx)
                    judge(rest[j + 1].text, ctx)
                    break


def _runs_script(appends: bool, ctx: Ctx) -> None:
    """A script run behind xargs or parallel takes stdin text (`-I{}` substitutes it into the script)."""
    if appends:
        ctx.unknown("xargs or parallel feeds stdin into a script that this guard cannot read")


def runs_text(command: str, naive: list[list[Word]]) -> bool:
    """The command holds a word that runs its input or an argument as a command.

    Substitutions need no naive pass: the quote-aware pass reads them, quoted or not.
    """
    for seg in naive:
        names = [posixpath.basename(_ASSIGNMENT.sub("", w.text)) for w in seg]
        if any(n in RUNS_TEXT for n in names):
            return True
        if "env" in names and any(re.match(r"-[A-Za-z]*S|--split-string", w.text) for w in seg):
            return True  # env -S splits a string into a command
    return False


def judge(command: str, ctx: Ctx, parsed: list[list[Word]] | None = None) -> None:
    """Judge every command in `command`; nesting (`bash -c`, `eval`, an alias) recurses here.

    The naive pass runs only when something in the command can run quoted text, so a comment or
    a commit message that mentions a refused command is not read as one.
    """
    if ctx.strict and _BUILT_COMMAND_WORD.search(command):
        ctx.unknown("a command word assembled from a substitution and other characters")
    naive = naive_segments(command)
    passes = [(False, naive)] if not ctx.strict else [(True, parsed if parsed is not None else segments(command))]
    if ctx.strict and runs_text(command, naive):
        passes.append((False, naive))
    for strict, segs in passes:
        sub = Ctx(ctx.cwd, ctx.cwd_known, strict, ctx.command, ctx.plan, ctx.stability)
        for words in segs:
            check_segment(words, sub)





def decide(payload: dict, plan: planguard.PlanSupport | None = None) -> tuple[int, str]:
    if payload.get("tool_name") not in (None, "Bash"):
        return ALLOW, ""
    command = (payload.get("tool_input") or {}).get("command")
    if command is None:
        return ALLOW, ""
    if not isinstance(command, str):
        return BLOCK, "guard-github: blocked: the command is not a string"
    cwd = payload.get("cwd")
    parsed = segments(command)
    cwd_known = isinstance(cwd, str) and cwd.startswith("/") and not any(
        w.text in DIR_CHANGERS for s in parsed for w in s) and not _DIR_CHANGE_TEXT.search(command)
    where = Path(cwd) if isinstance(cwd, str) and cwd else Path("/")
    support = plan or planguard.PlanSupport(where)
    # E21: in a run session nothing reaches GitHub until the gate has passed for the current lock, base
    # and default-branch head. Every command here is one the prefilter read as touching GitHub or a
    # remote ref, so each is refused while the gate fails — guard-paths refuses the whole shell anyway,
    # and this is the half that holds if only this hook is registered (ADR 0063 §3).
    if support.in_run(where) and not guardgate.holds(where):
        return BLOCK, ("guard-github: blocked: E21: the enforcement gate has not passed for this "
                       "session's lock, base and default-branch head, so a plan run reaches GitHub with "
                       "nothing (ADR 0063 §3, AGW-4). A recorder may have been started; the installed "
                       "bin/trimtab with `check-enforcement` says which row is not shown. Only the "
                       "operator lifts this, by exporting HOOK_ALLOW_GITHUB=1 before launching claude.")
    try:
        judge(command, Ctx(where, cwd_known, True, command, support,
                           planguard.stability_of(command)), parsed)
    except planrows.Refusal as why:
        return BLOCK, (f"guard-github: blocked: {why}. This is a plan-run rule "
                       "(docs/adr/0063-the-plan-run-enforcement-gate.md); do not look for another route. "
                       "Only the operator lifts it, by exporting HOOK_ALLOW_GITHUB=1 before launching claude.")
    except Refused as why:
        return BLOCK, (f"guard-github: blocked: {why}. This is a GitHub write the operator makes "
                       "(docs/adr/0037-guard-github-refuses-the-github-writes-the-operator-owns.md); "
                       "do not look for another route. Only the operator lifts it, by exporting "
                       "HOOK_ALLOW_GITHUB=1 before launching claude.")
    return ALLOW, ""


def _out_of_time(signum, frame):
    raise TimeoutError("the deadline passed")


def main() -> int:
    signal.signal(signal.SIGALRM, _out_of_time)
    signal.alarm(DEADLINE)
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
        code, message = decide(payload)
    except Exception as err:  # fail closed: a guard that cannot read the call blocks it (ADR 0001)
        print(f"guard-github: blocked: the call could not be read ({type(err).__name__})", file=sys.stderr)
        return BLOCK
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
