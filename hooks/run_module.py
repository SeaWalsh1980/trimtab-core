"""Run a module of this package for a hook, isolated from what the session can write.

    python3 -I [-S] -B run_module.py <package root> <trimtab.module> [args...]

The hook starts the interpreter with -I (no working directory or script
directory on sys.path, no user site-packages, no PYTHON* variables) and, where
it needs nothing from site-packages, -S. This loader then loads the trimtab
package straight from <package root>/trimtab and runs the module as __main__
with the remaining arguments. The root itself never goes on sys.path, so no
file at the root, whatever its name, can stand in for a module: only trimtab.*
comes from the checkout, and every other import resolves from the standard
library and site-packages as usual (ADR 0035). Standard library only.
"""

import importlib.util
import os
import runpy
import sys

PACKAGE = "trimtab"


def _load_package(root):
    """Import the package from <root>/trimtab, without putting <root> on sys.path."""
    pkg_dir = os.path.join(root, PACKAGE)
    spec = importlib.util.spec_from_file_location(
        PACKAGE, os.path.join(pkg_dir, "__init__.py"),
        submodule_search_locations=[pkg_dir])
    if spec is None or spec.loader is None:
        sys.exit(f"run_module.py: no {PACKAGE} package under {root}")
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = package
    spec.loader.exec_module(package)


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: run_module.py <package root> <trimtab.module> [args...]")
    root, module = sys.argv[1], sys.argv[2]
    if not module.startswith(PACKAGE + "."):
        sys.exit(f"run_module.py: runs only {PACKAGE} modules, not {module!r}")
    _load_package(root)
    sys.argv = [module] + sys.argv[3:]
    runpy.run_module(module, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
