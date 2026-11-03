# 0059. A plan run checks its authorisation through the CLI

## Status

Accepted on merge. Changes how `wrap-merged` runs its authorisation checks 1-3. The checks
themselves don't change, and neither do ADR 0039's guard or ADR 0056's admin check.

## Context

`wrap-merged` section 2 told a plan run to type checks 1-3 as shell commands. Those were
`git ls-tree`, `git log --diff-filter=A` and `git diff --quiet`, and each named the plan and its
declaration under `<plans>/authorised/`. guard-paths' layer 1 (ADR 0039) refuses any Bash command
that names that folder, reads included, for every tool but Read. A plan run has no override.

So no plan run could pass check 1. That was seen on a consumer: the first authorised plan's run
(`TRIMTAB_PLAN_RUN=1`, on a clone at the plan's merge) was refused at check 1, stopped, and
labelled its plan PR `plan:blocked`. That plan had been written in an interactive session the
operator launched with `HOOK_ALLOW_HARNESS=1`. A direct run of the guard at this base, with the
override off, refuses that write too. So the guard has no fail-open here. The skill was the
defect.

## Decision

`trimtab check-plan-authorised --repository OWNER/NAME --plan NAME [--print-writes] [--repo DIR]`
runs checks 1-3 in `trimtab/planauth.py`. `wrap-merged` calls it after `git fetch origin`. The
command line names no path under the folder, so layer 1 lets it run. The plan's text is read with
the Read tool, which layer 1 exempts.

| Case | Exit |
|---|---|
| Checks 1-3 hold | 0. Prints the PR and merge commit, then with `--print-writes` the declaration verbatim |
| A check fails, or the instance names no admin or one `TRIMTAB_ADMIN` disagrees with | 1. Names each failure, `check 1:` to `check 3:`; an admin problem is unprefixed |
| A read fails, has the wrong shape or can't be parsed, the fetch is stale, the history is cut, the instance file or variable can't be read, or an argument is bad | 2 |

- **The same checks.** Check 1: both files are on `origin/<default>`. Check 2: for each file,
  `git log --first-parent -m --diff-filter=A` finds the commit that last added it. That commit
  must belong to exactly one PR merged into the default branch, and that PR's `merged_by.login`
  must match the admin without case. Both files must resolve to the same PR and the same
  `merge_commit_sha`. Check 3: neither file differs between that merge commit and the tip.
- **The admin** comes from `check-plan-admin`'s reading: `accounts.admin`, once it matches the
  repository's `TRIMTAB_ADMIN` (ADR 0056). If they disagree, the command exits 1 before reading
  anything else.
- **A stale fetch is an error.** `origin/<default>` must equal the commit the server reports for
  the branch. Otherwise a run fetched before an edit to the plan would pass check 3. A merge to
  the default branch between the fetch and the read does this too, so `wrap-merged` re-fetches
  and runs the command once more on exit 2, as it does for `check-plan-scope-job`.
- **Reads are memoised by adding commit** (ENG-6.2). Both files are usually added by one commit,
  so a check makes two `gh` reads for them, not four.
- **Stricter than the skill was.**
  - If two merged PRs claim the adding commit, that is a failure; the skill took the first entry
    it found.
  - Check 1 needs both entries to be regular files, so a link or a submodule there fails.
  - Check 2 needs the merge commit in the default branch's history, so a PR whose
    `merge_commit_sha` names a commit off it fails.
  - An answer of the wrong shape, or a full page of PRs (`PULLS_LIMIT`, ENG-6.2), is exit 2.
- **A cut history is exit 2.** In a shallow clone, or under a grafts file, git takes the oldest
  commit it holds for a root, so it reports every file that commit holds as added by it. Check 2
  would then judge whichever PR landed that commit, and check 3 would compare from its merge.
  An edit to the plan made before it, by a PR the admin never approved, would pass unseen.
  The skill, which typed the same `git log`, had this gap too.
- **The declaration is printed** so the scope gate reads `.writes` from the same commit check 3
  judged. Without that, the run would need a second read, which would name the folder.
- The git reads are planscope's hardened `_git`, the same one plan-scope uses: no steering
  variables, no replace objects, literal pathspecs.

## Options rejected

- **Let layer 1 pass read-only git verbs on the folder.** ADR 0039 rejected telling reads from
  writes: "a path set is enumerable; a verb set is not". A `git` command can write or run code
  (`--output`, `-c core.hooksPath`), and a list of the safe ones is the semantic detection SEC-4
  refuses as a sole defence.
- **Run the plan with `HOOK_ALLOW_HARNESS=1`.** That would lift layer 1 for the whole run. The run
  could then write the plan that authorises it, making an autonomous run wider than an
  interactive one, which AGW-2 forbids.
- **Read the files through `gh api` contents instead of git.** That reads the server's tip rather
  than the commit the run works from, adds a contents read per file, and still needs git or the
  API for the add history. The freshness check gives the same guarantee with one branch read.
- **A command per check.** Three commands mean three outputs for the session to combine. One
  command keeps the combining in tested code.
- **Leave the skill text and tell sessions to use the Read tool for check 1.** The Read tool
  reads the working tree, not `origin/<default>`, and can't do checks 2-3.

## Consequences

- **A plan can run again.** A stopped plan restarts as `wrap-merged` describes: the admin removes
  `plan:blocked`, and a new launched session runs section 2.
- **Still called by an instruction.** No hook makes a run call it, as with `check-plan-admin` and
  `check-plan-scope-job`. A run that skipped it would also skip its only route to the
  declaration's text.
- **The run needs the CLI on `PATH`.** `session-path.sh` already puts it there.
- **The owner's question stays open.** An agent opening a PR that adds an authorised plan, which
  only the admin merges, is still refused by layer 1 and AGW's always-refused list. That is a
  separate proposal to the instance, not this change.

## Trial

Every test was written before the module and seen failing on import. The tests use real git
in a temporary repository and a faked `gh`. They cover each check passing and failing, a delete and
re-add, an identical copy merged later from a side branch, a stale fetch, and each malformed
answer. A CLI test drives `bin/trimtab` against a fixture instance.

A mutation trial broke 27 lines of `planauth.py`, the three `planscope.py` helpers and the CLI
handler, one at a time. The first run left 8 green:

- Five unchecked answer shapes: a full page of PRs, a PR number as text, a merger of the wrong
  type, an abbreviated merge commit, and a branch answer with no commit.
- Dropping `--first-parent`.
- A redundant check that the merge commit exists, which the diff already fails on. It was removed.
- The `..` check on the repository. GhApi refuses a `..` segment itself, so this is a second fence,
  kept to match `mergeauthority`.

Each of the six that could be killed now has a test.

The in-session review then found the cut-history gap, the unchecked merge commit, and checks that
took a link for a file. Each fix has a test, and a second run added a mutation for each new guard:
31 of 32 caught, the one left the `..` fence. One path has no test: git output that can't be
parsed, which only a fake git could produce. It still exits 2.

Against the consumer, read-only, from an interactive session after `git fetch origin`:

- The plan that was stopped exits 0, naming its PR and the admin's merge commit. Its empty
  declaration prints nothing.
- A plan that doesn't exist exits 1 with two `check 1:` lines.

This branch's guard-paths, given each command as a plan run would type it
(`TRIMTAB_PLAN_RUN=1`, `HOOK_ALLOW_HARNESS=0`, in the consumer's checkout), allows the new
command and still refuses the old `git ls-tree` naming the folder. The restarted plan run is the
first real use from a plan session.
