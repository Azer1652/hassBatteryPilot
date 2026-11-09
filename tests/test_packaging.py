"""The add-on files agree with each other and with the code."""
import os
import re
from dataclasses import fields

import yaml

from pilot.config import OVERRIDE_OPTION, Options, from_dict

ROOT = os.path.join(os.path.dirname(__file__), "..")
APP = os.path.join(ROOT, "battery_pilot")


def load(*p):
    with open(os.path.join(APP, *p), encoding="utf-8") as f:
        return yaml.safe_load(f)


CFG = load("config.yaml")
TR = load("translations", "en.yaml")["configuration"]


def test_every_option_has_a_schema_and_a_translation():
    assert set(CFG["options"]) <= set(CFG["schema"])
    assert set(CFG["schema"]) == set(TR)
    for k, v in TR.items():
        assert v.get("name") and v.get("description"), k


def test_schema_keys_are_known_to_the_code():
    known = {f.name for f in fields(Options)} - {"entities"}
    overrides = set(OVERRIDE_OPTION.values())
    for k, typ in CFG["schema"].items():
        assert k in known or k in overrides, k
        if k in overrides:
            assert typ == "str?", k


def test_code_defaults_match_the_add_on_defaults():
    o = Options()
    for k, v in CFG["options"].items():
        assert getattr(o, k) == v, (k, getattr(o, k), v)
    assert from_dict(CFG["options"]) == o


def test_defaults_are_inside_the_schema_ranges():
    for k, v in CFG["options"].items():
        m = re.match(r"(float|int)\(([-\d.]+),([-\d.]+)\)", CFG["schema"][k])
        if m:
            assert float(m.group(2)) <= v <= float(m.group(3)), k
        m = re.match(r"list\((.*)\)", CFG["schema"][k])
        if m:
            assert v in m.group(1).split("|"), k
        m = re.match(r"match\((.*)\)$", CFG["schema"][k])
        if m:
            assert re.fullmatch(m.group(1), str(v)), k


def test_version_is_in_the_changelog_and_the_build():
    with open(os.path.join(APP, "CHANGELOG.md"), encoding="utf-8") as f:
        assert f"## {CFG['version']}" in f.read()
    with open(os.path.join(APP, "Dockerfile"), encoding="utf-8") as f:
        assert "COPY pilot" in f.read()
    build = load("build.yaml")
    assert set(build["build_from"]) == set(CFG["arch"])
    assert CFG["ingress"] and CFG["ingress_port"] == 8099


def test_repository_file():
    with open(os.path.join(ROOT, "repository.yaml"), encoding="utf-8") as f:
        repo = yaml.safe_load(f)
    assert repo["name"] == "Battery Pilot"
