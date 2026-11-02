# 0056. The instance file names the admin and agent logins

## Status

Accepted on merge. Narrows nothing in ADR 0044, which still decides where the
merge-authority job reads its logins.

## Context

An authorised plan counts only when the admin account named in instance
configuration merged it, and the check fails when the configuration names none.
`wrap-merged` also needs the agent's login from there: an open item PR is resumed
only when the agent or the admin authored it, and anything else is a stop. The
instance file had no field for either, and both its readers refuse an unknown
key, so no instance could name them. Every plan failed the check, and an
agent-authored item PR could never be resumed.

The file has two readers: `trimtab/instance.py` and the instance's copy of
`shim/bootstrap.sh`, which runs before the base exists. A test pins their
agreement.

## Decision

An optional `accounts` object with two keys, each a GitHub user's login: `admin`,
required when the object is present, and `agent`, optional.

- A file with no `accounts` still parses and reads as neither, so what needs an
  admin fails, as it does today.
- `accounts` present without a valid `admin`, or with an invalid `agent`, is
  refused, not read as none.
- The login pattern can't match an App's `[bot]` name, so an App can't be named.
  It allows an underscore, which managed users' logins carry.
- One login for both is refused, compared without case as GitHub does: it would
  make the agent's PRs read as the admin's.
- `trimtab instance --json` prints both, `null` for one the file doesn't name.
- `schema_version` stays 1: every file valid before stays valid.

## Options rejected

- **Required.** Files at older commits are read too (`at_commit`), and they would
  all stop parsing.
- **Top-level `admin` and `agent` keys.** The approver follows, and an object takes
  it without another top-level key.
- **The admin alone.** The owner chose both once the review showed that a run would
  stop at its first resume without the agent, and a later key needs another base
  move and lock bump.
- **The approver's login in this change.** It doesn't exist yet, and its `[bot]`
  form needs its own pattern. It's an additive key, and needs another base move.
- **Bumping `schema_version` to 2.** An old reader refuses the new key either way,
  and a bump would also fail files that don't use it.
- **The repository's Actions variables, as ADR 0044 chose for the merge-authority
  job.** ADR 0044 rejected logins in this file because they are each repository's
  configuration and shouldn't need a pin bump to change. That still holds for that
  job, which runs on the consumer's own runner with its variables. A plan check
  runs in a session that can't always read them, and the rule names instance
  configuration. An instance also serves its one operator, so its admin isn't per
  repository in the same way.

## Consequences

- **Two sources for one fact.** `TRIMTAB_ADMIN` on the consumer and `accounts.admin`
  in the instance can disagree. If they do, the merge gate and the
  plan check judge different accounts, and a stale `accounts.admin` keeps a former
  admin's merge authorising plans. Nothing compares them yet. Changing the admin
  now takes a variable change and an instance pin bump.
- **Order matters.** An older reader refuses a file with `accounts`. An older shim
  changes nothing, and an older base's `trimtab instance --json`, `--check` and
  `--repo`, `trimtab bump` and the scrub stop with an error. The drift check only
  warns that `base_sha` can't be checked. So the instance adds the key in the same
  change that moves its `base.sha` here and replaces its copy of the shim, and each
  machine re-runs bootstrap before the consumer's lock moves.
- **Still text only.** That a file naming no admin fails the plan check is
  `wrap-merged`'s instruction: nothing in code reads `null` yet. `check-merge-authority`
  still takes its logins from its caller.
- `wrap-merged`'s `plan:continue` check also names the approver, which still has no
  field, so a label the approver added can't be recognised from the file until it
  does.
