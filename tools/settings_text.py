"""Copy the Configuration tab's text into the image, for the page's settings form.

    python tools/settings_text.py          # writes battery_pilot/pilot/static/settings_text.json
    python tools/settings_text.py --check  # exit 1 when that file is not up to date

The image has no YAML parser, so the names and descriptions of battery_pilot/translations/en.yaml
go into a JSON file next to the page. Run it after changing en.yaml; tests/test_form.py fails
until you do.
"""
from __future__ import annotations

import json
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "battery_pilot", "translations", "en.yaml")
TARGET = os.path.join(ROOT, "battery_pilot", "pilot", "static", "settings_text.json")


def build() -> str:
    with open(SOURCE, encoding="utf-8") as f:
        conf = yaml.safe_load(f)["configuration"]
    text = {k: {"name": v.get("name", k), "description": v.get("description", "")} for k, v in conf.items()}
    return json.dumps(text, indent=1, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    want = build()
    try:
        with open(TARGET, encoding="utf-8") as f:
            have = f.read()
    except OSError:
        have = ""
    if "--check" in argv:
        if have != want:
            print("battery_pilot/pilot/static/settings_text.json is out of date: run python tools/settings_text.py")
            return 1
        return 0
    if have != want:
        with open(TARGET, "w", encoding="utf-8") as f:
            f.write(want)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
