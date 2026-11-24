import dataclasses
import random
from datetime import datetime, timezone

import pytest

import gateway
from conftest import OFF_PEAK, PEAK


def by_soc(socs):
    return {cid: gateway.soc_weights(soc) for cid, soc in socs.items()}


# ---------------------------------------------------------------- building budget

def test_quiet_building_leaves_budget(cfg):
    assert gateway.available_ev_budget_kw(35.0, cfg) == 445.0

def test_spare_power_smaller_than_reserve(cfg):
    assert gateway.available_ev_budget_kw(500.0, cfg) == 0.0

def test_over_power_limit(cfg):
    assert gateway.available_ev_budget_kw(750.0, cfg) == 0.0

def test_negative_building_load_gives_zero_budget(cfg):
    assert gateway.available_ev_budget_kw(-35.0, cfg) == 0.0

def test_zero_building_load_gives_full_budget(cfg):
    assert gateway.available_ev_budget_kw(0.0, cfg) == 480.0

def test_demo_building_load_gives_ten_kw(cfg):
    assert gateway.available_ev_budget_kw(470.0, cfg) == 10.0

def test_exactly_at_reserve_gives_zero(cfg):
    assert gateway.available_ev_budget_kw(480.0, cfg) == 0.0

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_garbled_building_load_gives_zero(cfg, bad):
    assert gateway.available_ev_budget_kw(bad, cfg) == 0.0

def test_budget_follows_config(cfg):
    small_site = dataclasses.replace(cfg, panel_limit_kw=200.0, safety_margin_kw=20.0, stale_floor_kw=2.0)
    assert gateway.available_ev_budget_kw(100.0, small_site) == 80.0


# ---------------------------------------------------------------- weights

def test_soc_weights_max():
    assert gateway.soc_weights(100.0) == 0.0

def test_soc_weights_middle():
    assert gateway.soc_weights(50.0) == 1.5

def test_soc_weights_out_of_boundary():
    assert gateway.soc_weights(297.0) == 0.0

@pytest.mark.parametrize("soc, weight", [(0.0, 3.0), (29.99, 3.0), (30.0, 1.5), (79.99, 1.5),
                                         (80.0, 0.5), (99.99, 0.5), (-0.01, 0.0)])
def test_soc_weight_boundaries(soc, weight):
    assert gateway.soc_weights(soc) == weight

@pytest.mark.parametrize("kwh, weight", [(0.0, 3.0), (9.999, 3.0), (10.0, 1.5), (30.0, 1.5),
                                         (30.001, 0.5), (120.0, 0.5)])
def test_energy_weight_tiers(kwh, weight):
    assert gateway.energy_weights(kwh) == weight

@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_energy_weight_unreadable_is_middle_priority(bad):
    assert gateway.energy_weights(bad) == 1.5

def test_session_weight_prefers_soc():
    assert gateway.session_weight(85.0, 0.0) == 0.5          # SoC known: kWh ignored
    assert gateway.session_weight(None, 0.0) == 3.0          # no SoC: kWh tier
    assert gateway.session_weight(None, 35.0) == 0.5


# ---------------------------------------------------------------- allocation

def test_allocate_power_demo_split(cfg):
    shares = gateway.allocate_power(by_soc({'CP_1': 22.0, 'CP_2': 67.0}), 10.0, cfg)
    assert shares == {'CP_1': 6.666, 'CP_2': 3.333}

def test_allocate_power_all_capped(cfg):
    shares = gateway.allocate_power(by_soc({'CP_1': 10.0, 'CP_2': 50.0, 'CP_3': 50.0}), 30.0, cfg)
    assert shares == {'CP_1': 10.0, 'CP_2': 10.0, 'CP_3': 10.0}

def test_allocate_power_redistributes_after_cap(cfg):
    shares = gateway.allocate_power(by_soc({'CP_1': 10.0, 'CP_2': 50.0, 'CP_3': 50.0}), 25.0, cfg)
    assert shares == {'CP_1': 10.0, 'CP_2': pytest.approx(7.5), 'CP_3': pytest.approx(7.5)}

def test_allocate_power_leftover_unused(cfg):
    shares = gateway.allocate_power(by_soc({'CP_1': 10.0, 'CP_2': 50.0}), 30.0, cfg)
    assert shares == {'CP_1': 10.0, 'CP_2': 10.0}
    assert sum(shares.values()) < 30.0

def test_allocate_power_empty(cfg):
    assert gateway.allocate_power({}, 30.0, cfg) == {}

def test_allocate_power_all_zero_weight(cfg):
    assert gateway.allocate_power(by_soc({'CP_1': 100.0, 'CP_2': -1.0}), 30.0, cfg) == {'CP_1': 0.0, 'CP_2': 0.0}

def test_allocate_power_drops_share_below_minimum(cfg):
    # 2.0 kW floor split 3:1.5 would be 1.333 / 0.666: both too small, so the smaller one
    # drops out and the other gets all of it.
    shares = gateway.allocate_power(by_soc({'CP_1': 22.0, 'CP_2': 67.0}), 2.0, cfg)
    assert shares == {'CP_1': 2.0, 'CP_2': 0.0}

def test_allocate_power_budget_below_minimum_gives_nothing(cfg):
    assert gateway.allocate_power(by_soc({'CP_1': 22.0}), 1.399, cfg) == {'CP_1': 0.0}

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -5.0])
def test_allocate_power_bad_budget_gives_nothing(cfg, bad):
    assert gateway.allocate_power(by_soc({'CP_1': 22.0}), bad, cfg) == {'CP_1': 0.0}

def test_allocate_power_tapered_car_capped_and_headroom_redistributed(cfg):
    weights = by_soc({'CP_1': 22.0, 'CP_2': 67.0})
    shares = gateway.allocate_power(weights, 10.0, cfg, caps={'CP_1': cfg.min_charger_kw})
    assert shares == {'CP_1': 1.4, 'CP_2': 8.6}

def test_allocate_power_caps_never_exceed_max(cfg):
    shares = gateway.allocate_power({'CP_1': 3.0}, 50.0, cfg, caps={'CP_1': 99.0})
    assert shares == {'CP_1': 10.0}

@pytest.mark.parametrize("budget", [0.0, 5.0, 25.0, 30.0, 100.0])
def test_allocate_power_invariants(cfg, budget):
    socs = {'CP_1': 10.0, 'CP_2': 50.0, 'CP_3': 90.0, 'CP_4': 20.0}
    shares = gateway.allocate_power(by_soc(socs), budget, cfg)
    assert sum(shares.values()) <= budget + 1e-9
    assert all(v <= cfg.max_charger_kw for v in shares.values())
    assert shares['CP_1'] >= shares['CP_2'] >= shares['CP_3']

def test_allocate_power_randomised_invariants(cfg):
    """5000 random sites: the safety guarantees hold every time."""
    rng = random.Random(1234)
    for _ in range(5000):
        n = rng.randint(1, 12)
        weights = {f"CP_{i}": rng.choice([0.0, 0.5, 1.5, 3.0]) for i in range(n)}
        caps = {cid: rng.choice([cfg.min_charger_kw, cfg.max_charger_kw, rng.uniform(1.4, 10.0)])
                for cid in weights if rng.random() < 0.3}
        budget = rng.choice([0.0, rng.uniform(0.0, 3.0), rng.uniform(0.0, 150.0)])
        shares = gateway.allocate_power(weights, budget, cfg, caps)
        assert set(shares) == set(weights)
        assert sum(shares.values()) <= budget + 1e-9
        for cid, kw in shares.items():
            assert kw == 0.0 or kw >= cfg.min_charger_kw
            assert kw <= min(caps.get(cid, cfg.max_charger_kw), cfg.max_charger_kw) + 1e-9
            if weights[cid] == 0.0:
                assert kw == 0.0
            assert round(kw * 1000, 6) == int(round(kw * 1000, 6))      # whole watts
        uncapped = [cid for cid in weights if cid not in caps]
        for a in uncapped:
            for b in uncapped:
                if weights[a] > weights[b]:
                    assert shares[a] >= shares[b]

def test_appropriate_split_cap_allocations():
    active = {"CP_1": 3.0, "CP_2": 1.5, "CP_3": 0.5}
    caps = {cid: 10.0 for cid in active}
    results = gateway.split_with_cap(active, 150.0, caps)
    assert results == {"CP_1": 10.0, "CP_2": 10.0, "CP_3": 10.0}
    assert 150.0 - sum(results.values()) == 120

def test_split_with_cap_does_not_change_input():
    active = {"CP_1": 3.0}
    gateway.split_with_cap(active, 50.0, {"CP_1": 10.0})
    assert active == {"CP_1": 3.0}

def test_floor_to_watt_removes_float_noise():
    assert gateway.floor_to_watt(1.4 * 3.0 / 3.0) == 1.4
    assert gateway.floor_to_watt(6.6666666) == 6.666


# ---------------------------------------------------------------- budget decision

def test_budget_fresh_telemetry(cfg):
    d = gateway.decide_ev_budget(cfg, 470.0, 1.0, None, None, OFF_PEAK)
    assert (d.budget_kw, d.telemetry_stale, d.cap_source) == (10.0, False, None)

def test_budget_never_received_telemetry_uses_floor(cfg):
    d = gateway.decide_ev_budget(cfg, None, None, None, None, OFF_PEAK)
    assert (d.budget_kw, d.telemetry_stale) == (2.0, True)

def test_budget_stale_after_fifteen_seconds(cfg):
    assert not gateway.decide_ev_budget(cfg, 470.0, 15.0, None, None, OFF_PEAK).telemetry_stale
    d = gateway.decide_ev_budget(cfg, 470.0, 15.001, None, None, OFF_PEAK)
    assert (d.budget_kw, d.telemetry_stale) == (2.0, True)

def test_stale_floor_applies_even_when_building_was_full(cfg):
    # Stale = unknown, so the floor applies rather than the last (possibly zero) budget.
    assert gateway.decide_ev_budget(cfg, 600.0, 20.0, None, None, OFF_PEAK).budget_kw == 2.0

def test_budget_peak_window_caps(cfg):
    d = gateway.decide_ev_budget(cfg, 0.0, 1.0, None, None, PEAK)
    assert (d.budget_kw, d.cap_kw, d.cap_source) == (6.0, 6.0, "peak")

def test_budget_peak_cap_does_not_raise_a_lower_budget(cfg):
    assert gateway.decide_ev_budget(cfg, 470.0, 1.0, None, None, PEAK).budget_kw == 6.0
    assert gateway.decide_ev_budget(cfg, 476.0, 1.0, None, None, PEAK).budget_kw == 4.0

def test_budget_grid_signal_overrides_tariff(cfg):
    d = gateway.decide_ev_budget(cfg, 0.0, 1.0, 50.0, 10.0, PEAK)
    assert (d.budget_kw, d.cap_source) == (50.0, "grid")
    d = gateway.decide_ev_budget(cfg, 470.0, 1.0, 4.0, 10.0, OFF_PEAK)
    assert (d.budget_kw, d.cap_source) == (4.0, "grid")

def test_budget_old_grid_signal_falls_back_to_tariff(cfg):
    d = gateway.decide_ev_budget(cfg, 0.0, 1.0, 50.0, cfg.grid_signal_max_age_s + 1, PEAK)
    assert (d.budget_kw, d.cap_source) == (6.0, "peak")
    d = gateway.decide_ev_budget(cfg, 470.0, 1.0, 4.0, cfg.grid_signal_max_age_s + 1, OFF_PEAK)
    assert (d.budget_kw, d.cap_source) == (10.0, None)

@pytest.mark.parametrize("bad", [float("nan"), -1.0, float("inf")])
def test_budget_invalid_grid_signal_ignored(cfg, bad):
    assert gateway.decide_ev_budget(cfg, 470.0, 1.0, bad, 1.0, OFF_PEAK).cap_source is None

def test_budget_stale_and_capped_takes_lower(cfg):
    assert gateway.decide_ev_budget(cfg, None, None, 1.0, 1.0, OFF_PEAK).budget_kw == 1.0

@pytest.mark.parametrize("local, inside", [("15:59", False), ("16:00", True), ("20:59", True),
                                           ("21:00", False), ("03:00", False)])
def test_peak_window_edges_in_site_time(cfg, local, inside):
    hour, minute = map(int, local.split(":"))
    moment = datetime(2026, 11, 23, hour, minute, tzinfo=cfg.tz)
    assert gateway.in_peak_window(cfg, moment) is inside

def test_peak_window_follows_daylight_saving(cfg):
    # July is PDT (UTC-7): 23:00 UTC is 16:00 local (peak); in November it is 15:00 (not).
    assert gateway.in_peak_window(cfg, datetime(2026, 7, 1, 23, 0, tzinfo=timezone.utc))
    assert not gateway.in_peak_window(cfg, datetime(2026, 11, 23, 23, 0, tzinfo=timezone.utc))

def test_peak_window_rejects_naive_time(cfg):
    with pytest.raises(ValueError):
        gateway.in_peak_window(cfg, datetime(2026, 11, 23, 17, 0))


# ---------------------------------------------------------------- charging profiles

def limit_of(message):
    return gateway.profile_limit_w(message)

def test_negative_allocation_converts_kw_to_watts(cfg):
    assert limit_of(gateway.build_charging_profile(1, -70.0, cfg)) == 0.0

def test_zero_kw_conversion_to_watt(cfg):
    assert limit_of(gateway.build_charging_profile(1, 0.0, cfg)) == 0.0

def test_normal_allocation_converts_kw_to_watts(cfg):
    assert limit_of(gateway.build_charging_profile(1, 11.2, cfg)) == 11200.0

def test_below_minimum_snaps_to_zero(cfg):
    assert limit_of(gateway.build_charging_profile(1, 1.399, cfg)) == 0.0
    assert limit_of(gateway.build_charging_profile(1, 1.4, cfg)) == 1400.0

@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_allocation_sends_zero(cfg, bad):
    assert limit_of(gateway.build_charging_profile(1, bad, cfg)) == 0.0

def test_tx_default_profile_shape(cfg):
    message = gateway.build_charging_profile(1, 6.666, cfg)
    assert message[0] == 2 and message[2] == "SetChargingProfile"
    profile = message[3]["csChargingProfiles"]
    assert message[3]["connectorId"] == 1
    assert profile["chargingProfilePurpose"] == "TxDefaultProfile"
    assert profile["chargingProfileKind"] == "Relative" and profile["stackLevel"] == 0
    assert "transactionId" not in profile
    assert profile["chargingSchedule"] == {"chargingRateUnit": "W",
                                           "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 6666.0}]}

def test_tx_profile_carries_transaction(cfg):
    profile = gateway.build_charging_profile(1, 5.0, cfg, gateway.TX_PROFILE, 42)[3]["csChargingProfiles"]
    assert profile["transactionId"] == 42 and profile["chargingProfilePurpose"] == "TxProfile"

def test_charge_point_max_profile_shape(cfg):
    start = datetime(2026, 11, 23, 18, 0, 5, tzinfo=timezone.utc)
    message = gateway.build_charging_profile(0, 10.0, cfg, gateway.CHARGE_POINT_MAX_PROFILE,
                                             start_schedule=start)
    profile = message[3]["csChargingProfiles"]
    assert message[3]["connectorId"] == 0
    assert profile["chargingProfileKind"] == "Absolute" and profile["stackLevel"] == 0
    assert profile["chargingSchedule"]["startSchedule"] == "2026-11-23T18:00:05Z"

def test_profile_ids_differ_per_purpose(cfg):
    ids = set(gateway.PROFILE_IDS.values())
    assert len(ids) == 3

@pytest.mark.parametrize("kwargs", [
    dict(connector_id=1, purpose="Bogus"),
    dict(connector_id=1, purpose=gateway.TX_PROFILE),                         # no transaction
    dict(connector_id=0, purpose=gateway.TX_PROFILE, transaction_id=1),       # wrong connector
    dict(connector_id=1, purpose=gateway.CHARGE_POINT_MAX_PROFILE,
         start_schedule=datetime(2026, 1, 1, tzinfo=timezone.utc)),
    dict(connector_id=0, purpose=gateway.CHARGE_POINT_MAX_PROFILE),           # no start
    dict(connector_id=-1),
])
def test_invalid_profiles_refused(cfg, kwargs):
    connector_id = kwargs.pop("connector_id")
    with pytest.raises(ValueError):
        gateway.build_charging_profile(connector_id, 5.0, cfg, **kwargs)
