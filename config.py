"""Site configuration. Every site-specific number lives in site_config.json, not in code.

The file is validated strictly when it is loaded: a missing key, an unknown key (usually a
typo), a wrong type or an out-of-range value stops the server at startup with a clear
message, instead of running a building on a silently wrong number.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_CONFIG_PATH = Path(__file__).with_name("site_config.json")

_SCHEMA = {
    "site": {"panel_limit_kw", "safety_margin_kw", "site_meter_id", "timezone"},
    "chargers": {"max_charger_kw", "min_charger_kw", "taper_threshold_w", "taper_grace_s",
                 "offline_session_timeout_s"},
    "telemetry": {"stale_after_s", "stale_floor_kw"},
    "tariff": {"peak_windows", "peak_budget_cap_kw"},
    "grid_signal": {"enabled", "poll_interval_s", "max_age_s"},
    "server": {"host", "port", "allocation_interval_s", "profile_refresh_s", "call_timeout_s",
               "session_log_path"},
}
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ConfigError(ValueError):
    """site_config.json is missing, malformed, or has a value out of range."""


@dataclass(frozen=True)
class PeakWindow:
    """A daily window in site-local time. start is inclusive, end is exclusive.

    An end earlier than the start means the window runs past midnight (e.g. 22:00-02:00).
    """
    start_minute: int
    end_minute: int

    def contains(self, minute_of_day: int) -> bool:
        if self.start_minute < self.end_minute:
            return self.start_minute <= minute_of_day < self.end_minute
        return minute_of_day >= self.start_minute or minute_of_day < self.end_minute


@dataclass(frozen=True)
class SiteConfig:
    panel_limit_kw: float
    safety_margin_kw: float
    site_meter_id: str
    timezone: str
    max_charger_kw: float
    min_charger_kw: float
    taper_threshold_w: float
    taper_grace_s: float
    offline_session_timeout_s: float
    telemetry_stale_after_s: float
    stale_floor_kw: float
    peak_windows: tuple[PeakWindow, ...]
    peak_budget_cap_kw: float
    grid_signal_enabled: bool
    grid_signal_poll_interval_s: float
    grid_signal_max_age_s: float
    host: str
    port: int
    allocation_interval_s: float
    profile_refresh_s: float
    call_timeout_s: float
    session_log_path: str | None

    def __post_init__(self):
        # Runs on every construction, including dataclasses.replace(), so a config can
        # never exist in an invalid state.
        _check("site.panel_limit_kw", self.panel_limit_kw, low=0.0, low_inclusive=False)
        _check("site.safety_margin_kw", self.safety_margin_kw, low=0.0)
        if self.safety_margin_kw >= self.panel_limit_kw:
            raise ConfigError("site.safety_margin_kw must be smaller than site.panel_limit_kw")
        usable_kw = self.panel_limit_kw - self.safety_margin_kw
        _check_text("site.site_meter_id", self.site_meter_id)
        if "/" in self.site_meter_id:
            raise ConfigError("site.site_meter_id must not contain '/'")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise ConfigError(f"site.timezone {self.timezone!r} is not a known IANA time zone")
        _check("chargers.max_charger_kw", self.max_charger_kw, low=0.0, low_inclusive=False)
        _check("chargers.min_charger_kw", self.min_charger_kw, low=0.0, low_inclusive=False,
               high=self.max_charger_kw)
        # A threshold at or above the minimum would make every car at the minimum look idle.
        _check("chargers.taper_threshold_w", self.taper_threshold_w, low=0.0,
               high=self.min_charger_kw * 1000.0, high_inclusive=False)
        _check("chargers.taper_grace_s", self.taper_grace_s, low=0.0)
        _check("chargers.offline_session_timeout_s", self.offline_session_timeout_s,
               low=0.0, low_inclusive=False)
        _check("telemetry.stale_after_s", self.telemetry_stale_after_s, low=0.0, low_inclusive=False)
        # Below the minimum nobody could charge on stale data, which is exactly the 0 W
        # case the floor exists to avoid.
        _check("telemetry.stale_floor_kw", self.stale_floor_kw, low=self.min_charger_kw,
               high=usable_kw)
        if not isinstance(self.peak_windows, tuple) or not all(
                isinstance(w, PeakWindow) for w in self.peak_windows):
            raise ConfigError("tariff.peak_windows must be a list of windows")
        _check("tariff.peak_budget_cap_kw", self.peak_budget_cap_kw, low=0.0)
        if not isinstance(self.grid_signal_enabled, bool):
            raise ConfigError("grid_signal.enabled must be true or false")
        _check("grid_signal.poll_interval_s", self.grid_signal_poll_interval_s, low=0.0,
               low_inclusive=False)
        _check("grid_signal.max_age_s", self.grid_signal_max_age_s, low=0.0, low_inclusive=False)
        _check_text("server.host", self.host)
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 0 <= self.port <= 65535:
            raise ConfigError("server.port must be a whole number from 0 to 65535")
        _check("server.allocation_interval_s", self.allocation_interval_s, low=0.0, low_inclusive=False)
        _check("server.profile_refresh_s", self.profile_refresh_s, low=0.0, low_inclusive=False)
        _check("server.call_timeout_s", self.call_timeout_s, low=0.0, low_inclusive=False)
        if self.session_log_path is not None:
            _check_text("server.session_log_path", self.session_log_path)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load_site_config(path: str | Path = DEFAULT_CONFIG_PATH) -> SiteConfig:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"cannot read {path}: {error}")
    except json.JSONDecodeError as error:
        raise ConfigError(f"{path} is not valid JSON: {error}")

    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    _exact_keys("top level", data, set(_SCHEMA))
    for section, keys in _SCHEMA.items():
        if not isinstance(data[section], dict):
            raise ConfigError(f"{section} must be a JSON object")
        _exact_keys(section, data[section], keys)

    site, chargers, telemetry = data["site"], data["chargers"], data["telemetry"]
    tariff, grid, server = data["tariff"], data["grid_signal"], data["server"]

    log_path = server["session_log_path"]
    if isinstance(log_path, str) and log_path and not Path(log_path).is_absolute():
        log_path = str(path.resolve().parent / log_path)   # relative to the config file

    return SiteConfig(
        panel_limit_kw=_num("site.panel_limit_kw", site["panel_limit_kw"]),
        safety_margin_kw=_num("site.safety_margin_kw", site["safety_margin_kw"]),
        site_meter_id=site["site_meter_id"],
        timezone=site["timezone"],
        max_charger_kw=_num("chargers.max_charger_kw", chargers["max_charger_kw"]),
        min_charger_kw=_num("chargers.min_charger_kw", chargers["min_charger_kw"]),
        taper_threshold_w=_num("chargers.taper_threshold_w", chargers["taper_threshold_w"]),
        taper_grace_s=_num("chargers.taper_grace_s", chargers["taper_grace_s"]),
        offline_session_timeout_s=_num("chargers.offline_session_timeout_s",
                                       chargers["offline_session_timeout_s"]),
        telemetry_stale_after_s=_num("telemetry.stale_after_s", telemetry["stale_after_s"]),
        stale_floor_kw=_num("telemetry.stale_floor_kw", telemetry["stale_floor_kw"]),
        peak_windows=_parse_windows(tariff["peak_windows"]),
        peak_budget_cap_kw=_num("tariff.peak_budget_cap_kw", tariff["peak_budget_cap_kw"]),
        grid_signal_enabled=grid["enabled"],
        grid_signal_poll_interval_s=_num("grid_signal.poll_interval_s", grid["poll_interval_s"]),
        grid_signal_max_age_s=_num("grid_signal.max_age_s", grid["max_age_s"]),
        host=server["host"],
        port=server["port"],
        allocation_interval_s=_num("server.allocation_interval_s", server["allocation_interval_s"]),
        profile_refresh_s=_num("server.profile_refresh_s", server["profile_refresh_s"]),
        call_timeout_s=_num("server.call_timeout_s", server["call_timeout_s"]),
        session_log_path=log_path,
    )


def _exact_keys(where, section, expected):
    missing, unknown = expected - set(section), set(section) - expected
    if missing:
        raise ConfigError(f"{where}: missing key(s) {sorted(missing)}")
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)} (typo?)")


def _num(name, value):
    """A JSON number as float. Rejects true/false, which Python would treat as 1/0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number, got {value!r}")
    return float(value)


def _check(name, value, *, low=None, high=None, low_inclusive=True, high_inclusive=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConfigError(f"{name} must be a finite number, got {value!r}")
    if low is not None and (value < low if low_inclusive else value <= low):
        raise ConfigError(f"{name} must be {'>=' if low_inclusive else '>'} {low}, got {value}")
    if high is not None and (value > high if high_inclusive else value >= high):
        raise ConfigError(f"{name} must be {'<=' if high_inclusive else '<'} {high}, got {value}")


def _check_text(name, value):
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")


def _parse_windows(raw):
    if not isinstance(raw, list):
        raise ConfigError("tariff.peak_windows must be a list")
    windows = []
    for i, item in enumerate(raw):
        where = f"tariff.peak_windows[{i}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} must be an object with start and end")
        _exact_keys(where, item, {"start", "end"})
        start, end = _minute_of_day(where, item["start"]), _minute_of_day(where, item["end"])
        if start == end:
            raise ConfigError(f"{where}: start and end are equal")
        windows.append(PeakWindow(start, end))
    return tuple(windows)


def _minute_of_day(where, text):
    match = _HHMM.match(text) if isinstance(text, str) else None
    if not match:
        raise ConfigError(f"{where}: {text!r} is not a 24-hour HH:MM time")
    return int(match.group(1)) * 60 + int(match.group(2))
