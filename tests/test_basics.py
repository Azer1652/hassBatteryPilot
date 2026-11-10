"""Options, price parsing, contract formulas and entity discovery."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from pilot import config, discovery, prices, tariffs
from pilot.config import ConfigError, Options

BXL = ZoneInfo("Europe/Brussels")


# ---------------------------------------------------------------- options
def test_defaults_are_the_original_setup():
    o = Options()
    assert (o.import_limit_kw, o.fixed_day_rate, o.fixed_night_rate, o.grid_fees_c_kwh) == (2.3, 18.59, 15.22, 11.96)
    assert (o.capacity_tariff_eur_kw_year, o.capacity_min_kw, o.battery_capacity_kwh) == (52.37, 2.5, 16.0)
    assert o.max_charge_kw == pytest.approx(50 * 51.2 / 1000)
    assert o.charge_efficiency * o.discharge_efficiency == pytest.approx(0.9)


def test_from_dict_reads_entity_overrides_and_ignores_unknown_keys():
    o = config.from_dict({"import_limit_kw": 3.0, "battery_soc": " sensor.my_soc ", "pv_power": "",
                          "something_old": 1})
    assert o.import_limit_kw == 3.0
    assert o.entities == {"battery_soc": "sensor.my_soc"}


@pytest.mark.parametrize("raw", [
    {"contract": "cheap"},
    {"night_start": "7:00"},
    {"night_start": "22:00", "night_end": "22:00"},
    {"battery_min_soc": 40, "battery_reserve_soc": 30},
    {"fixed_contract_end": "31/03/2027"},
    {"winter_mode": "always"},
    {"slot_entities": "slot_1"},
])
def test_invalid_combinations_are_refused(raw):
    with pytest.raises(ConfigError):
        config.from_dict(raw)


def test_fixed_end_date():
    assert config.from_dict({"fixed_contract_end": "2027-03-31"}).fixed_end_date.isoformat() == "2027-03-31"
    assert config.from_dict({}).fixed_end_date is None


# ---------------------------------------------------------------- prices
def _entsoe_state(unit="€/kWh", minutes=60, n=4):
    start = datetime(2026, 11, 9, 0, 0, tzinfo=BXL)
    items = [{"time": (start + timedelta(minutes=minutes * i)).isoformat(), "price": 0.1 + 0.01 * i} for i in range(n)]
    return {"state": "0.1", "attributes": {"unit_of_measurement": unit, "prices": items}}


def test_entsoe_eur_per_kwh_becomes_eur_per_mwh():
    slots = prices.parse_state(_entsoe_state())
    assert [s.eur_mwh for s in slots] == [100.0, 110.0, 120.0, 130.0]
    assert slots[0].start == datetime(2026, 11, 8, 23, 0, tzinfo=timezone.utc)
    assert all(s.minutes == 60 for s in slots)


def test_quarter_hour_slots_are_detected():
    slots = prices.parse_state(_entsoe_state(minutes=15))
    assert all(s.minutes == 15 for s in slots)


def test_nordpool_raw_today_and_tomorrow_with_cents():
    day = datetime(2026, 11, 9, tzinfo=BXL)
    raw = lambda d: [{"start": (d + timedelta(hours=h)).isoformat(), "end": (d + timedelta(hours=h + 1)).isoformat(),
                      "value": 10.0} for h in range(24)]
    st = {"attributes": {"unit_of_measurement": "c/kWh", "raw_today": raw(day),
                         "raw_tomorrow": raw(day + timedelta(days=1))}}
    slots = prices.parse_state(st)
    assert len(slots) == 48 and slots[0].eur_mwh == 100.0


def test_unit_guess_without_unit():
    assert prices.unit_factor(None, [0.08, 0.12]) == 1000
    assert prices.unit_factor("", [80, 120]) == 1
    assert prices.unit_factor("EUR/MWh", [80]) == 1


def test_price_at_and_merge():
    slots = prices.parse_state(_entsoe_state())
    t = slots[1].start + timedelta(minutes=30)
    assert prices.price_at(slots, t) == 110.0
    assert prices.price_at(slots, slots[-1].end) is None
    newer = [prices.PriceSlot(slots[1].start, slots[1].end, 999.0)]
    merged = prices.merge(slots, newer, keep_after=slots[0].end)
    assert [s.eur_mwh for s in merged] == [999.0, 120.0, 130.0]
    assert prices.from_json(prices.to_json(merged)) == merged


def test_broken_items_are_skipped():
    st = {"attributes": {"prices": [{"time": "nonsense", "price": 1}, {"time": "2026-11-09T00:00:00+00:00"},
                                    {"time": "2026-11-09T00:00:00+00:00", "price": "x"}]}}
    assert prices.parse_state(st) == []
    assert prices.parse_state(None) == []


# ---------------------------------------------------------------- tariffs
def test_formulas_match_the_original_templates():
    o = Options()
    t = datetime(2026, 11, 10, 12, tzinfo=BXL)   # a Tuesday
    dyn, fix = tariffs.DynamicTariff(o), tariffs.FixedTariff(o)
    assert dyn.buy(t, 100) == pytest.approx((0.102 * 100 + 1) * 1.06 + 11.96)
    assert dyn.sell(t, 100) == pytest.approx(0.1 * 100 - 1.188 - 0.19)
    assert fix.buy(t) == pytest.approx(18.59 + 11.96)
    assert fix.sell(t, None, 100) == pytest.approx(0.084 * 0.95 * 100 - 2.72 - 0.19, abs=1e-9)
    assert round(tariffs.breakeven_eur_mwh(15.22, o)) == 131
    assert round(tariffs.breakeven_eur_mwh(18.59, o)) == 162


@pytest.mark.parametrize("when,night", [
    (datetime(2026, 11, 10, 21, 59), False), (datetime(2026, 11, 10, 22, 0), True),
    (datetime(2026, 11, 11, 6, 59), True), (datetime(2026, 11, 11, 7, 0), False),
    (datetime(2026, 11, 14, 12, 0), True),     # Saturday
])
def test_day_night_schedule(when, night):
    assert tariffs.is_night(when.replace(tzinfo=BXL), Options()) is night


def test_weekend_can_be_day_tariff():
    o = Options(weekend_is_night=False)
    assert not tariffs.is_night(datetime(2026, 11, 14, 12, tzinfo=BXL), o)


def test_window_that_does_not_cross_midnight():
    o = Options(night_start="01:00", night_end="06:00", weekend_is_night=False)
    assert tariffs.is_night(datetime(2026, 11, 10, 3, tzinfo=BXL), o)
    assert not tariffs.is_night(datetime(2026, 11, 10, 23, tzinfo=BXL), o)


def test_capacity_tariff_uses_the_minimum_and_can_be_off():
    assert tariffs.capacity_month_eur(1.8, Options()) == pytest.approx(2.5 * 52.37 / 12)
    assert tariffs.capacity_month_eur(4.0, Options()) == pytest.approx(4.0 * 52.37 / 12)
    assert tariffs.capacity_month_eur(4.0, Options(capacity_tariff_eur_kw_year=0)) == 0


# ---------------------------------------------------------------- discovery
def _state(unit=None):
    return {"state": "1", "attributes": {"unit_of_measurement": unit} if unit else {}}


SOLIS = {
    "sensor.solis_grid_import_power": _state("W"),
    "sensor.solis_grid_export_power": _state("W"),
    "sensor.solis_household_load_power": _state("kW"),
    "sensor.solis_backup_load_power": _state("W"),
    "sensor.solis_total_pv_power": _state("W"),
    "sensor.solis_battery_soc": _state("%"),
    "sensor.solis_battery_voltage": _state("V"),
    "switch.solis_time_of_use": _state(),
    "switch.solis_allow_grid_to_charge_the_battery": _state(),
    "number.solis_time_charging_charge_current": _state("A"),
    "number.solis_time_charging_discharge_current": _state("A"),
    "number.solis_grid_time_of_use_charge_current": _state("A"),
    "number.solis_grid_time_of_use_discharge_current": _state("A"),
    "number.solis_export_power_limit": _state("W"),
    "time.solis_time_charging_charge_start_slot_1": _state(),
    "time.solis_time_charging_charge_end_slot_1": _state(),
    "time.solis_time_charging_discharge_start_slot_1": _state(),
    "time.solis_time_charging_discharge_end_slot_1": _state(),
    "time.solis_grid_time_of_use_charge_start_slot_1": _state(),
    "time.solis_grid_time_of_use_charge_end_slot_1": _state(),
    "time.solis_grid_time_of_use_discharge_start_slot_1": _state(),
    "time.solis_grid_time_of_use_discharge_end_slot_1": _state(),
    "sensor.power_production_now": _state("W"),
    "sensor.energy_production_today": _state("kWh"),
    "sensor.energy_production_tomorrow": _state("kWh"),
    "sensor.current_electricity_market_price": _state("€/kWh"),
}


def test_finds_a_full_solis_setup():
    found, missing = discovery.resolve_all(Options(), SOLIS)
    assert missing == []
    assert found["house_load_power"].entity_id == "sensor.solis_household_load_power"
    assert found["house_load_power"].scale == 1.0
    assert found["grid_import_power"].scale == 0.001
    assert found["charge_start"].entity_id == "time.solis_time_charging_charge_start_slot_1"
    assert found["charge_current"].entity_id == "number.solis_time_charging_charge_current"
    assert found["export_limit"].entity_id == "number.solis_export_power_limit"


def test_slot_set_switch_picks_the_other_entities():
    found, _ = discovery.resolve_all(Options(slot_entities="grid_time_of_use"), SOLIS)
    assert found["discharge_end"].entity_id == "time.solis_grid_time_of_use_discharge_end_slot_1"
    assert found["discharge_current"].entity_id == "number.solis_grid_time_of_use_discharge_current"


def test_override_that_does_not_exist_is_reported():
    o = config.from_dict({"battery_soc": "sensor.nope"})
    _, missing = discovery.resolve_all(o, SOLIS)
    assert missing == ["battery_soc"]


def test_several_candidates_without_solis_tiebreak():
    states = {"sensor.a_battery_soc": _state(), "sensor.b_battery_soc": _state()}
    f = discovery.resolve("battery_soc", Options(), states)
    assert f.entity_id is None and "several candidates" in f.how


def test_find_notify():
    phones = [{"domain": "notify", "services": {"mobile_app_pixel": {}, "persistent_notification": {}}}]
    assert discovery.find_notify(Options(), phones)[0] == "notify.mobile_app_pixel"
    two = [{"domain": "notify", "services": {"mobile_app_a": {}, "mobile_app_b": {}}}]
    assert discovery.find_notify(Options(), two)[0] is None
    assert discovery.find_notify(Options(notify_service="mobile_app_x"), [])[0] == "notify.mobile_app_x"


def test_price_times_without_a_time_zone_are_in_home_assistants_time_zone():
    st = {"state": "0.1", "attributes": {"unit_of_measurement": "EUR/kWh",
          "prices": [{"time": "2026-01-14T00:00:00", "price": 0.10}, {"time": "2026-01-14T01:00:00", "price": 0.12}]}}
    slots = prices.parse_state(st, ZoneInfo("Europe/Brussels"))
    assert slots[0].start == datetime(2026, 1, 13, 23, 0, tzinfo=timezone.utc)     # midnight in Brussels
    assert prices.parse_state(st)[0].start == datetime(2026, 1, 14, 0, 0, tzinfo=timezone.utc)   # no zone: UTC


def test_price_times_in_every_form():
    aware = datetime(2026, 1, 14, 12, 0, tzinfo=BXL)
    assert prices.parse_time(aware) == aware.astimezone(timezone.utc)
    assert prices.parse_time("2026-01-14T11:00:00Z") == datetime(2026, 1, 14, 11, 0, tzinfo=timezone.utc)
    assert prices.parse_time("not a time") is None and prices.parse_time(None) is None and prices.parse_time(12) is None


def test_slots_without_an_end_take_the_spacing_of_the_list():
    st = {"state": "0.1", "attributes": {"unit_of_measurement": "EUR/MWh", "data": [
        {"start": "2026-01-14T00:00:00+01:00", "value": 100},
        {"start": "2026-01-14T00:15:00+01:00", "value": 110},
        {"start": "garbage", "value": 120},
        {"start": "2026-01-14T00:30:00+01:00", "value": "n/a"}]}}
    slots = prices.parse_state(st)
    assert [s.minutes for s in slots] == [15, 15] and [s.eur_mwh for s in slots] == [100, 110]


def test_each_contract_has_its_own_yearly_fee():
    o = Options(fixed_yearly_fee_eur=65, dynamic_yearly_fee_eur=99)
    assert tariffs.FixedTariff(o).yearly_fee_eur == 65 and tariffs.DynamicTariff(o).yearly_fee_eur == 99
