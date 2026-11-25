import dataclasses
from datetime import datetime, timezone

import pytest

from config import load_site_config

# Site time zone is America/Los_Angeles; in November that is PST (UTC-8).
OFF_PEAK = datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc)    # 10:00 local
PEAK = datetime(2026, 11, 24, 1, 0, tzinfo=timezone.utc)         # 17:00 local


@pytest.fixture
def cfg():
    """A fixed, physically valid test site, independent of the shipped site_config.json
    (which describes one real installation and may change): a large commercial service,
    480 V 3-phase, 1600 A main breaker, 1000 kVA transformer -> 958 kW continuous at PF
    0.9; limit 600 kW with a 120 kW margin; 48 A chargers at 240 V capped at 10 kW.
    CT accuracy 0 % here, so every cut is sent (tests of holding small cuts set it)."""
    return dataclasses.replace(
        load_site_config(),
        panel_limit_kw=600.0, safety_margin_kw=120.0,
        service_voltage_v=480.0, service_phases=3, main_breaker_a=1600.0,
        breaker_100_percent_rated=False, transformer_kva=1000.0, min_power_factor=0.9,
        charger_voltage_v=240.0, charger_max_current_a=48.0,
        max_charger_kw=10.0, min_charger_kw=1.44, min_increase_w=100.0,
        taper_threshold_w=100.0, taper_grace_s=30.0, offline_session_timeout_s=300.0,
        telemetry_stale_after_s=15.0, stale_floor_kw=2.0, ct_accuracy_pct=0.0,
        peak_budget_cap_kw=5.0,
        session_log_path=None)
