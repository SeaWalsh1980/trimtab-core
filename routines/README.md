# Routines

Scheduled cloud sessions that run Trimtab's loops unattended. This directory
holds what the base can say about them; the deployment values (repositories,
environment, schedule changes) belong to the instance and the operator.

## What is here

- `templates/`: one routine per pair of files, `<name>.json.tmpl` (the routine
  body) and `<name>.prompt.md.tmpl` (its prompt).
  - `upstream-retro`: the config loop, run on the instance
    (`/trimtab-upstream-retro`).
  - `conformance` and `retro`: per project, bound to one consuming repository
    (`/trimtab-conformance`, `/trimtab-retro`).
- `environment-setup.sh`: the setup script for the cloud environment the
  routines run in. It is pasted into the environment's settings, and runs the
  instance's bootstrap before each session starts, so the session is guarded.

## Rendering

```bash
trimtab routine render <name> --instance <instance checkout> --set environment_id=<id> [--set project_repo=<owner/repo>]
```

It prints the routine body as JSON and writes nothing. The instance's
repository comes from the instance; a per-project template also needs
`project_repo`, and a missing value is an error naming it. A render is created
disabled, with no connectors, and is found afterwards by its rendered name,
never by a stored ID: no routine, environment or session ID belongs in any
file.

Creating, updating, running or enabling a routine is the operator's decision,
one action at a time. After a create or an update, read the routine back and
compare it with the render before running it.

## What a rendered routine enforces

- **The guards are live before anything else.** The prompt has the session
  read the guard files and settings, find `trimtab` on PATH, and run one exact
  command that guard-paths must refuse, quoting it and the refusal; a refusal
  of any other command does not count. If any check fails, the run stops.
- **GitHub through repository-scoped REST only** (ADR 0019, narrowed by ADR
  0021). The commands the routines run use `gh api` on
  `repos/{owner}/{repo}/…` paths, never another path (`search/…`, `user`,
  `orgs/…`) or gh's GraphQL-backed porcelain: cloud sessions refuse both, and
  allow only the repositories the routine names as sources.
- **No reference to the run** (ADR 0022). Nothing a run writes carries a
  `Claude-Session:` trailer, a session link or a routine, environment or
  session ID; the prompt overrides the default attribution, `check-pr` refuses
  such a PR body, and the installed settings turn the session link off.
- **No remote-session MCP tools** (ADR 0020). The body refuses the session
  server's tools, so an unattended run neither waits on an approval nor takes
  in PR events after its work. On read-back, the routine's
  `session_request.config.disallowed_tools` must list them; if it is empty,
  do not enable the routine.

## After every pin move: change the setup script's text

The cloud environment caches the result of its setup script. It rebuilds only
when the script's text or the environment's allowed hosts change, or after
about seven days. So when the instance moves its base pin, the routines keep
running the old install, with the old guards, commands and CLI, for up to a
week, and nothing in a run says so.

After every pin move, in the environment's setup script (the copy in the
environment's settings, not this file), add or bump one comment line naming
the new pin:

```bash
# trimtab-core <short pin>
```

The comment changes nothing the script does. Changing its text is what makes
the environment run the script again, so the next routine run installs the
pin the instance now names. Check that run's guard report before relying on
it.
