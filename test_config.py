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
    cfg = load_site_config()
    assert (cfg.panel_limit_kw, cfg.safety_margin_kw) == (600.0, 120.0)
    assert (cfg.max_charger_kw, cfg.min_charger_kw) == (10.0, 1.4)
    assert (cfg.stale_floor_kw, cfg.telemetry_stale_after_s) == (2.0, 15.0)
    assert cfg.peak_windows == (PeakWindow(16 * 60, 21 * 60),)       # PG&E 4-9 PM
    assert cfg.timezone == "America/Los_Angeles"
    assert cfg.grid_signal_enabled is False
    assert (cfg.host, cfg.port) == ("localhost", 9000)


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
    ("site", "safety_margin_kw", 600.0),          # leaves nothing for the building
    ("site", "timezone", "Mars/Olympus"),
    ("site", "site_meter_id", ""),
    ("site", "site_meter_id", "a/b"),
    ("chargers", "max_charger_kw", 0),
    ("chargers", "min_charger_kw", 11.0),         # above the max
    ("chargers", "min_charger_kw", 0),
    ("chargers", "taper_threshold_w", 1400.0),    # would flag every car at the minimum
    ("chargers", "taper_grace_s", -1),
    ("telemetry", "stale_after_s", 0),
    ("telemetry", "stale_floor_kw", 1.0),         # below min: the stale floor would be 0 W
    ("telemetry", "stale_floor_kw", 481.0),       # above the usable capacity
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
