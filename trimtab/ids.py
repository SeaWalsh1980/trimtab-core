"""The shape of a routine, environment or session ID, which no file, PR, commit or log may hold.

Shared by the scrub (the base's own tree) and `check-pr` (what a run writes,
ADR 0022). Callers report where a shape is, never the matched text.
"""

from __future__ import annotations

import re

# A routine (trig_), environment (env_) or session (session_, cse_) ID: a
# random tail of 16 or more letters and digits that mixes upper and lower
# case. An option name in one case after a prefix (`env_` and a long
# upper-case name), as release notes quote them, does not match.
ID_SHAPE = re.compile(r"\b(?:trig|env|session|cse)_"
                      r"(?=[0-9A-Za-z]*[A-Z])(?=[0-9A-Za-z]*[a-z])"
                      r"[0-9A-Za-z]{16,}\b")


def id_shape_lines(text: str) -> list[int]:
    """The numbers of the lines holding an ID shape: never the IDs themselves."""
    return [n for n, line in enumerate(text.splitlines(), 1) if ID_SHAPE.search(line)]
