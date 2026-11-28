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
    "electrical_service": {"voltage_v", "phases", "main_breaker_a", "breaker_100_percent_rated",
                           "transformer_kva", "min_power_factor"},
    "chargers": {"voltage_v", "max_current_a", "max_charger_kw", "min_charger_kw", "min_increase_w",
                 "taper_threshold_w", "taper_grace_s", "offline_session_timeout_s"},
    "telemetry": {"stale_after_s", "stale_floor_kw", "ct_accuracy_pct"},
    "tariff": {"peak_windows", "peak_budget_cap_kw"},
    "grid_signal": {"enabled", "poll_interval_s", "max_age_s"},
    "server": {"host", "port", "dashboard_port", "allocation_interval_s", "profile_refresh_s",
               "call_timeout_s", "meter_sample_interval_s", "session_log_path"},
}
# Optional keys: either all of a group are present, or none of them.
_OPTIONAL = {
    "tariff": {"peak_demand_rate_per_kw", "peak_energy_rate_per_kwh", "offpeak_energy_rate_per_kwh"},
}
# Sanity bounds that catch unit mistakes (cents typed as dollars). PG&E commercial
# demand charges are tens of $/kW-month and energy rates tens of cents per kWh.
MAX_DEMAND_RATE_PER_KW = 200.0
MAX_ENERGY_RATE_PER_KWH = 5.0
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
J1772_MIN_CURRENT_A = 6.0       # SAE J1772: the lowest pilot current a charger may offer
STANDARD_BREAKER_CONTINUOUS = 0.8   # NEC: a standard breaker carries 80 % continuously


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
    service_voltage_v: float
    service_phases: int
    main_breaker_a: float
    breaker_100_percent_rated: bool
    transformer_kva: float
    min_power_factor: float
    charger_voltage_v: float
    charger_max_current_a: float
    max_charger_kw: float
    min_charger_kw: float
    min_increase_w: float
    taper_threshold_w: float
    taper_grace_s: float
    offline_session_timeout_s: float
    telemetry_stale_after_s: float
    stale_floor_kw: float
    ct_accuracy_pct: float
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
    meter_sample_interval_s: int
    session_log_path: str | None
    dashboard_port: int
    # Tariff rates for the savings estimate (None: savings are reported in kW/kWh only).
    peak_demand_rate_per_kw: float | None = None       # $ per kW of monthly peak-window demand
    peak_energy_rate_per_kwh: float | None = None      # $ per kWh inside a peak window
    offpeak_energy_rate_per_kwh: float | None = None   # $ per kWh outside the peak windows

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
        self._check_electrical()
        _check("chargers.max_charger_kw", self.max_charger_kw, low=0.0, low_inclusive=False)
        _check("chargers.min_charger_kw", self.min_charger_kw, low=0.0, low_inclusive=False,
               high=self.max_charger_kw)
        # Increases smaller than this are not sent (decreases always are). It must stay
        # below the minimum, or a stopped car could never be started.
        _check("chargers.min_increase_w", self.min_increase_w, low=0.0,
               high=self.min_charger_kw * 1000.0, high_inclusive=False)
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
        # A meter accuracy class: 0.2, 0.5, 1 or 2 % are usual for CT meters.
        _check("telemetry.ct_accuracy_pct", self.ct_accuracy_pct, low=0.0, high=5.0)
        if self.ct_tolerance_kw > self.safety_margin_kw:
            raise ConfigError("site.safety_margin_kw must at least cover the CT meter's accuracy "
                              f"({self.ct_tolerance_kw:g} kW)")
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
        if isinstance(self.meter_sample_interval_s, bool) or not isinstance(self.meter_sample_interval_s, int) \
                or not 1 <= self.meter_sample_interval_s < self.telemetry_stale_after_s:
            # OCPP's MeterValueSampleInterval is whole seconds; readings must arrive well
            # before they would count as stale.
            raise ConfigError("server.meter_sample_interval_s must be a whole number of seconds, "
                              "at least 1 and below telemetry.stale_after_s")
        if self.session_log_path is not None:
            _check_text("server.session_log_path", self.session_log_path)
        if isinstance(self.dashboard_port, bool) or not isinstance(self.dashboard_port, int) \
                or not 0 <= self.dashboard_port <= 65535:
            raise ConfigError("server.dashboard_port must be a whole number from 0 to 65535")
        if self.dashboard_port != 0 and self.dashboard_port == self.port:
            raise ConfigError("server.dashboard_port must differ from server.port")
        self._check_rates()

    def _check_rates(self):
        rates = (self.peak_demand_rate_per_kw, self.peak_energy_rate_per_kwh,
                 self.offpeak_energy_rate_per_kwh)
        if all(rate is None for rate in rates):
            return
        if any(rate is None for rate in rates):
            raise ConfigError("tariff: give all three of peak_demand_rate_per_kw, "
                              "peak_energy_rate_per_kwh and offpeak_energy_rate_per_kwh, or none")
        _check("tariff.peak_demand_rate_per_kw", self.peak_demand_rate_per_kw, low=0.0,
               high=MAX_DEMAND_RATE_PER_KW)
        _check("tariff.peak_energy_rate_per_kwh", self.peak_energy_rate_per_kwh, low=0.0,
               high=MAX_ENERGY_RATE_PER_KWH)
        _check("tariff.offpeak_energy_rate_per_kwh", self.offpeak_energy_rate_per_kwh, low=0.0,
               high=MAX_ENERGY_RATE_PER_KWH)
        if self.offpeak_energy_rate_per_kwh > self.peak_energy_rate_per_kwh:
            raise ConfigError("tariff.offpeak_energy_rate_per_kwh must not exceed "
                              "tariff.peak_energy_rate_per_kwh")

    @property
    def savings_enabled(self) -> bool:
        return self.peak_demand_rate_per_kw is not None

    def _check_electrical(self):
        """The kW settings must be physically possible for the installed equipment."""
        _check("electrical_service.voltage_v", self.service_voltage_v, low=0.0, low_inclusive=False)
        if self.service_phases not in (1, 3) or isinstance(self.service_phases, bool):
            raise ConfigError("electrical_service.phases must be 1 or 3")
        _check("electrical_service.main_breaker_a", self.main_breaker_a, low=0.0, low_inclusive=False)
        if not isinstance(self.breaker_100_percent_rated, bool):
            raise ConfigError("electrical_service.breaker_100_percent_rated must be true or false")
        _check("electrical_service.transformer_kva", self.transformer_kva, low=0.0, low_inclusive=False)
        _check("electrical_service.min_power_factor", self.min_power_factor, low=0.0,
               low_inclusive=False, high=1.0)
        capacity_kw = self.continuous_capacity_kw
        if self.panel_limit_kw > capacity_kw + 1e-9:
            raise ConfigError(
                f"site.panel_limit_kw {self.panel_limit_kw} is above what the service can carry "
                f"continuously: {capacity_kw:.1f} kW (the lower of the transformer and "
                f"{'100' if self.breaker_100_percent_rated else '80'} % of the main breaker, at "
                f"power factor {self.min_power_factor}). EV charging is a continuous load.")

        _check("chargers.voltage_v", self.charger_voltage_v, low=0.0, low_inclusive=False)
        _check("chargers.max_current_a", self.charger_max_current_a, low=J1772_MIN_CURRENT_A)
        rated_kw = round(self.charger_voltage_v * self.charger_max_current_a, 6) / 1000
        if self.max_charger_kw > rated_kw:
            raise ConfigError(f"chargers.max_charger_kw {self.max_charger_kw} is above the chargers' "
                              f"rating: {self.charger_max_current_a:g} A x {self.charger_voltage_v:g} V "
                              f"= {rated_kw:g} kW")
        j1772_min_kw = round(self.charger_voltage_v * J1772_MIN_CURRENT_A, 6) / 1000
        if self.min_charger_kw < j1772_min_kw:
            raise ConfigError(f"chargers.min_charger_kw {self.min_charger_kw} is below the SAE J1772 "
                              f"minimum of 6 A x {self.charger_voltage_v:g} V = {j1772_min_kw:g} kW")

    @property
    def ct_tolerance_kw(self) -> float:
        """What the CT meter cannot resolve: its accuracy over the usable capacity."""
        return self.ct_accuracy_pct / 100 * (self.panel_limit_kw - self.safety_margin_kw)

    @property
    def continuous_capacity_kw(self) -> float:
        """The most the service can carry continuously, in kW: the lower of the utility
        transformer and the main breaker's continuous rating (80 % unless 100 %-rated),
        times the worst expected power factor (breakers and transformers are limited by
        current, i.e. kVA, while the CT meter reports real power, kW)."""
        phase_factor = math.sqrt(3) if self.service_phases == 3 else 1.0
        breaker_share = 1.0 if self.breaker_100_percent_rated else STANDARD_BREAKER_CONTINUOUS
        breaker_kva = self.service_voltage_v * self.main_breaker_a * phase_factor * breaker_share / 1000
        return min(self.transformer_kva, breaker_kva) * self.min_power_factor

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
        optional = _OPTIONAL.get(section, set())
        present = optional & set(data[section])
        if present and present != optional:
            raise ConfigError(f"{section}: give all of {sorted(optional)} or none "
                              f"(missing {sorted(optional - present)})")
        _exact_keys(section, data[section], keys | present)

    site, chargers, telemetry = data["site"], data["chargers"], data["telemetry"]
    service = data["electrical_service"]
    tariff, grid, server = data["tariff"], data["grid_signal"], data["server"]

    log_path = server["session_log_path"]
    if isinstance(log_path, str) and log_path and not Path(log_path).is_absolute():
        log_path = str(path.resolve().parent / log_path)   # relative to the config file

    return SiteConfig(
        panel_limit_kw=_num("site.panel_limit_kw", site["panel_limit_kw"]),
        safety_margin_kw=_num("site.safety_margin_kw", site["safety_margin_kw"]),
        site_meter_id=site["site_meter_id"],
        timezone=site["timezone"],
        service_voltage_v=_num("electrical_service.voltage_v", service["voltage_v"]),
        service_phases=service["phases"],
        main_breaker_a=_num("electrical_service.main_breaker_a", service["main_breaker_a"]),
        breaker_100_percent_rated=service["breaker_100_percent_rated"],
        transformer_kva=_num("electrical_service.transformer_kva", service["transformer_kva"]),
        min_power_factor=_num("electrical_service.min_power_factor", service["min_power_factor"]),
        charger_voltage_v=_num("chargers.voltage_v", chargers["voltage_v"]),
        charger_max_current_a=_num("chargers.max_current_a", chargers["max_current_a"]),
        max_charger_kw=_num("chargers.max_charger_kw", chargers["max_charger_kw"]),
        min_charger_kw=_num("chargers.min_charger_kw", chargers["min_charger_kw"]),
        min_increase_w=_num("chargers.min_increase_w", chargers["min_increase_w"]),
        taper_threshold_w=_num("chargers.taper_threshold_w", chargers["taper_threshold_w"]),
        taper_grace_s=_num("chargers.taper_grace_s", chargers["taper_grace_s"]),
        offline_session_timeout_s=_num("chargers.offline_session_timeout_s",
                                       chargers["offline_session_timeout_s"]),
        telemetry_stale_after_s=_num("telemetry.stale_after_s", telemetry["stale_after_s"]),
        stale_floor_kw=_num("telemetry.stale_floor_kw", telemetry["stale_floor_kw"]),
        ct_accuracy_pct=_num("telemetry.ct_accuracy_pct", telemetry["ct_accuracy_pct"]),
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
        meter_sample_interval_s=server["meter_sample_interval_s"],
        session_log_path=log_path,
        dashboard_port=server["dashboard_port"],
        peak_demand_rate_per_kw=_optional_num(tariff, "peak_demand_rate_per_kw"),
        peak_energy_rate_per_kwh=_optional_num(tariff, "peak_energy_rate_per_kwh"),
        offpeak_energy_rate_per_kwh=_optional_num(tariff, "offpeak_energy_rate_per_kwh"),
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


def _optional_num(section, key):
    return _num(f"tariff.{key}", section[key]) if key in section else None


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
