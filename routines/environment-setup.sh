#!/usr/bin/env bash
# Setup script for the cloud environment Trimtab's routines run in.
#
# Paste this into the environment's setup script (claude.ai, environment
# settings). It runs before the routine's Claude Code session starts, which is
# the only point at which a bootstrap guards that session: a bootstrap run from
# the prompt guards later sessions only.
#
# It installs the normal sandboxed bootstrap from the routine's instance
# clone: every guard, the rules, and bin/trimtab, into ~/.claude. It fails
# loudly rather than leave a session unguarded without saying so.
#
# The first run showed the setup script runs after the sources are cloned,
# with the clone beside its working directory rather than under $HOME. Still
# unverified: whether writing ~/.claude replaces settings the environment
# itself relies on. The routine's prompt checks the guards are live before
# doing anything.
set -euo pipefail

# The instance clone is the one checked-out source holding bootstrap.sh and
# rules/, looked for under $HOME and beside the directory this script starts
# in. Exactly one: with two, the choice would be glob order's. A clone reached
# by both routes, or through a link, is one clone: each candidate is compared
# by its physical path.
start=$PWD
clone=""
for candidate in "$HOME"/*/ "$start"/*/; do
  candidate=${candidate%/}
  if [[ -x "$candidate/bootstrap.sh" && -d "$candidate/rules" ]]; then
    candidate=$(cd -P "$candidate" && pwd -P)
    if [[ -n "$clone" && "$clone" != "$candidate" ]]; then
      echo "trimtab setup: more than one instance clone ($clone, $candidate); refusing to guess" >&2
      exit 1
    fi
    clone="$candidate"
  fi
done
if [[ -z "$clone" ]]; then
  echo "trimtab setup: no instance clone (bootstrap.sh and rules/) under $HOME or $start; the routine would run unguarded" >&2
  exit 1
fi

cd "$clone"
./bootstrap.sh
./bootstrap.sh --check
