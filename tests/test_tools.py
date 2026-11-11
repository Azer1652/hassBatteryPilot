"""The developer tools in tools/ and the agent docs: they must keep working and stay true."""
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import plan_probe  # noqa: E402
import release  # noqa: E402

AGENT_DOCS = ["CLAUDE.md"] + [os.path.join("docs", "agents", f) for f in
                              ("architecture.md", "recipes.md", "gotchas.md", "unverified.md", "workflow.md")]


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- plan_probe
def test_plan_probe_prints_a_plan(capsys):
    assert plan_probe.main(["--hours", "6", "--peak-shave"]) == 0
    out = capsys.readouterr().out
    assert "contract fixed" in out and out.count("self_use") + out.count("peak_shave") >= 6


def test_plan_probe_reads_an_exported_settings_file_and_a_price_pattern(tmp_path, capsys):
    f = tmp_path / "settings.json"
    f.write_text('{"app": "battery_pilot", "options": {"contract": "dynamic", "battery_min_soc": 20}}', encoding="utf-8")
    plan_probe.main(["--settings", str(f), "--at", "2026-06-14T17:00", "--soc", "90", "--solar", "30",
                     "--prices", "base=80,19-21=300", "--hours", "8"])
    out = capsys.readouterr().out
    assert "contract dynamic" in out and "min 20 %" in out
    assert re.search(r"19:00 .* discharge", out) and not re.search(r"17:00 .* discharge", out)   # sells in the dear hours


def test_plan_probe_price_pattern_and_solar():
    assert plan_probe.parse_prices("base=80, 19-21=300, 2=20") == (80.0, [(19, 21, 300.0), (2, 3, 20.0)])
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    kwh = plan_probe.solar_fn(10.0, ZoneInfo("Europe/Brussels"))
    day = kwh(datetime(2026, 6, 14, 0, tzinfo=timezone.utc), datetime(2026, 6, 15, 0, tzinfo=timezone.utc))
    assert day == pytest.approx(10.0, abs=0.01)


# ---------------------------------------------------------------- release
CONFIG = 'name: x\nversion: "2.11.1"\nslug: x\n'
CHANGELOG = "# Changelog\n\n## 2.11.1\n\n- old\n"


def test_release_sets_the_version_and_opens_the_changelog_entry():
    config, changelog = release.plan("2.12.0", CONFIG, CHANGELOG)
    assert 'version: "2.12.0"' in config and "2.11.1" not in config
    assert changelog == "# Changelog\n\n## 2.12.0\n\n- \n\n## 2.11.1\n\n- old\n"


@pytest.mark.parametrize("version", ["2.11.1", "2.11.0", "1.99.99", "2.12", "v2.12.0"])
def test_release_refuses_a_version_that_is_not_higher_or_not_a_version(version):
    with pytest.raises(SystemExit):
        release.plan(version, CONFIG, CHANGELOG)


def test_release_dry_run_writes_nothing():
    before = read("battery_pilot/config.yaml"), read("battery_pilot/CHANGELOG.md")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "release.py"), "--dry-run", "99.0.0"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "Nothing written" in r.stdout
    assert (read("battery_pilot/config.yaml"), read("battery_pilot/CHANGELOG.md")) == before


# ---------------------------------------------------------------- the agent docs stay true
def test_the_agent_docs_exist_and_are_linked_from_claude_md():
    index = read("CLAUDE.md")
    for rel in AGENT_DOCS[1:]:
        assert os.path.isfile(os.path.join(ROOT, rel)), rel
        assert rel.replace(os.sep, "/") in index, f"CLAUDE.md does not point to {rel}"


def test_files_named_in_the_agent_docs_exist():
    """A path in backticks that looks like a repository file must be one (docs rot quietly otherwise)."""
    missing = []
    for rel in AGENT_DOCS:
        for path in re.findall(r"`((?:battery_pilot|tests|tools|docs|\.github|\.claude)/[\w./-]+\.\w+)`", read(rel)):
            if not os.path.exists(os.path.join(ROOT, path)):
                missing.append(f"{rel}: {path}")
    assert not missing, missing


def test_claude_md_stays_short():
    assert len(read("CLAUDE.md").splitlines()) <= 120      # the detail belongs in docs/agents/
