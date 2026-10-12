# 0019. GitHub through REST only

## Status

Narrowed by ADR 0021 (2026-10-12): the trial below found REST paths outside
`repos/` refused too, so every call is now repository-scoped, and the
`search/issues` decisions here are superseded. The rest stands, still
proposed.

Was: proposed. Becomes accepted once a guarded cloud routine runs `ingest` and
its command end to end on this base (see "Trial, still open" below).

## Context

Claude Code cloud sessions refuse GitHub's GraphQL API. A call answers HTTP
403 with a message pointing at the REST API (`gh api repos/…`). gh's porcelain
goes through GraphQL: `gh pr list|view|create|edit`, `gh issue list|create`
and `gh repo view` all fail there.

The base used the porcelain throughout. In the package, `ingest` and
`propose` listed PRs and issues with it, labelled PRs with `gh pr edit`,
opened issues with `gh issue create`, and `dependabot-block` read the PR with
`gh pr view`. In the commands, the retro, conformance, upstream-retro, bump
and review methods resolved the repository with `gh repo view`, searched PRs
with `gh pr list` and opened drafts with `gh pr create`.

It was found when the per-project routines first ran in a guarded cloud
environment. The retro routine could not run at all: `ingest --dry-run`
failed on its first call. The conformance routine finished only by
improvising REST calls in place of its command's porcelain, including the
one that opened its PR. A method that works only when the model improvises
around it is not a method.

Constraints: the watermark query (ADR 0005) and the bounded reads (ADR 0006)
keep their meaning; the dry-run and `--apply --confirm` gates are unchanged;
no new dependency.

## Decision

- **Every GitHub call the base makes or prescribes is `gh api` on a REST
  path.** In the package, one runner builds `gh api -X <method> <path>` and
  refuses a `graphql` path before gh runs; every `Gh*` class goes through it.
  In the commands, the repository and its default branch come from
  `repos/{owner}/{repo}`, PR searches from `search/issues`, drafts open with
  `POST repos/<r>/pulls` (`-F draft=true -F body=@<file>`), and reviews read
  `pulls/<n>` and its `/files`.
- **The watermark query keeps its qualifiers.** `search/issues` takes the same
  `repo:`, `is:pr is:merged`, `-label:` / `label:` and `merged:>=` terms, with
  `sort=created` and the same order. A search that reports
  `incomplete_results` is refused rather than read, since a partial answer
  would move the watermark wrongly.
- **List reads stay bounded.** Pages are 100 at most, and reading stops at the
  existing limits (`--limit`, `--history-limit`, 500 issues). A limit above
  GitHub's 1000-result search cap is refused before any call. An issue
  listing is used to rule out a duplicate, so one with more than 500 rows is
  an error, not a cut-off: a partial list would let a duplicate through.
  The old `gh issue list --limit 500` truncated silently.
- **`closed_since` is a day.** Both the `since` query and the `closed_at`
  comparison read it as `YYYY-MM-DD`; anything else is refused before a call.
- **REST's shapes are read where the porcelain's were.** Dependabot is
  `dependabot[bot]` with `user.type` `Bot` (ADR 0012 named gh's
  `app/dependabot`). A merged PR is told from a closed one by `merged_at`.
  `--workflow` names the workflow by its file name, as REST requires.
- **Tests enforce it.** Every public method of every `Gh*` class, driven
  through an in-memory stand-in for gh, issues only `gh api` and never
  `graphql`. A static test fails if a command, agent or template names a
  GraphQL-backed gh command.
- **The PR-body check follows.** `pr-body-check` reads only `gh pr
  create|edit`; recognising `gh api` writes to `pulls` is a separate change,
  because its hook script is registered as a control. Until it lands, a body
  sent through `gh api` is checked by the consumer's CI alone.

## Options considered

- **Keep the porcelain and fall back to REST when GraphQL is refused.**
  Rejected. Two paths for every call, one of them exercised only in the
  environment that can least afford a surprise, and a fallback is a silent
  degradation where a failure should be loud. The refusal is not
  transient, so there is nothing to retry.
- **`gh api graphql` with the same queries.** Rejected. It is the same
  endpoint, refused the same way.
- **REST in the routine prompts only, porcelain elsewhere.** Rejected. The
  routines run the shared commands and the CLI, and an interactive session
  runs the same ones; a split would leave the commands broken where the
  routines need them, and two dialects to keep in step.
- **List merged PRs from `repos/<r>/pulls?state=closed` and filter in
  memory**, avoiding the search API. Rejected. The pulls endpoint cannot
  filter by label or merge date, so the watermark query would read every
  closed PR, a read that grows without bound with the repository, to find the few it
  needs.

## Consequences

- The routines can run their methods in a guarded cloud session without the
  model improvising GitHub calls.
- `search/issues` has its own rate limit (30 requests a minute for an
  authenticated caller) and a 1000-result cap. One run makes at most a handful
  of searches, and the default limits are well below the cap.
- `dependabot-block --workflow` no longer accepts a workflow's display name.
  An instance that passed one gets a loud 404, not a wrong run.
- Accepted: GitHub's search index lags writes by seconds to minutes, which
  the porcelain's search also did; a PR merged moments before a run is picked
  up by the next one, as the watermark already allows.
- Accepted: the issues endpoint lists PRs too. They are dropped, but they
  count toward the 500-row bound, past which the listing is an error; a
  `harness-feedback` label is not put on PRs, so the bound is not reached
  that way in practice.
- Accepted: an instance with more than 500 open, or recently closed,
  `harness-feedback` issues cannot run `propose` or `upstream` until it
  closes some or the bound is raised. That is loud by design.

## Trial, still open

Whether a cloud session can reach `search/issues` is not yet observed: the
refusal message named `repos/…` paths, and search is REST but not under
`repos/`. The first guarded cloud routine run on this base settles it. If it
is refused, this record is revisited and the result recorded here; the
pulls-endpoint option above is the one to re-weigh.

## Trial result (2026-10-12)

Refused. The first guarded retro routine on this base got, on
`ingest --dry-run --ledger-from-labels`'s first call, HTTP 403: sessions are
bound to their configured repositories and must use repository-scoped
endpoints (`repos/{owner}/{repo}/...`). GraphQL's refusal held as recorded
above. The pulls-endpoint option was re-weighed, with a bound and a moving
floor that answer the reason it lost here, and taken: ADR 0021.
