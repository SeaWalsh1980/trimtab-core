# 0056. The instance file names the admin and agent logins

## Status

Accepted on merge. Narrows nothing in ADR 0044, which still decides where the
merge-authority job reads its logins. Amended 2026-11-02: "The plan check reads this in
code", below. Amended again: "The approver's login", below.

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
  admin's merge authorising plans. Nothing compares them yet (see "The plan check
  reads this in code" below). Changing the admin
  now takes a variable change and an instance pin bump.
- **Order matters.** An older reader refuses a file with `accounts`. An older shim
  changes nothing, and an older base's `trimtab instance --json`, `--check` and
  `--repo`, `trimtab bump` and the scrub stop with an error. The drift check only
  warns that `base_sha` can't be checked. So the instance adds the key in the same
  change that moves its `base.sha` here and replaces its copy of the shim, and each
  machine re-runs bootstrap before the consumer's lock moves.
- **Still text only.** That a file naming no admin fails the plan check is
  `wrap-merged`'s instruction: nothing in code reads `null` yet (see "The plan check
  reads this in code" below). `check-merge-authority` still takes its logins from its
  caller.
- `wrap-merged`'s `plan:continue` check also names the approver, which still has no
  field, so a label the approver added can't be recognised from the file until it
  does.

## Amendment (2026-11-02): The plan check reads this in code

Added after the decision above, on the owner's yes to two questions: should code fail
the plan check when no admin is named, and should `accounts.admin` be compared with
`TRIMTAB_ADMIN`.

`trimtab check-plan-admin --repository OWNER/NAME` prints the login a plan's merge is
compared with, and `wrap-merged`'s check 2 calls it.

| Case | Exit | Stdout |
|---|---|---|
| `accounts.admin` matches the repository's `TRIMTAB_ADMIN`, without case | 0 | The login |
| The instance names no admin, or the two differ | 1 | Nothing |
| The instance file or the variable can't be read, the variable holds no login, or `--repository` isn't owner/name | 2 | Nothing |

So a repository with no variable fails the plan check. Space around the variable is
ignored, as `accounts()` ignores it for the merge gate.

### Options rejected

- **Only `wrap-merged`'s text.** That was the state above, and it's an instruction a
  session can misread.
- **Comparing in the merge-authority job.** That job has no instance file (ADR 0044).
- **Comparing in `trimtab instance --check`.** The instance's CI has no one repository to
  ask, since an instance serves several consumers.
- **Warning and passing when the variable can't be read.** The check would then pass in
  exactly the case it can't judge.
- **The variable alone.** AGW-1 names instance configuration, and an agent can't change
  either source, so two that must agree are a stronger test than one.

### Consequences

- **A session that can't read the variable can't pass the plan check.** The option
  rejected above said such sessions exist. That now stops a run rather than letting it
  go on the instance's word alone. The agent's token read it on a consumer, the base and
  the instance. A cloud session's read is untried.
- **It's still called by an instruction.** `wrap-merged` tells a session to run it, and
  no hook makes it. The session chooses the `--repository` and instance it names, so
  nothing ties them to the plan's. The comparison is of the moment the session runs it,
  and the login it prints only means something if the session compares `merged_by` with it.
- **The merge gate doesn't change.** It judges by the variable and never reads the
  instance, so an instance that disagrees stops plans, not merges.

### Trial

Each test that isn't a control was seen failing on its assertion against a stub that
reported no problem. Six controls pass either way. A mutation trial broke 28 lines one
at a time. Its first run left 2 green, a `..` inside a name and `--repository` made
optional; each now has a test case, and all 28 are caught.

Read-only against real repositories, with the agent's token: a consumer, the base and the
instance each exit 0 in under half a second, and a repository that doesn't exist exits 2.

## Amendment (2026-11-02): The approver's login

Added after the decision above, on the owner's decision that the instance file names the
approver's login once the App exists. The option rejected above, "The approver's login in
this change", said it was additive and needed its own pattern and a base move. This is that
change.

`accounts.approver` is optional, and it is an App's login: `<name>[bot]`, where the name is
up to 34 characters, GitHub's limit. A user's login is refused, as an App's is for `admin`
and `agent`. So the two patterns can't both match one login, and no collision check is
needed. `trimtab instance --json` prints it, `null` when the file names none, and
`wrap-merged` names it as the source. `schema_version` stays 1, and the order under
"Order matters" above applies again.

### Options rejected

- **The approver in the user login pattern.** The account that will hold the role is an App,
  and its login carries `[bot]`, which that pattern exists to refuse.
- **Either form.** Nothing plans a user as approver, and an agent's account could then be
  named by mistake and pass the file's check.
- **An empty string for "none".** `agent` refuses it, and an absent key already says it.
- **A new record.** This finishes a change the record above already deferred, with no new
  decision about where logins live.

### Consequences

- **Two sources again.** A consumer's merge-authority job reads its approver from outside
  this file, and nothing compares that with `accounts.approver`. If they differ, the merge
  gate and `wrap-merged` judge different accounts. `check-plan-admin` compares only the admin.
- **Still read by instruction.** `wrap-merged` tells a session to read the field, and no code
  yet refuses a plan on it. The `plan:continue` gap under "Consequences" above closes the
  same way: the file can name the login, and a session reads it.

### Trial

Each new test was seen failing against a stub that took the key and checked nothing, then
passing. The parity test holds the shim and the package to the same answer on each form.
