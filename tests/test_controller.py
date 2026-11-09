"""Controller rules: one inverter command from the plan step and live readings."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pilot.config import Options
from pilot.controller import Readings, decide, follow_amps, paced_amps
from pilot.planner import Step

TZ = ZoneInfo("Europe/Brussels")
T = datetime(2026, 1, 15, 1, 10, tzinfo=TZ).astimezone(timezone.utc)
O = Options()


def step(action, soc_end, start=T.replace(minute=0), hours=1.0):
    return Step(start, hours, action, 30, soc_end, 0, 0, 0, 0, 27, 5, 0)


def rd(soc=30, load=0.5, pv=0.0, imp=0.5, proj=0.5, v=51.2, t=T):
    return Readings(t, t.astimezone(TZ), soc, load, pv, imp, proj, v)


def test_shadow_mode_sends_nothing():
    assert decide(step("charge", 60), rd(), O, "self_use", "", 0, control_on=False).command == "none"
    assert decide(step("charge", 60), rd(), O, "unknown", "", 0, control_on=False).command == "none"


def test_control_off_while_charging_goes_back_to_self_use():
    assert decide(step("charge", 60), rd(), O, "charge", "01:00:00", 30, control_on=False).command == "self_use"


def test_follow_amps_keeps_import_at_the_limit():
    assert follow_amps(rd(load=0.5), O) == 30          # (2.3 - 0.5) kW / 51.2 V / 1.03 = 34 A -> 30 A
    assert follow_amps(rd(load=2.5), O) == 0
    assert follow_amps(rd(load=0.5, pv=1.0), O) == 50   # solar adds room, capped at the max current
    assert follow_amps(rd(load=0.5, v=0), O) == 30      # no voltage reading: the option is used


def test_paced_amps_spreads_charging_over_the_slot():
    s = step("charge", 40)                              # 10 points = 1.6 kWh in the 50 min left
    a = paced_amps(s, rd(soc=30), O, up=True)
    assert 35 <= a <= 40
    assert paced_amps(step("charge", 31), rd(soc=30), O, up=True) == 10   # never below 10 A


def test_start_charging_only_with_headroom():
    d = decide(step("charge", 60), rd(), O, "self_use", "", 0, True)
    assert d.command == "charge" and d.amps == 30
    d = decide(step("charge", 60), rd(load=1.8, imp=1.8, proj=1.8), O, "self_use", "", 0, True)
    assert d.command == "none" and "waiting" in d.reason


def test_pause_when_the_quarter_heads_over_the_limit():
    d = decide(step("charge", 60), rd(load=2.6, imp=2.9, proj=2.6), O, "charge", "01:00:00", 30, True)
    assert d.command == "self_use" and "house load too high" in d.reason


def test_follow_load_changes_the_current_only_on_a_real_change():
    assert decide(step("charge", 60), rd(load=0.5), O, "charge", "01:00:00", 30, True).command == "none"
    d = decide(step("charge", 60), rd(load=1.2, imp=1.6, proj=1.6), O, "charge", "01:00:00", 30, True)
    assert d.command == "current" and d.amps == 20      # (2.3 - 1.2) kW / 51.2 V / 1.03 = 20.9 A


def test_hourly_slot_is_renewed():
    t = T.replace(minute=0) + timedelta(hours=1, minutes=1)
    d = decide(step("charge", 60, start=t.replace(minute=0)), rd(t=t), O, "charge", "01:00:00", 30, True)
    assert d.command == "charge"


def test_bug1_target_reached_means_self_use_never_more_charge():
    d = decide(step("charge", 60), rd(soc=60.2), O, "charge", "01:00:00", 30, True)
    assert d.command == "self_use" and d.reason == "target reached"
    assert decide(step("charge", 60), rd(soc=70), O, "self_use", "", 0, True).command == "none"


def test_discharge_respects_the_reserve():
    d = decide(step("discharge", 20), rd(soc=60), O, "self_use", "", 0, True)
    assert d.command == "discharge" and "35" in d.reason            # the reserve, not the step's 20 %
    assert decide(step("discharge", 20), rd(soc=35), O, "discharge", "01:00:00", 90, True).command == "self_use"


def test_no_plan_means_self_use():
    assert decide(None, rd(), O, "charge", "01:00:00", 30, True).command == "self_use"
    assert decide(None, rd(), O, "self_use", "", 0, True).command == "none"
