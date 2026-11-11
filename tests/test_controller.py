"""Controller rules: one inverter command from the plan step and live readings."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from pilot.config import Options
from pilot.controller import Readings, decide, follow_amps, paced_amps
from pilot.planner import Plan, Step, step_to_follow
from pilot.simulate import Battery

TZ = ZoneInfo("Europe/Brussels")
T = datetime(2026, 1, 15, 1, 10, tzinfo=TZ).astimezone(timezone.utc)
OPTS = Options()


def step(action, soc_end, start=T.replace(minute=0), hours=1.0):
    return Step(start, hours, action, 30, soc_end, 0, 0, 0, 0, 27, 5, 0)


def rd(soc=30, load=0.5, pv=0.0, imp=0.5, proj=0.5, v=51.2, t=T):
    return Readings(t, t.astimezone(TZ), soc, load, pv, imp, proj, v)


def test_shadow_mode_sends_nothing():
    assert decide(step("charge", 60), rd(), OPTS, "self_use", "", 0, control_on=False).command == "none"
    assert decide(step("charge", 60), rd(), OPTS, "unknown", "", 0, control_on=False).command == "none"


def test_control_off_while_charging_goes_back_to_self_use():
    assert decide(step("charge", 60), rd(), OPTS, "charge", "01:00:00", 30, control_on=False).command == "self_use"


def test_follow_amps_keeps_import_at_the_limit():
    assert follow_amps(rd(load=0.5), OPTS) == 30          # (2.3 - 0.5) kW / 51.2 V / 1.03 = 34 A -> 30 A
    assert follow_amps(rd(load=2.5), OPTS) == 0
    assert follow_amps(rd(load=0.5, pv=1.0), OPTS) == 50   # solar adds room, capped at the max current
    assert follow_amps(rd(load=0.5, v=0), OPTS) == 30      # no voltage reading: the option is used


def test_paced_amps_spreads_charging_over_the_slot():
    s = step("charge", 40)                              # 10 points = 1.6 kWh in the 50 min left
    a = paced_amps(s, rd(soc=30), OPTS, up=True)
    assert 35 <= a <= 40
    assert paced_amps(step("charge", 31), rd(soc=30), OPTS, up=True) == 10   # never below 10 A


def test_start_charging_only_with_headroom():
    d = decide(step("charge", 60), rd(), OPTS, "self_use", "", 0, True)
    assert d.command == "charge" and d.amps == 30
    d = decide(step("charge", 60), rd(load=1.8, imp=1.8, proj=1.8), OPTS, "self_use", "", 0, True)
    assert d.command == "none" and "waiting" in d.reason


def test_pause_when_the_quarter_heads_over_the_limit():
    d = decide(step("charge", 60), rd(load=2.6, imp=2.9, proj=2.6), OPTS, "charge", "01:00:00", 30, True)
    assert d.command == "self_use" and "house load too high" in d.reason


def test_follow_load_changes_the_current_only_on_a_real_change():
    assert decide(step("charge", 60), rd(load=0.5), OPTS, "charge", "01:00:00", 30, True).command == "none"
    d = decide(step("charge", 60), rd(load=1.2, imp=1.6, proj=1.6), OPTS, "charge", "01:00:00", 30, True)
    assert d.command == "current" and d.amps == 20      # (2.3 - 1.2) kW / 51.2 V / 1.03 = 20.9 A


def test_hourly_slot_is_renewed():
    t = T.replace(minute=0) + timedelta(hours=1, minutes=1)
    d = decide(step("charge", 60, start=t.replace(minute=0)), rd(t=t), OPTS, "charge", "01:00:00", 30, True)
    assert d.command == "charge"


def test_bug1_target_reached_means_self_use_never_more_charge():
    d = decide(step("charge", 60), rd(soc=62.1), OPTS, "charge", "01:00:00", 30, True)
    assert d.command == "self_use" and d.reason == "target reached"
    assert decide(step("charge", 60), rd(soc=70), OPTS, "self_use", "", 0, True).command == "none"


def test_target_reached_early_in_the_slot_holds_at_minimum_current():
    """So the battery keeps the energy until the slot ends (e.g. for the 18:00 cooking peak)."""
    d = decide(step("charge", 60), rd(soc=60.5), OPTS, "charge", "01:00:00", 30, True)
    assert d.command == "current" and d.amps == 10


def test_discharge_respects_the_reserve():
    d = decide(step("discharge", 20), rd(soc=60), OPTS, "self_use", "", 0, True)
    assert d.command == "discharge" and "35" in d.reason            # the reserve, not the step's 20 %
    assert decide(step("discharge", 20), rd(soc=35), OPTS, "discharge", "01:00:00", 90, True).command == "self_use"


def test_no_plan_means_self_use():
    assert decide(None, rd(), OPTS, "charge", "01:00:00", 30, True).command == "self_use"
    assert decide(None, rd(), OPTS, "self_use", "", 0, True).command == "none"


def test_a_paid_for_higher_peak_is_followed_instead_of_the_limit():
    """The plan decided a price pays for a higher capacity peak: charge up to that, pause above it."""
    boost = step("charge", 90)
    boost.limit_kw = 3.45
    d = decide(boost, rd(load=0.5), OPTS, "self_use", "", 0, True)
    assert d.command == "charge" and d.amps == 50              # (3.45 - 0.5) kW is more than the 50 A maximum
    assert follow_amps(rd(load=1.2), OPTS, 3.45) == 40         # (3.45 - 1.2) / 51.2 / 1.03 = 42.7 A
    d = decide(boost, rd(load=2.0, imp=3.0, proj=3.0), OPTS, "charge", "01:00:00", 50, True)
    assert d.command != "self_use"                             # 3.0 kW is fine under 3.45
    d = decide(boost, rd(load=3.3, imp=3.7, proj=3.7), OPTS, "charge", "01:00:00", 50, True)
    assert d.command == "self_use" and "house load too high" in d.reason


def test_an_extreme_price_step_discharges_below_the_reserve():
    s = step("discharge", 10)
    s.reserve = 10
    d = decide(s, rd(soc=30), OPTS, "self_use", "", 0, True)
    assert d.command == "discharge" and "10" in d.reason
    assert decide(step("discharge", 10), rd(soc=30), OPTS, "self_use", "", 0, True).command == "none"   # normally not


def test_a_rising_house_load_lowers_the_charge_current_instead_of_pausing():
    """Pausing and restarting is a full command sequence; one lower current is enough."""
    d = decide(step("charge", 60), rd(load=0.8, imp=2.45, proj=2.45), OPTS, "charge", "01:00:00", 30, True)
    assert d.command == "current" and d.amps == follow_amps(rd(load=0.8), OPTS) < 30
    d = decide(step("charge", 60), rd(load=0.8, imp=2.45, proj=2.45), OPTS, "charge", "01:00:00", 25, True)
    assert d.command == "self_use"                    # already at that current and still over: pause


def test_a_failed_charge_command_is_simply_sent_again():
    d = decide(step("charge", 60), rd(load=0.5, imp=2.3, proj=2.3), OPTS, "failed", "01:00:00", 30, True)
    assert d.command == "charge"                      # not a pause and restart


def test_an_export_follows_its_pacing_in_steps_of_ten_amps():
    s = step("discharge", 40)
    want = paced_amps(s, rd(soc=60), OPTS, up=False)
    d = decide(s, rd(soc=60), OPTS, "discharge", "01:00:00", want, True)
    assert d.command == "none" and d.reason == "discharging"
    d = decide(s, rd(soc=60), OPTS, "discharge", "01:00:00", want + 20, True)
    assert d.command == "current" and d.amps == want


# ---------------------------------------------------------------- the minutes before a slot boundary
def _plan_before_a_boundary(minutes, nxt="charge"):
    """A plan made `minutes` before 01:15: a short self-use step, then a quarter-hour of `nxt`."""
    start = T.replace(minute=15) - timedelta(minutes=minutes)
    short = Step(start, minutes / 60, "self_use", 30, 30, 0, 0, 0.45 * minutes / 60, 0, 14, 5, 0)
    after = Step(T.replace(minute=15), 0.25, nxt, 30, {"charge": 32, "discharge": 27}.get(nxt, 30), 0, 0, 0.11, 0, 15, 5, 0)
    return Plan([short, after], start, "dynamic"), start


def test_a_charge_is_not_stopped_for_the_minutes_before_a_slot_boundary():
    """The optimizer cannot fit one SOC point of charging in 3 minutes, so that slot is self-use. With
    a new plan every 5 minutes the charge was stopped and started again before every price boundary."""
    plan, t = _plan_before_a_boundary(3)
    now = rd(t=t + timedelta(minutes=1))
    assert decide(plan.step_at(t), now, OPTS, "charge", "01:00:00", 30, True).command == "self_use"   # the plan alone
    step_ = step_to_follow(plan, t, Battery.from_options(OPTS), "charge")
    assert step_.action == "charge" and step_.start == t and step_.soc_end == 32
    assert step_.hours == pytest.approx(3 / 60 + 0.25)                 # paced to the next step's target
    assert decide(step_, now, OPTS, "charge", "01:00:00", 30, True).command == "none"                 # charges on


def test_a_short_slot_starts_nothing_and_a_real_pause_is_kept():
    b = Battery.from_options(OPTS)
    plan, t = _plan_before_a_boundary(3)
    assert step_to_follow(plan, t, b, "self_use").action == "self_use"     # not charging now: nothing starts early
    assert step_to_follow(plan, t, b, "discharge").action == "self_use"    # the plan does not go on with an export
    plan, t = _plan_before_a_boundary(10)
    assert step_to_follow(plan, t, b, "charge").action == "self_use"       # 10 minutes can charge: a real pause
    plan, t = _plan_before_a_boundary(3, nxt="self_use")
    assert step_to_follow(plan, t, b, "charge").action == "self_use"       # the charge ends at the boundary
    plan, t = _plan_before_a_boundary(1, nxt="discharge")
    assert step_to_follow(plan, t, b, "discharge").action == "discharge"   # an export goes on for a minute
    plan, t = _plan_before_a_boundary(3, nxt="discharge")
    assert step_to_follow(plan, t, b, "discharge").action == "self_use"    # 3 minutes hold a point of export
    assert step_to_follow(None, t, b, "charge") is None


# ---------------------------------------------------------------- peak shaving
def test_a_peak_shaving_step_switches_the_inverter_to_peak_shaving_once():
    d = decide(step("peak_shave", 30), rd(), OPTS, "self_use", "", 0, control_on=True)
    assert d.command == "peak_shave"
    assert decide(step("peak_shave", 30), rd(), OPTS, "peak_shave", "", 0, control_on=True).command == "none"
    assert decide(step("peak_shave", 30), rd(), OPTS, "failed", "", 0, control_on=True).command == "peak_shave"
    assert decide(step("peak_shave", 30), rd(), OPTS, "charge", "01:00:00", 30, control_on=True).command == "peak_shave"


def test_peak_shaving_ends_with_the_step_and_with_control():
    assert decide(step("self_use", 30), rd(), OPTS, "peak_shave", "", 0, control_on=True).command == "self_use"
    assert decide(step("charge", 60), rd(), OPTS, "peak_shave", "", 0, control_on=True).command == "charge"
    assert decide(None, rd(), OPTS, "peak_shave", "", 0, control_on=True).command == "self_use"
    assert decide(step("peak_shave", 30), rd(), OPTS, "peak_shave", "", 0, control_on=False).command == "self_use"
    assert decide(step("peak_shave", 30), rd(), OPTS, "self_use", "", 0, control_on=False).command == "none"


# ---------------------------------------------------------------- solar hold
def test_solar_hold_starts_only_with_surplus_and_ends_without():
    sun, cloud = rd(load=0.5, pv=2.0, imp=0.0), rd(load=0.8, pv=0.3)
    st = step("solar_hold", 30)
    assert decide(st, sun, OPTS, "self_use", "", 0, control_on=True).command == "solar_hold"
    assert decide(st, cloud, OPTS, "self_use", "", 0, control_on=True).command == "none"
    assert decide(st, sun, OPTS, "solar_hold", "", 0, control_on=True).command == "none"
    assert decide(st, cloud, OPTS, "solar_hold", "", 0, control_on=True).command == "self_use"
    # a passing cloud right after switching: wait two minutes before switching again
    assert decide(st, cloud, OPTS, "solar_hold", "", 0, control_on=True, mode_age_s=30).command == "none"
    assert decide(st, sun, OPTS, "self_use", "", 0, control_on=True, mode_age_s=30).command == "none"
    assert decide(st, cloud, OPTS, "peak_shave", "", 0, control_on=True).command == "self_use"


def test_solar_hold_ends_with_the_step_and_with_control():
    sun = rd(load=0.5, pv=2.0, imp=0.0)
    assert decide(step("self_use", 30), sun, OPTS, "solar_hold", "", 0, control_on=True).command == "self_use"
    assert decide(None, sun, OPTS, "solar_hold", "", 0, control_on=True).command == "self_use"
    assert decide(step("solar_hold", 30), sun, OPTS, "solar_hold", "", 0, control_on=False).command == "self_use"


async def test_the_inverter_driver_turns_feed_in_priority_off_before_anything_else():
    from pilot.solis import Solis

    class HA:
        def __init__(self):
            self.calls = []

        async def call(self, domain, service, data):
            self.calls.append((service, data["entity_id"]))

    keys = ("tou_switch", "grid_charge_switch", "charge_current", "discharge_current", "charge_start",
            "charge_end", "discharge_start", "discharge_end", "peak_shaving_switch", "feed_in_priority_switch")
    ha = HA()
    s = Solis(ha, {k: k for k in keys})
    await s.solar_hold(T)
    assert s.mode == "solar_hold" and ha.calls[-1] == ("turn_on", "feed_in_priority_switch")
    assert ("turn_off", "tou_switch") in ha.calls and ("turn_off", "peak_shaving_switch") in ha.calls
    for command in (lambda: s.charge(T, 20), lambda: s.self_use(T), lambda: s.discharge(T, 20),
                    lambda: s.peak_shave(T, 2.3, 10)):
        await s.solar_hold(T)
        ha.calls.clear()
        await command()
        assert ("turn_off", "feed_in_priority_switch") in ha.calls and s.hold is False
    ha.calls.clear()
    await s.self_use(T)                                   # already off: not written again
    assert ("turn_off", "feed_in_priority_switch") not in ha.calls
