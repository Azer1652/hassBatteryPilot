"""Start a release: set the version and open its CHANGELOG entry.

    python tools/release.py 2.12.0             # edits config.yaml and CHANGELOG.md
    python tools/release.py --dry-run 2.12.0   # shows what it would do

It sets `version` in battery_pilot/config.yaml and inserts a `## X.Y.Z` heading with an empty
bullet at the top of battery_pilot/CHANGELOG.md. Write the entry, run `python tools/check.py`,
then commit. It never commits or pushes. A version that is not higher than the current one is
refused. See docs/agents/recipes.md, "Release".
"""
from __future__ import annotations

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "battery_pilot", "config.yaml")
CHANGELOG = os.path.join(ROOT, "battery_pilot", "CHANGELOG.md")
VERSION_LINE = re.compile(r'^version: "(\d+)\.(\d+)\.(\d+)"$', re.M)


def current(config_text: str) -> tuple[int, int, int]:
    m = VERSION_LINE.search(config_text)
    if not m:
        raise SystemExit("config.yaml has no version line like: version: \"1.2.3\"")
    return tuple(int(x) for x in m.groups())


def plan(new: str, config_text: str, changelog_text: str) -> tuple[str, str]:
    """The two files with the new version; raises SystemExit when the version is not usable."""
    if not re.fullmatch(r"\d+\.\d+\.\d+", new):
        raise SystemExit(f"{new!r} is not a version like 2.12.0")
    old = current(config_text)
    if tuple(int(x) for x in new.split(".")) <= old:
        raise SystemExit(f"{new} is not higher than the current version {'.'.join(map(str, old))}")
    if f"## {new}\n" in changelog_text:
        raise SystemExit(f"CHANGELOG.md already has an entry for {new}")
    head, sep, rest = changelog_text.partition("\n## ")
    if not sep:
        raise SystemExit("CHANGELOG.md has no '## version' entries to put the new one above")
    return (VERSION_LINE.sub(f'version: "{new}"', config_text, count=1),
            f"{head}\n## {new}\n\n- \n\n## {rest}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("version")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    with open(CONFIG, encoding="utf-8") as f:
        config_text = f.read()
    with open(CHANGELOG, encoding="utf-8") as f:
        changelog_text = f.read()
    new_config, new_changelog = plan(a.version, config_text, changelog_text)
    was = ".".join(map(str, current(config_text)))
    if a.dry_run:
        print(f"Would change the version {was} -> {a.version} in battery_pilot/config.yaml")
        print(f"Would insert '## {a.version}' at the top of battery_pilot/CHANGELOG.md. Nothing written.")
        return 0
    with open(CONFIG, "w", encoding="utf-8") as f:
        f.write(new_config)
    with open(CHANGELOG, "w", encoding="utf-8") as f:
        f.write(new_changelog)
    print(f"Version {was} -> {a.version}. Now write the entry under '## {a.version}' in battery_pilot/CHANGELOG.md,")
    print("update DOCS.md if users see the change, and run: python tools/check.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
