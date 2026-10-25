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
refused; and a Bash fragment holding `*`, `?` or `[` is refused when the
pattern could expand to a protected path, including a glob in a directory
above the repository, where the root cannot be found from the literal path
and any tail of the pattern is tested instead.

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
- A Bash glob such as `*.md` is refused in a repository where it could
  expand to `CLAUDE.md`, which is noisier than the literal checks.
- Each write call costs one more `cd -P` subshell to resolve its physical
  path. Bash paths are normalised once and shared between the control-plane
  and harness checks.
- Editing a file through a symlink is refused, even when the link points
  somewhere harmless; edit the real file.
- A Bash glob in a directory outside any repository is refused when a tail
  of it could match a protected path (`/x/*/settings.json` matches
  `.claude/settings.json`), which over-blocks some unrelated globs.
