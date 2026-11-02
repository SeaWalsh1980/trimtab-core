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
- a write to a github.com host through `curl` or `wget`, which this guard cannot read.

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
        if group.text in ("pr", "repo", "alias", "run", "workflow"):
            ctx.unknown("the gh command is a shell expansion")
        return
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
    if named[:2] in (["pr", "review"], ["repo", "sync"]):
        return True
    return len(named) == 1 and named[0] in ("api", "pr", "repo", "run", "workflow")


# ---- gh api ------------------------------------------------------------------------------

@dataclass
class Request:
    method: str | None             # None: a shell expansion
    path: str                      # normalised, lower case; HOLE where the shell substitutes
    fields: list[tuple[str, Word]]  # (option, "key=value" word), in order
    input: Word | None


_ID_PARENTS = {"repos", "pulls", "issues", "comments", "reviews", "replies", "check-runs", "check-suites",
               "releases", "labels", "milestones", "runs", "jobs", "assets", "commits", "statuses",
               "deployments", "events"}


def _norm_path(raw: str) -> str:
    path = re.sub(r"^[a-z][a-z0-9+.-]*://[^/]*", "", raw, flags=re.I)
    path = path.split("?", 1)[0].split("#", 1)[0]
    path = unquote(path).lstrip("/")
    path = re.sub(r"^api/v3/", "", path, flags=re.I)
    path = posixpath.normpath("/" + path).lstrip("/") if path else ""
    # GitHub also serves a repository by its id (`repositories/<id>/…`); the id fills both slots.
    return re.sub(r"^repositories/([^/]+)", r"repos/\1/\1", path.lower())


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
    return Request(method, _norm_path(raw.text), fields, source)


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
    if req.path.split("/")[-1] == "graphql":
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
    if group in ("pr", "api", "repo", "alias"):
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
    naive = naive_segments(command)
    passes = [(False, naive)] if not ctx.strict else [(True, parsed if parsed is not None else segments(command))]
    if ctx.strict and runs_text(command, naive):
        passes.append((False, naive))
    for strict, segs in passes:
        sub = Ctx(ctx.cwd, ctx.cwd_known, strict, ctx.command)
        for words in segs:
            check_segment(words, sub)


def decide(payload: dict) -> tuple[int, str]:
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
        w.text in DIR_CHANGERS for s in parsed for w in s)
    try:
        judge(command, Ctx(Path(cwd) if isinstance(cwd, str) and cwd else Path("/"), cwd_known, True, command),
              parsed)
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
