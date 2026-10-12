"""An in-memory stand-in for the gh CLI, a true external system (TST-2), for the `Gh*` classes' `run` seam."""

import subprocess
from urllib.parse import parse_qsl


class FakeGh:
    """Records each gh command line and its stdin, and answers canned stdout.

    `answers` maps a REST path, without its query, or a (method, path) pair,
    to stdout, or to a list of stdouts served one per call (pages). The pair
    wins over the bare path; a call with no answer gets `default`.
    """

    def __init__(self, answers=None, default="", returncode=0, stderr=""):
        self.answers = dict(answers or {})
        self.default = default
        self.returncode = returncode
        self.stderr = stderr
        self.calls = []

    def __call__(self, argv, stdin):
        self.calls.append((list(argv), stdin))
        path = path_of(argv)
        answer = self.answers.get((method_of(argv), path), self.answers.get(path, self.default))
        if isinstance(answer, list):
            answer = answer.pop(0) if answer else self.default
        return subprocess.CompletedProcess(argv, self.returncode, answer, self.stderr)

    def argvs(self):
        return [argv for argv, _ in self.calls]


def path_of(argv):
    """The REST path a `gh api -X <method> <path>` command line targets, without its query."""
    return argv[4].split("?", 1)[0] if len(argv) > 4 else ""


def query_of(argv):
    """The query a `gh api` command line sends, as a dict of strings."""
    target = argv[4] if len(argv) > 4 else ""
    return dict(parse_qsl(target.split("?", 1)[1])) if "?" in target else {}


def method_of(argv):
    return argv[3] if len(argv) > 3 and argv[2] == "-X" else ""
