# 0039. guard-paths refuses the harness and enforces a write allowlist

## Status

Accepted on merge. Extends guard-paths (ADR 0002's control-plane check is
unchanged). This is a change to a security control; the operator authorised
it in chat as step 4a of the autonomous-plan-runs plan. It lands in a pull
request of its own.

## Context

An instance can now let an autonomous session run an operator-merged plan
item after item. Its authorisation check refuses to start a run unless a
write-allowlist guard is live for the repository, and the operator set
three constraints for it:

- an agent must not be able to change the rules, skills or controls that
  bind it, even for one session, and then put them back;
- protection is always on; no mode switches it on;
- shell writes cannot be confined by a path hook, so the merge gate
  (CODEOWNERS) has to carry what the hook cannot.

guard-paths already protects the live control plane under the config
directory, and it deliberately refuses every *reference* to a protected path
in a shell command rather than trying to tell reads from writes ("a path set
is enumerable; a verb set is not"). Nothing protected a repository's own
harness: its `.claude/` settings and rules, its instruction file, its
workflows and CODEOWNERS, or the authorised plans.

## Decision

guard-paths gains a harness layer that runs inside any git repository (the
nearest ancestor holding a `.git` file or directory, so a worktree is its
own repository), for every tool except Read:

1. **Always refused:** `.claude/` and everything under it except
   `.claude/worktrees/` (settings, hooks, the project's rules directory,
   agents, commands, skills, the Trimtab lock, the allowlist itself);
   `CLAUDE.md` and `CLAUDE.local.md` at any depth; `.github/`; `CODEOWNERS`
   at the root and under `docs/`; `.gitleaks.toml`, `.trivyignore` and
   `.bandit-allowlist.yml`; and the authorised plans (`authorised/` under
   the project's plans directory). A Bash command naming one of these paths
   is refused too, reads included, by the same token and fragment scans the
   control-plane check uses.
2. **Default-deny, where `.claude/write-allowlist` exists:** an Edit, Write,
   MultiEdit or NotebookEdit to a path the list does not match is refused.
   One glob per line, relative to the repository root; `#` comments, blank
   lines, a leading `./` and CRLF endings are ignored; a trailing `/` means
   everything under that directory; `**` is read as `*`, and `*` spans
   directories in the guard's matching, so `*.md` matches at any depth. The
   list cannot widen layer 1. An
   allowlist that exists and cannot be read refuses every write (fail
   closed). Bash is not confined by the list: any program can write
   anywhere, so shell writes outside it are left to CODEOWNERS at merge.

Three bypass shapes are closed in the same change: a write is judged at its
physical path as well as its spelt one (the deepest existing ancestor is
resolved with `cd -P`, and a link on the way that does not resolve blocks),
so a symlink inside an allowlisted directory cannot reach `.claude/`, and
a write whose target is itself a symlink is refused outright; layer 1
compares lower-cased, so `Claude.md` on a case-insensitive filesystem is
refused; a Bash word holding `*`, `?` or `[` (with or without `/` or `.`)
is expanded the way the shell would, with bash's own pathname expansion,
and refused when any match is a protected path, wherever the glob sits;
`nocaseglob` is always on for that expansion, and `dotglob` when the
command mentions `dotglob` or `GLOBIGNORE`; and a write to an existing file
with more than one hard link is refused, since a hard link to a protected
file is invisible to every path check.

A repository without an allowlist gets layer 1 only, so nothing breaks
before each repository adds its file (step 4b). The operator lifts both
layers for a session that develops the harness by exporting
`HOOK_ALLOW_HARNESS=1` before launching claude; the secrets and
control-plane checks stay on. An inline prefix on a Bash command never
reaches the hook, as for the other overrides.

Top-level `rules/`, `skills/`, `hooks/`, `agents/` and `commands/` are not
in layer 1: in an ordinary project they are application code (a React
`hooks/` directory, a CLI's `commands/`). In the base and instance
repositories, where they are the harness, they are protected by leaving them
out of that repository's allowlist and by CODEOWNERS.

## Options considered

| Option | Why it lost |
|---|---|
| Confine Bash writes too, by spotting write verbs (`>`, `tee`, `sed -i`, `cp`, `mv`, …) | Semantic detection the guard's design rejects; never complete, since any script or program writes |
| Refuse every write in a repository with no allowlist | Breaks work in every repository the moment the pin lands, before step 4b adds the files |
| An opt-in "autonomous mode" that turns the layer on | Protection that has to be switched on fails open (the operator's decision) |
| Lift it with `HOOK_ALLOW_PATHS` | Lifts the secrets and control-plane checks as well, for a session that only needs to edit rules |
| Exempt `.claude/worktrees/` from the whole layer | Autonomous runs work in worktrees too, so the allowlist would protect almost nothing; instead a worktree is its own repository and its harness is refused like any other |
| Find the repository root with `git rev-parse` | An exec on every tool call; the `.git` ancestor walk is builtins only, in keeping with the guard's dependency rules (ADR 0001) |
| Refuse top-level `rules/`, `skills/`, `hooks/` everywhere | Over-blocks ordinary application directories with those names |

## Consequences

- Every session, interactive or autonomous, now needs `HOOK_ALLOW_HARNESS=1`
  to edit a repository's `.claude/`, its instruction file, its workflows or
  CODEOWNERS. Developing Trimtab's own rules and skills takes that export
  once each repository has its allowlist.
- A shell command that merely mentions a refused path is refused: `cat
  .claude/settings.json`, `git diff .github/`, or a commit message naming
  `CLAUDE.md` while run in that repository. The Read tool still reads these
  files. This is the same trade the control-plane check makes.
- Shell writes outside an allowlist are not stopped in the session; CODEOWNERS
  at merge is the backstop. That residual is accepted, not hidden.
- The base and instance repositories need allowlists that leave out their
  top-level harness directories (step 4b), or those directories have only
  CODEOWNERS protecting them.
- A path built at run time in a shell (`p=.cl; cat ${p}aude/x`) is out of
  reach of any path guard; CODEOWNERS at merge is the backstop there too.
- A Bash glob is refused when it expands to a protected file that exists:
  `cat *.md` or `git add *` in a directory holding `CLAUDE.md`. A glob that
  matches nothing names nothing (the shell keeps it literal) and passes.
- The harness layer's own glob expansion is bounded: a word with more than
  three wildcard path components (`/*/*/*/*`), or one that expands to more
  than 1,000 matches, is refused as too broad to check (fail closed). The
  bound matters because the hook's registered timeout is not a safe backstop.
  Claude Code's hooks reference (code.claude.com/docs/en/hooks) documents
  that a PreToolUse command hook killed at its timeout does not block the
  call unless the handler sets `onFailure: "block"`, which
  `settings.base.json` does not, so a slow expansion would let the command
  through. The harness check runs inside the whole-command scan, before the
  guard's older token loop, and a glob over the cap exits there; only words
  within the cap reach the token loop, which has always let bash expand each
  unquoted word as it splits the command.
- Measurement behind the part limit (2026-10-25, a Debian workstation,
  bash 5.2, in-process expansion with `nocaseglob` and `nullglob`):

  | Glob | Wildcard parts | Matches | Time |
  |---|---|---|---|
  | `/*/*` | 2 | 3,412 | 9 ms |
  | `/*/*/*` | 3 | 26,678 | 51 ms |
  | `/usr/*/*/*` | 3 | 14,360 | 41 ms |
  | `~/*/*/*` | 3 | 121 | 2 ms |

  The worst realistic three-part glob costs about 50 ms against a 10 s
  timeout, and both whole-filesystem globs above already exceed the match
  cap. Cost follows directory size, not the number of wildcard parts: only a
  tree of millions of entries, or a slow network mount, would approach the
  timeout, and a two-part glob over it would be as slow. A two-part limit
  was tried after review and reverted: it refused ordinary commands such as
  `ls tests/*/*/*.py` and bought no measurable time, and the base branch's
  guard matrix pins three parts as allowed.
- A link where `.claude/write-allowlist` should be, dangling or not,
  refuses every write: the list must be a regular file inside the reviewed
  tree, or a link to a world-writable file elsewhere could widen it. It does
  not switch default-deny off.
- Accepted residual: a shell command that reaches a protected file through
  a hard link (`sed -i … src/hard.json`) is judged at its spelt path. The
  link-count check costs a `stat` per call and runs for Edit and Write only;
  running it for every path-shaped shell word would cost one per word.
  CODEOWNERS at merge is the backstop, and the matrix pins this as a
  residual case.
- Accepted residual: a shell write through a symlinked directory inside the
  repository (`sed -i … src/cfg/settings.json` with `src/cfg ->
  ../.claude`) is judged at its spelt path. Edit and Write resolve the
  physical path; resolving every path-shaped shell word the same way would
  cost a subshell per word. CODEOWNERS at merge is the backstop. The matrix
  pins the current behaviour as a residual case.
- Accepted residual: the guard turns on `dotglob` for its expansion only
  when the command text contains `dotglob` or `GLOBIGNORE`, so a spelling
  that hides the word (`shopt -s dot''glob`, or a variable) enables it in the
  shell alone. Always enabling it would refuse every `*` at a repository
  root, since `*` would then reach `.claude/`. CODEOWNERS at merge is the
  backstop; the matrix pins this as a residual case too.
- Each write call costs one more `cd -P` subshell to resolve its physical
  path. Bash paths are normalised once and shared between the control-plane
  and harness checks.
- Editing a file through a symlink is refused, even when the link points
  somewhere harmless; edit the real file. Editing a file with other hard
  links is refused the same way.
- The guard gains one external: `stat`, run once per write to an existing
  file inside a repository to read its link count (GNU spelling, then BSD).
  If neither answers, the write blocks (ADR 0001).
