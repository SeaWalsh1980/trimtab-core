# 0028. guard-mcp passes set_monitor only for PRs the session opened

## Status

Proposed (2026-10-17). Becomes accepted once the trial below holds in a
desktop session on a base that carries this change. Amends ADR 0013
(`docs/adr/0013-guard-mcp-asks-before-mcp-tools-that-may-write.md`) for one
tool, and narrows the accepted risk of ADR 0027
(`docs/adr/0027-guard-mcp-passes-desktop-app-tools-by-exact-name.md`); the
rest of both stands. ADR 0020
(`docs/adr/0020-routines-refuse-the-remote-session-mcp-tools.md`) still
governs cloud routines.

## Context

ADR 0027 lets guard-mcp pass `mcp__ccd_pr__set_monitor` by exact name, so a
desktop session can switch the app's Auto-fix on for its PR without a
prompt. guard-mcp reads names, never arguments (ADR 0013), so the pass covers
every PR the app has bound to the session. The app binds some PRs by itself:
when the session runs `gh pr create`, reports a PR URL, or pushes a branch
that has an open PR. ADR 0027 accepted Auto-fix on any of them and tracked
narrowing it to PRs the session opened in issue #69. With Auto-fix on, the
session receives that PR's comments and CI output, untrusted text, while it
can push to the PR.

The operator wants this to run unattended: a desktop session in auto mode,
with nobody answering prompts. ADR 0013 measured that an `ask` ends as a
denial in a non-interactive run. A desktop session is interactive, so an
`ask` there shows a prompt and the session waits for it. For this tool an
`ask` would stop exactly the run the operator wants to keep going.

Measured for this record:

- **Claude Code 2.1.302** (read from the CLI): a PostToolUse payload for
  `Bash` carries `tool_input.command` and a `tool_response` of `stdout`,
  `stderr`, `interrupted` and, when they apply, `backgroundTaskId`,
  `persistedOutputPath` and `noOutputExpected`. A failed tool call raises a
  separate `PostToolUseFailure` event, not PostToolUse. The payload carries
  `session_id`.
- **Claude Code 2.1.302, a nonzero exit** (read from the CLI's code,
  2026-10-17): PostToolUse runs only when a tool returns, and
  PostToolUseFailure only when it throws. Bash judges an exit by the last
  command in the chain: only `grep`, `rg`, `egrep`, `fgrep`, `find`, `diff`,
  `test`, `[`, `git diff` and `git grep` treat exit 1 as success. For any
  other command, `gh` included, a nonzero exit throws, so the call raises
  PostToolUseFailure and never reaches the recorder. A user interrupt
  returns normally with `interrupted: true`. `tool_response` carries no exit
  status; `returnCodeInterpretation` is set only for those non-error codes.
  So a `git push … && gh pr create` whose create fails cannot record a line
  that something else wrote to stdout: the recorder never sees that call.
- **Desktop app 44.7.2** (read from its `ccd_pr` server code, 2026-10-17):
  - `set_monitor` takes `auto_fix`, `address_comments`,
    `auto_archive_on_close` and `url`, none required.
  - With `auto_fix` set, the app looks up the session's binding whose host,
    repository (case-insensitive) and number match `url`, and switches
    Auto-fix on that binding only. A `url` the session has not bound is
    refused as not bound to the session; a missing `url` is refused with a
    request to pass it.
  - `address_comments` must equal `auto_fix` when passed, so it never
    arrives alone.
  - `auto_archive_on_close` without `auto_fix` is a session-wide switch that
    touches no PR, and the app refuses `url` on it.
  - The app refuses every `ccd_pr` tool in a session it classes as
    unattended, whatever a hook answers.

So the PR a call acts on is the one its `url` names. A guard that checks
`url` against the PRs the session created confines Auto-fix to those PRs:
the guard passes only those URLs, and the app acts only on the PR in `url`.

## Decision

**Record.** A new PostToolUse hook, `hooks/pr-monitor-record.sh`, matcher
`Bash`, runs `trimtab/adapters/pr_monitor.py`. When a Bash call created a PR,
it appends the PR's canonical key to a list for that session. It records
nothing on any doubt, writes nothing to stdout, and never names a URL or a
session on stderr.

- **What counts as a create.** The command must parse (shlex) into this
  shape, joined only by `&&`, with nothing after the create:

  `[cd <literal path> &&] [git push [args] &&] <create>`

  where `<create>` is `gh pr create` (without `--web` or `--dry-run`), or
  `gh api` with a POST to `repos/<owner>/<repo>/pulls` (without `--jq`,
  `--template`, `--include`, `--paginate`, `--silent` or `--verbose`).
  Anywhere in the command: no `;`, `||`, `|`, `&`, redirection, `$(`,
  backtick, `${`, variable, newline or `VAR=` prefix; `gh` is the bare
  name; `cd` takes exactly one literal path and not `-`; `git push` takes no
  `-c` and no `--porcelain`.
- **Where the URL comes from.** Only from a successful call's stdout, never
  from the command text. For `gh pr create`, stdout is exactly one line and
  it is a PR URL. For `gh api`, stdout is one JSON object whose `html_url`
  is a PR URL, whose integer `number` matches it, and whose
  `base.repo.full_name`, when present, matches its owner and repository.
- **Bound to where the create went.** A server only vouches for its own PRs,
  so the PR must be on the host the command sent the create to, and in the
  repository the command names, if it names one:
  - `gh pr create`: the host and repository of `-R`/`--repo`
    (`OWNER/REPO`, `HOST/OWNER/REPO` or an https URL; given twice, or in any
    other form, nothing is recorded), else `github.com` and any repository;
  - `gh api`: the `--hostname` value, else `github.com`; and the path's
    owner and repository unless it uses gh's `{owner}/{repo}` placeholders.
- **The key.** `(host, owner, repo, number)`: https only, no userinfo, no
  port; host lower-cased and limited to `[a-z0-9.-]`; path exactly
  `/<owner>/<repo>/pull/<n>` with an optional trailing `/`; owner and
  repository limited to GitHub's characters and lower-cased; `n` decimal
  with no leading zero; query and fragment dropped. A `%`, `/pulls/`, a
  sub-page or an API URL produces no key. A fork's `html_url` is the base
  repository's PR, which is what the app binds, so forks need no special
  case. GitHub Enterprise works without a host list, provided the command
  names its host (above).

**Store.** `<config dir>/guard-state/pr-monitor/<sha256 of session_id>.json`,
JSON with `"v": 1`. The session ID must match `[A-Za-z0-9_-]{1,128}`; the
file name is its hash, so the ID is never written. Each entry carries the
time it was recorded.

- An entry expires after **2 days** and then counts as absent. A list holds
  at most **30** PRs; past that the oldest drops. Expiry gates only turning
  Auto-fix on: a monitor already on stays on in the app.
- Every record rewrites the session's own list, dropping expired entries,
  and deletes other sessions' lists whose newest entry has expired. A list
  with another integer `v` was written by another version of this module
  and is kept while its file is younger than the TTL, since that version
  rewrites it on every record and prunes its own (added after review: a v1
  recorder would otherwise delete a later version's live lists). `v` must be
  exactly the integer 1 to be read; `true` or `1.0` is refused.
- Writes take an exclusive `flock` and replace the file atomically. A replay
  of the same create leaves one entry.
- The directory is created `0700` and checked with `lstat` on every use: a
  real directory, not a symlink, owned by the effective user, no group or
  other permission bits. Files are opened with `O_NOFOLLOW`, mode `0600`.
  Anything else and the check denies and the recorder writes nothing.
- guard-paths adds `guard-state` to the config locations it protects, so a
  session's Write, Edit, MultiEdit, NotebookEdit or Bash command cannot
  write there. Reads stay open. Hooks write outside those tools. It is
  protected by its physical path as well (added after review): when the
  config dir is a link, a path spelt through the link's target would
  otherwise miss the root. A config dir that exists and does not resolve
  blocks every call except a Read, as a config link that does not resolve
  already does for writes. A Read cannot write the list, and it is what a
  session needs to see why the dir does not resolve (added after review: the
  first version blocked Reads too). An empty or unknown tool still blocks.

**Check.** guard-mcp gives `mcp__ccd_pr__set_monitor` its own one-name list,
`CCD_OWN_PR`, and takes it off `CCD_ALLOW`. For that name only, it hands the
payload to the same module and passes when the module exits 0 and prints
`pass`:

- `url` is a string whose key is on the session's list and has not expired;
  or
- `url`, `auto_fix` and `address_comments` are all absent: an auto-archive
  switch, which touches no PR.

**Every other answer for this tool is a deny**, `permissionDecision:
"deny"` with exit 0, never an `ask`. That includes a URL not on the list, a
missing or malformed `url` with `auto_fix` or `address_comments`, a missing
or bad `session_id`, a missing, unreadable, corrupt, expired, unknown-version
or wrongly permissioned list, a missing `python3`, and any parse error. The
reason is fixed text that names no URL, session or hash. It tells the model
that Auto-fix passes only for a PR this session created in the recorded
shape, with its `url`; not to retry with another PR's URL; and to carry on
without Auto-fix and report it to the operator. A check that does not run
to a verdict (a nonzero exit, no output, or an answer the guard does not
know) is denied with its own fixed reason, which says the check could not
run and nothing about the url (added after review: it first fell through
to the url reason, which sent a broken install hunting a foreign PR).

Every other tool name keeps its ADR 0013 or ADR 0027 answer. Of all MCP
tools, only `set_monitor` has its arguments read, and only it can be denied.

## Options considered

- **Leave ADR 0027's pass as it is.** Rejected. Auto-fix on any bound PR
  includes a PR bound because the session pushed to its branch or saw its
  link, which is the injection route issue #69 names.
- **Ask, as every other guard-mcp answer does.** Rejected by the operator. In
  a desktop session an `ask` waits for a person, so an unattended run stalls
  on its first foreign PR. A deny returns to the model as a tool error, and
  the session carries on without Auto-fix. The cost: a person can no longer
  approve Auto-fix for a foreign PR at a prompt; they switch it on in the
  app's own PR bar instead.
- **Exit 2.** Rejected. guard-mcp answers in JSON with exit 0, and a JSON
  deny carries a reason the model reads.
- **Deny `set_monitor` outright.** Rejected. No unattended session could
  watch its own PRs, which is what ADR 0027 was for.
- **Gate `bind_pr` instead.** Rejected. It already asks (ADR 0027), and the
  app binds PRs without it.
- **Check the app's bound PRs, from `get_status` output or the app's
  state on disk.** Rejected. The app's state is undocumented and changes
  between builds, and the app already acts only on the PR in `url`, so the
  guard's own list is enough.
- **Take the URL from the command text, or any URL anywhere in stdout.**
  Rejected. `echo <url>; gh pr create …` would record a PR the session did
  not create. Taking it only from a single, allowlisted command's one-line
  stdout keeps forging out.
- **Only a lone create, no prefixes.** Rejected by the operator. Pushing and
  creating in one call is common, and `cd <path>` and `git push` print
  nothing to stdout in normal use. Should one print, through `CDPATH` or a
  pre-push hook, stdout has two lines and nothing is recorded. A failed
  prefix stops the chain, and a failed call raises PostToolUseFailure.
- **Also accept a heredoc body, `--body "$(cat <<'EOF' … EOF)"`.** Rejected.
  A command substitution can write to the parent shell's stdout, and telling
  a harmless `cat` heredoc apart from anything else is hard to parse safely.
  Bodies go in a file, by `--body-file`.
- **Ask GitHub who authored the PR.** Rejected. A network call and a
  credential inside a hook.
- **A host allowlist.** Rejected. It breaks GitHub Enterprise, which the app
  supports.
- **Trust the host the printed URL names.** Rejected after review. A session
  can log gh in to a server it runs and send the create there
  (`--hostname`, `-R host/…`); that server can answer with any PR's URL on
  any host. Binding the URL to the host the command named stops that. **Comparing URLs as raw strings.** Rejected. Case and a trailing
  `/` would deny the session's own PR.
- **Clean up in a SessionEnd hook.** Rejected. A crash or kill skips it, so
  an age limit is needed anyway. **No clean-up.** Rejected: unbounded growth.
  **Expiry by file mtime.** Rejected: one new entry would keep old ones
  alive. **One shared file.** Rejected: contention, and one corrupt file
  would break every session. **A timer to prune.** Rejected: new
  infrastructure.
- **Store under `hooks/`, `logs/`, `projects/` or the XDG state directory.**
  Rejected. `hooks/` links into the pinned base snapshot, which an install
  replaces and retention deletes (ADR 0003); `logs/` and `projects/` are
  writable by the session by design (ADR 0002); an XDG directory would need a second protected location.
- **Sign the list with a machine key.** Rejected. It adds key material to
  protect, and session IDs are not guessable.
- **Canonicalise in bash and jq.** Rejected. The jq branch, the python3
  branch and the recorder would be three copies that must agree, and
  `${x,,}` on bash 3 fails open.
- **Amend ADR 0027 in place.** Rejected. New state, a new hook and a new
  protected location need their own rejected options and trial (ADR 0027
  is not rewritten; it gains a Status line).

## Consequences

- An unattended desktop session gets Auto-fix, with no prompt from
  guard-mcp, on a PR it created in the recorded shape and passes by `url`,
  within 2 days of creating it.
- Any other `set_monitor` call that would switch Auto-fix is denied, and the
  session carries on. The operator can still switch Auto-fix on in the app.
- A create in any other shape, including a heredoc body or a `--jq` REST
  create, still creates the PR, but it is not recorded, and Auto-fix for it
  is denied. It cannot be recorded after the fact.
- A GitHub Enterprise create is recorded only when the command names the
  host (`-R HOST/OWNER/REPO`, or `gh api --hostname`); a bare `gh pr create`
  in an Enterprise checkout is not.
- The binding reads the command, not gh's configuration. gh can also pick
  its host from its own config or `GH_HOST`, which the recorder cannot see;
  a session that rewrites those, or the shell environment gh runs in, is
  outside what this record closes.
- "Auto-fix only on the session's own PRs" rests on two layers: this
  guard's list, and the app acting only on the PR in `url`. The second is
  app behaviour, read from 44.7.2. The trial checks it live, and an app
  update can change it.
- The recorder cannot see an exit status, so "only a successful create is
  recorded" rests on the CLI raising PostToolUseFailure for a failed Bash
  call (measured on 2.1.302, above). A CLI that returned an error exit as a
  normal result would let a `git push` prefix supply the one stdout line
  when the create fails; the trial checks it.
- A host without `python3` 3.11 or later denies `set_monitor`.
- The recorder reads a tool's input and output, which ADR 0010 says the
  evidence loop never does. This list is guard state, not evidence: no loop
  reads it, and it never leaves the machine.
- Hooks run as the same Unix user as the session, so guard-paths' text
  check is the only thing keeping the session out of the list. Its residual
  routes are those ADR 0002 lists; a session that takes one could as well
  rewrite guard-mcp itself, so the list adds no weaker link.
- This limits only Auto-fix. A session's `gh` and `git` still reach whatever
  its token reaches.
- The bootstrap self-probe records a synthetic create in a scratch config
  directory and checks that the same URL passes, while another PR number and
  another session are denied; a pass on either fails the install. The
  session-start liveness check (ADR 0004, as amended 2026-10-17) lists the
  recorder as a non-guard hook: a missing recorder reads `HOOKS MISSING`,
  not `GUARDS OFF`, because without it the guard denies, which fails closed.
  This was first written as "not covered"; it changed when that amendment
  began requiring every registered hook to be in the check's list.
- Both CI steps that run the base branch's guard matrix against the pull
  request's hooks go red on the pull request that makes this change, because
  the base's matrix expects `set_monitor` to pass by name. Those steps are
  meant to go red on a legitimate change to the base's expectations, for a
  person to merge over.

## Trial, still open

In a desktop session in auto mode, on a base that carries this change:

1. `git push … && gh pr create --draft … --body-file <file>`, then
   `set_monitor` with `auto_fix: true` and that PR's `url`: passes, with no
   prompt, and Auto-fix turns on.
2. A PR bound from a link the session did not create: `set_monitor` with its
   `url` is denied, and the session carries on.
3. With that foreign PR still bound, `set_monitor` with the session's own
   `url`: Auto-fix turns on for the session's own PR, not the foreign one.
4. `set_monitor` with only `auto_archive_on_close`: passes.
5. A deny reaches the model as a tool error, with no prompt.
6. `git push … && gh pr create …` for a branch that already has a PR, so
   the create fails: nothing is recorded, and `set_monitor` for that PR is
   denied. This re-checks the 2.1.302 measurement on the CLI in use.
7. Not yet known, recorded here when seen: whether a subagent's PostToolUse
   carries its parent's `session_id`; whether a resumed session keeps its
   `session_id`; the exact non-TTY stdout of `gh pr create`; and which
   sessions the app classes as unattended.
