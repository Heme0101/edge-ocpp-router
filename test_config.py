import copy
import dataclasses
import json

import pytest

from config import DEFAULT_CONFIG_PATH, ConfigError, PeakWindow, load_site_config

RAW = json.loads(DEFAULT_CONFIG_PATH.read_text())


def write(tmp_path, data):
    path = tmp_path / "site_config.json"
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return path


def with_change(section, key, value):
    data = copy.deepcopy(RAW)
    data[section][key] = value
    return data


def test_shipped_config_values():
    """The reference site: a small commercial / HOA building."""
    cfg = load_site_config()
    assert (cfg.service_voltage_v, cfg.service_phases, cfg.main_breaker_a) == (208.0, 3, 400.0)
    assert (cfg.transformer_kva, cfg.min_power_factor) == (150.0, 0.9)
    assert (cfg.panel_limit_kw, cfg.safety_margin_kw) == (100.0, 20.0)   # margin >= largest load step
    assert (cfg.charger_voltage_v, cfg.charger_max_current_a) == (208.0, 32.0)
    assert (cfg.max_charger_kw, cfg.min_charger_kw) == (6.656, 1.44)    # 32 A / 6.9 A at 208 V
    assert cfg.min_increase_w == 100.0 and cfg.meter_sample_interval_s == 5
    assert (cfg.stale_floor_kw, cfg.telemetry_stale_after_s) == (2.0, 15.0)
    assert cfg.ct_accuracy_pct == 1.0 and round(cfg.ct_tolerance_kw, 3) == 0.8    # 1 % of 80 kW
    assert cfg.peak_windows == (PeakWindow(16 * 60, 21 * 60),)       # PG&E 4-9 PM
    assert cfg.peak_budget_cap_kw == 5.0
    assert cfg.timezone == "America/Los_Angeles"
    assert cfg.grid_signal_enabled is False
    assert (cfg.host, cfg.port, cfg.dashboard_port) == ("localhost", 9000, 8000)
    assert (cfg.peak_demand_rate_per_kw, cfg.peak_energy_rate_per_kwh,
            cfg.offpeak_energy_rate_per_kwh) == (25.0, 0.42, 0.18)
    assert cfg.savings_enabled


def test_shipped_limit_fits_the_service_continuously():
    # 208 V x 400 A x sqrt(3) x 80 % = 115.3 kVA (transformer 150 kVA is larger);
    # x 0.9 power factor = 103.8 kW >= the 100 kW limit.
    cfg = load_site_config()
    assert round(cfg.continuous_capacity_kw, 1) == 103.8
    assert cfg.panel_limit_kw <= cfg.continuous_capacity_kw


def test_limit_above_continuous_rating_rejected(tmp_path):
    with pytest.raises(ConfigError, match="continuously"):
        load_site_config(write(tmp_path, with_change("site", "panel_limit_kw", 110.0)))


def test_100_percent_rated_breaker_allows_more(tmp_path):
    data = with_change("site", "panel_limit_kw", 110.0)
    data["electrical_service"]["breaker_100_percent_rated"] = True    # 144.1 kVA x 0.9 = 129.7 kW
    assert load_site_config(write(tmp_path, data)).panel_limit_kw == 110.0


def test_transformer_can_be_the_limit(tmp_path):
    data = with_change("electrical_service", "transformer_kva", 75.0)   # 75 kVA x 0.9 = 67.5 kW
    with pytest.raises(ConfigError, match="continuously"):
        load_site_config(write(tmp_path, data))


def test_single_phase_service(tmp_path):
    data = copy.deepcopy(RAW)
    data["electrical_service"].update(voltage_v=240.0, phases=1, main_breaker_a=200.0)   # 38.4 kVA
    data["site"].update(panel_limit_kw=34.0, safety_margin_kw=4.0)
    assert load_site_config(write(tmp_path, data)).continuous_capacity_kw == pytest.approx(34.56)


def test_charger_max_above_its_rating_rejected(tmp_path):
    with pytest.raises(ConfigError, match="rating"):
        load_site_config(write(tmp_path, with_change("chargers", "max_charger_kw", 7.2)))


def test_charger_min_below_j1772_rejected(tmp_path):
    with pytest.raises(ConfigError, match="J1772"):
        load_site_config(write(tmp_path, with_change("chargers", "min_charger_kw", 1.2)))   # < 1.248


def test_relative_session_log_resolves_next_to_config(tmp_path):
    cfg = load_site_config(write(tmp_path, RAW))
    assert cfg.session_log_path == str(tmp_path.resolve() / "sessions.jsonl")


def test_null_session_log_disables_it(tmp_path):
    assert load_site_config(write(tmp_path, with_change("server", "session_log_path", None))).session_log_path is None


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_site_config(tmp_path / "nope.json")


def test_invalid_json(tmp_path):
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_site_config(write(tmp_path, "{ not json"))


def test_missing_key(tmp_path):
    data = copy.deepcopy(RAW)
    del data["chargers"]["max_charger_kw"]
    with pytest.raises(ConfigError, match="missing"):
        load_site_config(write(tmp_path, data))


def test_unknown_key_catches_typos(tmp_path):
    with pytest.raises(ConfigError, match="unknown"):
        load_site_config(write(tmp_path, with_change("chargers", "max_charger_kW", 10.0)))


def test_missing_section(tmp_path):
    data = copy.deepcopy(RAW)
    del data["tariff"]
    with pytest.raises(ConfigError):
        load_site_config(write(tmp_path, data))


@pytest.mark.parametrize("section, key, value", [
    ("site", "panel_limit_kw", 0),
    ("site", "panel_limit_kw", "600"),
    ("site", "panel_limit_kw", True),
    ("site", "safety_margin_kw", -1),
    ("site", "safety_margin_kw", 100.0),          # leaves nothing for the building
    ("site", "timezone", "Mars/Olympus"),
    ("site", "site_meter_id", ""),
    ("site", "site_meter_id", "a/b"),
    ("chargers", "max_charger_kw", 0),
    ("chargers", "min_charger_kw", 7.0),          # above the max
    ("chargers", "min_charger_kw", 0),
    ("chargers", "taper_threshold_w", 1440.0),    # would flag every car at the minimum
    ("chargers", "min_increase_w", 1440.0),       # a stopped car could never start
    ("chargers", "min_increase_w", -1),
    ("server", "meter_sample_interval_s", 0),
    ("server", "meter_sample_interval_s", 15),    # not below the stale limit
    ("server", "meter_sample_interval_s", 5.5),
    ("server", "meter_sample_interval_s", True),
    ("chargers", "taper_grace_s", -1),
    ("telemetry", "stale_after_s", 0),
    ("telemetry", "stale_floor_kw", 1.0),         # below min: the stale floor would be 0 W
    ("telemetry", "stale_floor_kw", 91.0),        # above the usable capacity
    ("electrical_service", "phases", 2),
    ("telemetry", "ct_accuracy_pct", 6.0),
    ("telemetry", "ct_accuracy_pct", -1.0),
    ("electrical_service", "min_power_factor", 1.1),
    ("electrical_service", "breaker_100_percent_rated", "no"),
    ("electrical_service", "main_breaker_a", 0),
    ("chargers", "max_current_a", 5.0),           # below the J1772 minimum
    ("tariff", "peak_budget_cap_kw", -1),
    ("tariff", "peak_windows", [{"start": "16:00", "end": "16:00"}]),
    ("tariff", "peak_windows", [{"start": "24:00", "end": "02:00"}]),
    ("tariff", "peak_windows", [{"start": "4pm", "end": "9pm"}]),
    ("tariff", "peak_windows", [{"start": "16:00"}]),
    ("tariff", "peak_windows", "16:00-21:00"),
    ("grid_signal", "enabled", "yes"),
    ("grid_signal", "max_age_s", 0),
    ("server", "port", 70000),
    ("server", "port", 9000.5),
    ("server", "allocation_interval_s", 0),
    ("server", "call_timeout_s", -1),
    ("server", "dashboard_port", 9000),           # same as the OCPP port
    ("server", "dashboard_port", 70000),
    ("server", "dashboard_port", "8000"),
    ("server", "dashboard_port", True),
    ("tariff", "peak_demand_rate_per_kw", -1.0),
    ("tariff", "peak_demand_rate_per_kw", 2500.0),     # cents typed as dollars
    ("tariff", "peak_demand_rate_per_kw", "25"),
    ("tariff", "peak_demand_rate_per_kw", True),
    ("tariff", "peak_demand_rate_per_kw", None),
    ("tariff", "peak_energy_rate_per_kwh", 42.0),      # cents typed as dollars
    ("tariff", "peak_energy_rate_per_kwh", -0.1),
    ("tariff", "peak_energy_rate_per_kwh", 0.10),      # below the off-peak rate
    ("tariff", "offpeak_energy_rate_per_kwh", 0.50),   # above the peak rate
])
def test_bad_values_rejected(tmp_path, section, key, value):
    with pytest.raises(ConfigError):
        load_site_config(write(tmp_path, with_change(section, key, value)))


def test_replace_is_validated_too():
    with pytest.raises(ConfigError):
        dataclasses.replace(load_site_config(), min_charger_kw=50.0)


def test_empty_peak_window_list_is_allowed(tmp_path):
    assert load_site_config(write(tmp_path, with_change("tariff", "peak_windows", []))).peak_windows == ()


@pytest.mark.parametrize("minute, inside", [(959, False), (960, True), (1259, True), (1260, False)])
def test_peak_window_bounds(minute, inside):
    assert PeakWindow(960, 1260).contains(minute) is inside


@pytest.mark.parametrize("minute, inside", [(1319, False), (1320, True), (0, True), (119, True), (120, False)])
def test_peak_window_past_midnight(minute, inside):
    assert PeakWindow(22 * 60, 2 * 60).contains(minute) is inside


def test_demo_config_is_valid():
    cfg = load_site_config(DEFAULT_CONFIG_PATH.with_name("site_config.demo.json"))
    assert (cfg.panel_limit_kw, cfg.safety_margin_kw) == (600.0, 120.0)
    assert cfg.continuous_capacity_kw == pytest.approx(675.0)   # 750 kVA transformer x 0.9
    assert cfg.max_charger_kw == 9.984 and cfg.session_log_path is None


def test_tariff_rates_are_optional(tmp_path):
    data = copy.deepcopy(RAW)
    for key in ("peak_demand_rate_per_kw", "peak_energy_rate_per_kwh", "offpeak_energy_rate_per_kwh"):
        del data["tariff"][key]
    cfg = load_site_config(write(tmp_path, data))
    assert cfg.peak_demand_rate_per_kw is None and not cfg.savings_enabled


def test_tariff_rates_all_or_none(tmp_path):
    data = copy.deepcopy(RAW)
    del data["tariff"]["offpeak_energy_rate_per_kwh"]
    with pytest.raises(ConfigError, match="offpeak_energy_rate_per_kwh"):
        load_site_config(write(tmp_path, data))


def test_tariff_rates_all_or_none_on_replace():
    with pytest.raises(ConfigError, match="all three"):
        dataclasses.replace(load_site_config(), peak_demand_rate_per_kw=None)


def test_zero_rates_are_allowed(tmp_path):
    data = copy.deepcopy(RAW)
    data["tariff"].update(peak_demand_rate_per_kw=0, peak_energy_rate_per_kwh=0.3,
                          offpeak_energy_rate_per_kwh=0.3)
    cfg = load_site_config(write(tmp_path, data))
    assert cfg.peak_demand_rate_per_kw == 0.0 and cfg.savings_enabled
