"""`trimtab lint references`: every named agent, skill or command resolves.

A project's review once named two agents from a plugin that was never enabled;
both passes silently never ran. This finds such names before anything runs.

A reference is recognised only in forms that are unambiguous, so the check is
deterministic and quiet on prose:
- `subagent_type: <name>`;
- `Skill(<name>)`;
- a backticked name in a markdown table column headed Agent, Skill or Command;
- a backticked `/trimtab-*` command.

A name resolves if it is a built-in, is defined in the project or in Trimtab
(`agents/`, `skills/<name>/SKILL.md`, `commands/`, and their `.claude/`
equivalents), or is `<plugin>:<name>` for an enabled plugin. When a plugin
cache directory is given, the plugin's own agents, skills and commands must
also contain the name.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from trimtab.items import Problem

BUILTIN_AGENTS = {"general-purpose", "Explore", "Plan", "statusline-setup", "claude-code-guide", "claude"}
BUILTIN_COMMANDS = {
    "agents", "clear", "code-review", "compact", "config", "context", "doctor", "fewer-permission-prompts",
    "help", "hooks", "init", "loop", "mcp", "memory", "model", "permissions", "plugin", "review", "run",
    "schedule", "security-review", "simplify", "skill-doctor", "skills", "status", "update-config",
}

FENCE = re.compile(r"^\s*(```|~~~)")
SUBAGENT = re.compile(r"subagent_type[\"']?\s*[:=]\s*[\"']?([\w.:-]+)")
SKILL_CALL = re.compile(r"\bSkill\(\s*[\"']?([\w.:-]+)")
TRIMTAB_COMMAND = re.compile(r"`/(trimtab-[\w-]+)`")
TICKED = re.compile(r"`/?([\w.:-]+)`")
COLUMN_KINDS = {"agent": "agent", "agents": "agent", "skill": "skill", "skills": "skill",
                "command": "command", "commands": "command"}


def _scanned(root: Path) -> list[Path]:
    patterns = ["commands/**/*.md", "agents/**/*.md", "skills/*/SKILL.md",
                ".claude/commands/**/*.md", ".claude/agents/**/*.md", ".claude/skills/*/SKILL.md",
                "CLAUDE.md", "AGENTS.md", ".claude/CLAUDE.md", "rules/*.md", ".claude/rules/*.md"]
    found = {p for pattern in patterns for p in root.glob(pattern) if p.is_file()}
    return sorted(found)


def _defined(roots: list[Path]) -> dict[str, set[str]]:
    names = {"agent": set(BUILTIN_AGENTS), "skill": set(BUILTIN_COMMANDS), "command": set(BUILTIN_COMMANDS)}
    for root in roots:
        for d in ("agents", ".claude/agents"):
            names["agent"] |= {p.stem for p in (root / d).glob("**/*.md")}
        for d in ("skills", ".claude/skills"):
            names["skill"] |= {p.parent.name for p in (root / d).glob("*/SKILL.md")}
        for d in ("commands", ".claude/commands"):
            names["command"] |= {p.stem for p in (root / d).glob("**/*.md")}
    return names


def enabled_plugins(roots: list[Path]) -> set[str]:
    """Plugin names enabled in Trimtab's settings.base.json and the project's settings."""
    enabled: set[str] = set()
    for path in [r / "settings.base.json" for r in roots] + [r / ".claude" / "settings.json" for r in roots]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        plugins = data.get("enabledPlugins") or {}
        enabled |= {key.split("@", 1)[0] for key, on in plugins.items() if on is True}
    return enabled


def _plugin_names(cache: Path, plugin: str) -> set[str] | None:
    """Names a plugin defines, from the newest cached copy; None if it isn't cached."""
    copies = sorted(cache.glob(f"*/{plugin}/*"))
    if not copies:
        return None
    root = copies[-1]
    return ({p.stem for p in root.glob("agents/**/*.md")} | {p.stem for p in root.glob("commands/**/*.md")}
            | {p.parent.name for p in root.glob("skills/*/SKILL.md")})


def references(text: str) -> list[tuple[int, str, str]]:
    """(line number, kind, name) for every reference in the recognised forms."""
    out: list[tuple[int, str, str]] = []
    lines = text.splitlines()
    fenced, columns = False, {}
    for n, line in enumerate(lines, 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        out += [(n, "agent", m) for m in SUBAGENT.findall(line)]
        out += [(n, "skill", m) for m in SKILL_CALL.findall(line)]
        out += [(n, "command", m) for m in TRIMTAB_COMMAND.findall(line)]
        stripped = line.strip()
        if not stripped.startswith("|"):
            columns = {}
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not columns:
            nxt = lines[n].strip() if n < len(lines) else ""
            if re.match(r"^\|?\s*:?-{3,}", nxt):  # a header row
                columns = {i: COLUMN_KINDS[c.strip("`*_ ").lower()] for i, c in enumerate(cells)
                           if c.strip("`*_ ").lower() in COLUMN_KINDS}
                columns = columns or {-1: None}
            continue
        for i, kind in columns.items():
            if kind and i < len(cells):
                out += [(n, kind, m) for m in TICKED.findall(cells[i])]
    return out


def lint(project: Path, base_root: Path | None, enabled_plugins: set[str],
         plugin_cache: Path | None = None) -> list[Problem]:
    project = Path(project)
    roots = [project] + ([Path(base_root)] if base_root and Path(base_root).resolve() != project.resolve() else [])
    defined = _defined(roots)
    problems = []
    for path in _scanned(project):
        rel = path.relative_to(project).as_posix()
        for n, kind, name in references(path.read_text(encoding="utf-8")):
            if ":" in name:
                plugin, local = name.split(":", 1)
                ok = plugin in enabled_plugins
                if ok and plugin_cache is not None:
                    names = _plugin_names(plugin_cache, plugin)
                    ok = names is None or local in names
                why = f"plugin {plugin} is not enabled, or does not define it"
            else:
                ok = name in defined[kind] or (kind == "command" and name in defined["skill"])
                why = f"no {kind} of that name in the project, Trimtab or the built-ins"
            if not ok:
                problems.append(Problem("unresolved", f"{rel}:{n} {name}", why))
    return problems
