# 0012. A command adds the harness block to a Dependabot PR

## Status

Accepted.

## Context

An instance whose CI runs `trimtab check-pr` fails every PR whose body lacks
the three-section harness block. Dependabot writes its own PR body, and
`dependabot.yml` has no option to set or template it; the repository's
`pull_request_template.md` applies only to PRs a person opens. So every
Dependabot PR fails the check until someone adds the block by hand and
re-runs CI (ADR 0009 is why those PRs exist).

The instance that hit this first had already rejected the two cheap ways
out: exempting `dependabot[bot]` from `check-pr` in its CI (a change to a
security control, for a rare PR) and merging with the check failing (the
loop then has no evidence for that PR). The fix by hand worked, but it is
several error-prone steps: copy the block, edit the body, re-run the right
run.

The constraints:

- Writing to GitHub is a side effect: it needs an allowlist, a dry-run
  preview, an explicit confirmation and an abort when the plan is stale.
- The PR body is untrusted, and Dependabot's part of it carries
  third-party release notes.
- No new infrastructure unless it is needed.

## Decision

- **Add `trimtab dependabot-block PR --repo R --workflow W`.** It refuses
  any PR that is not open or was not opened by Dependabot (gh's login
  `app/dependabot`, with the bot flag set). It appends a fixed block that
  cites `id: none` after Dependabot's text, which it keeps byte for byte,
  and checks the result with `check_body` before writing. It then re-runs
  the failed jobs of the newest run of the named workflow at the PR's head.
- **Dry run by default; `--apply --confirm <token>` to write**, the same
  gate as `ingest`, `propose` and `bump`. The token is a digest of exactly
  what would be written and re-run, so it goes stale when the body, the head
  or the runs change, and `--apply` then aborts with nothing written.
- **Idempotent.** A body that already holds a valid block is not written
  again, but a failed newest run is still offered for re-run, so a run
  interrupted after the write can be finished. A body holding a partial or
  invalid block is refused, not repaired.
- **The preview shows Dependabot's text only by length and sha256 digest.**
  The block it appends is constant text and is shown in full.
- **`--workflow` is required.** The base names no instance's workflow.
- **"CI needs a look" has its own exit code, 4.** When the head has no run
  of the workflow, or its newest run ended neither passed nor failed
  (cancelled, timed out), the block alone does not turn CI green and
  nothing is re-run. Both the dry run and `--apply` say so on stderr and
  exit 4, so the outcome is not mistaken for success (0) or for a refusal
  (1), after which a valid token would be thrown away. The other codes: 2
  for a GitHub or usage error, 3 for a plan gone stale since the dry run.
- **The body is written through the REST endpoint**, with the JSON payload
  on stdin, not through `gh pr edit`.

## Options considered

- **An Actions job that adds the block when Dependabot opens a PR.**
  Rejected. A `pull_request` run triggered by Dependabot gets a read-only
  token, so the job needs `pull_request_target` or a `workflow_run` chain,
  both of which run with write tokens on PR-triggered events. It is also a
  new workflow, so a security-control change in every instance, and since
  the body edit does not re-trigger CI it would need to re-run the check
  too. New infrastructure, more privilege, for a weekly PR.
- **A `--yes` flag.** Rejected. It lets the write happen without anyone
  seeing the preview and without the staleness check.
- **An interactive `y/n` prompt.** Rejected. A session's shell has no
  terminal, so it hangs or reads end-of-file, and a bare "y" is not tied to
  the preview it answers.
- **Writing the body with `gh pr edit --body-file -`.** Tried by hand and
  reverted: on a repository where GitHub has sunset classic projects,
  `gh pr edit` fails on its project-cards query before it edits anything.
  The REST `PATCH` does not touch projects.
- **Re-running every failed run at the head.** Rejected. It could re-run
  workflows the operator did not name.

## Consequences

- A Dependabot PR is fixed with one dry run and one apply instead of a
  hand edit and a re-run.
- The re-run fixes the check only where the instance's CI reads the PR
  body from the API when the job runs. A GitHub re-run reuses the original
  event, so a check that reads the body from the event payload fails again
  on the old body; such an instance has to push a commit, or close and
  reopen the PR, instead.
- Accepted: Dependabot rewrites the body when it updates a PR (a newer
  version, a rebase), which removes the block. The command is run again;
  it is safe to.
- Accepted: the block says `id: none`. It records that the PR was an
  automated bump, not which items governed it; the loop learns nothing
  from it beyond that.
- Accepted: the operator still reviews and merges each Dependabot PR.
  The command adds evidence; it does not approve anything.
