"""Run a module of this package for a hook, isolated from what the session can write.

    python3 -I [-S] -B run_module.py <package root> <module> [args...]

The hook starts the interpreter with -I (no working directory or script
directory on sys.path, no user site-packages, no PYTHON* variables) and, where
it needs nothing from site-packages, -S. This loader then puts the package
root on sys.path after the standard library and before any site-packages, and
runs the module as __main__ with the remaining arguments. So a standard-library
name at the root (json.py) cannot shadow the standard library, a trimtab
package installed in site-packages cannot shadow the checkout's, and nothing at
the root is on sys.path while site runs (ADR 0035). Standard library only.
"""

import runpy
import sys


def _first_site_index(path):
    """Index of the first site-packages entry, or len(path) when there is none."""
    for i, entry in enumerate(path):
        if entry.rstrip("/").endswith(("site-packages", "dist-packages")):
            return i
    return len(path)


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: run_module.py <package root> <module> [args...]")
    root, module = sys.argv[1], sys.argv[2]
    sys.path.insert(_first_site_index(sys.path), root)
    sys.argv = [module] + sys.argv[3:]
    runpy.run_module(module, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
