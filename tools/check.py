"""One command for "is the repository good": lint and tests.

    python tools/check.py           # ruff + the whole test suite (about 100 s)
    python tools/check.py --fast    # ruff + the tests not marked slow (about 25 s)

The release consistency (config.yaml version = top CHANGELOG entry; options, schema,
translations and DOCS.md agree) is part of the test suite: tests/test_packaging.py.
Exit code 0 only when everything passed.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(title: str, cmd: list[str]) -> bool:
    print(f"== {title}: {' '.join(cmd)}", flush=True)
    t0 = time.monotonic()
    ok = subprocess.run(cmd, cwd=ROOT).returncode == 0
    print(f"== {title}: {'ok' if ok else 'FAILED'} in {time.monotonic() - t0:.0f} s\n", flush=True)
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fast", action="store_true", help="skip the tests marked slow")
    a = ap.parse_args(argv)
    tests = [sys.executable, "-m", "pytest", "-q"] + (["-m", "not slow"] if a.fast else [])
    results = [run("lint", [sys.executable, "-m", "ruff", "check", "."]), run("tests", tests)]
    if all(results):
        print("All good." + (" (fast run: run without --fast before a release)" if a.fast else ""))
        return 0
    print("Something failed; see above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
