#!/usr/bin/env bash
# Redacting config inspector. NOT a hook — invoked on demand, never
# registered in settings.json.
#
#   env-names.sh <envfile>
#       Print each variable's NAME, value SHAPE, and LENGTH. Values are
#       never printed, in whole or in part.
#
#   env-names.sh <envfile> --compare <template>
#       Additionally report variables the template expects but the file
#       lacks (MISSING) and variables the template doesn't know (EXTRA).
#       Exits 1 if anything is MISSING, so it can gate a deploy check.
#
# This answers "does the app have the config it needs" without putting a
# credential in a transcript. guard-paths exempts a bare invocation of this
# script from its path policy for exactly that reason.
set -euo pipefail

exec python3 - "$@" <<'PY'
import re
import sys

USAGE = "usage: env-names.sh <envfile> [--compare <template>]"

def fail(msg, code=2):
    print(f"env-names: {msg}", file=sys.stderr)
    sys.exit(code)

args = sys.argv[1:]
compare = None
if "--compare" in args:
    i = args.index("--compare")
    if i + 1 >= len(args):
        fail(USAGE)
    compare = args[i + 1]
    del args[i:i + 2]
if len(args) != 1:
    fail(USAGE)
path = args[0]

NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

def parse_env(p):
    """Parse dotenv format: NAME=value, optional 'export ', # comments."""
    entries = {}
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        fail(f"cannot read {p}: {e.strerror}", 1)
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        if s.startswith("export "):
            s = s[len("export "):]
        name, _, value = s.partition("=")
        name = name.strip()
        if not NAME_RE.fullmatch(name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        entries[name] = value
    return entries

def shape(v):
    """Classify a value's shape without revealing it."""
    if v == "":
        return "empty"
    if re.fullmatch(r"-?[0-9]+(\.[0-9]+)?", v):
        return "number"
    if v.lower() in ("true", "false", "yes", "no", "on", "off"):
        return "bool"
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", v):
        return "uuid"
    if re.fullmatch(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", v):
        return "jwt-shaped"
    if re.fullmatch(r"1000\.[0-9a-f]{32}\.[0-9a-f]{32}", v):
        return "zoho-token-shaped"
    if re.match(r"shp(at|ss|ca|pa)_", v):
        return "shopify-token-shaped"
    if re.match(r"[a-z][a-z0-9+.-]*://", v):
        if re.match(r"[a-z][a-z0-9+.-]*://[^:/@\s]+:[^@\s]+@", v):
            return "url-with-password"
        return "url"
    if "@" in v and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
        return "email-shaped"
    if re.fullmatch(r"[0-9a-fA-F]+", v) and len(v) >= 16:
        return "hex"
    if re.fullmatch(r"[A-Za-z0-9+/=_-]+", v) and len(v) >= 16:
        return "opaque(base64ish)"
    if v.startswith(("/", "./", "~")):
        return "path"
    return "word" if " " not in v else "text"

env = parse_env(path)

print(f"{path}: {len(env)} variable(s)")
print(f"{'NAME':<40} {'SHAPE':<22} LEN")
for name in sorted(env):
    print(f"{name:<40} {shape(env[name]):<22} {len(env[name])}")

if compare is not None:
    tmpl = parse_env(compare)
    missing = sorted(set(tmpl) - set(env))
    extra = sorted(set(env) - set(tmpl))
    print()
    print(f"compared against {compare}: {len(tmpl)} expected variable(s)")
    if missing:
        print(f"MISSING ({len(missing)}) — expected by the template, absent here:")
        for name in missing:
            print(f"  {name}")
    else:
        print("MISSING: none — every template variable is present")
    if extra:
        print(f"EXTRA ({len(extra)}) — present here, unknown to the template:")
        for name in extra:
            print(f"  {name}")
    if missing:
        sys.exit(1)
PY
