"""Latency of hooks/pr-body-check.sh (measured before optimising; results in ADR 0005). Not a test: run by hand.

    python3 tests/trimtab/bench_pr_body_check.py [runs]

Times the whole process, as Claude Code would pay for it, on a Bash call that
does not mention `gh pr` (the fast path, which is almost every call) and on a
`gh pr create --body-file` with a valid body (the full check).
"""

import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "hooks" / "pr-body-check.sh"
BODY = Path(__file__).resolve().parent / "fixtures" / "pr-tst2-ambiguous.md"


def timed(payload: str, runs: int) -> list[float]:
    out = []
    for _ in range(runs):
        start = time.perf_counter()
        subprocess.run([str(HOOK)], input=payload, capture_output=True, text=True, check=False)
        out.append((time.perf_counter() - start) * 1000)
    return out


def summary(name: str, ms: list[float]) -> str:
    q = statistics.quantiles(ms, n=100)
    return f"{name:<34} n={len(ms)}  p50={q[49]:.1f} ms  p95={q[94]:.1f} ms  max={max(ms):.1f} ms"


def main() -> None:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    with tempfile.TemporaryDirectory() as cwd:
        Path(cwd, "body.md").write_text(BODY.read_text())
        miss = json.dumps({"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": "git status --short"}})
        hit = json.dumps({"tool_name": "Bash", "cwd": cwd,
                          "tool_input": {"command": "gh pr create --draft --title t --body-file body.md"}})
        timed(miss, 5)  # warm the page cache
        print(summary("non-matching (fast path)", timed(miss, runs)))
        print(summary("gh pr create --body-file (valid)", timed(hit, max(runs // 4, 20))))


if __name__ == "__main__":
    main()
