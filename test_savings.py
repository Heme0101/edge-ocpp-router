"""Tariff savings: the pure math in gateway.py and its use by the CentralSystem."""
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

import gateway
from conftest import OFF_PEAK, PEAK
from test_server import FakeCharger, cycle, make, measure, plug, run, session

# PEAK is 17:00 local (America/Los_Angeles, PST); OFF_PEAK is 10:00 local.


def tracker(cfg, max_gap_s=10_000.0):
    return gateway.SavingsTracker(cfg, max_gap_s=max_gap_s)


# ---------------------------------------------------------------- rate math

@pytest.mark.parametrize("dollars, cents", [(1.005, 1.0), (0.1 + 0.2, 0.3), (2.999, 2.99),
                                            (0.0, 0.0), (1250.0, 1250.0)])
def test_floor_cents_rounds_down_and_strips_float_noise(dollars, cents):
    assert gateway.floor_cents(dollars) == cents


def test_demand_charge_savings():
    assert gateway.demand_charge_savings(10.0, 25.0) == 250.0
    assert gateway.demand_charge_savings(0.0123, 25.0) == 0.3        # 0.3075 -> down to the cent
    assert gateway.demand_charge_savings(-5.0, 25.0) == 0.0          # never negative


def test_energy_shift_savings_uses_the_rate_difference():
    assert gateway.energy_shift_savings(10.0, 0.42, 0.18) == 2.4     # 10 kWh x $0.24
    assert gateway.energy_shift_savings(1.0, 0.42, 0.18) == 0.24
    assert gateway.energy_shift_savings(-1.0, 0.42, 0.18) == 0.0
    assert gateway.energy_shift_savings(10.0, 0.30, 0.30) == 0.0     # flat rate: nothing gained


def test_unmanaged_ev_kw(cfg):
    assert gateway.unmanaged_ev_kw(2.5, cfg, drawing=True) == cfg.max_charger_kw   # 10 kW charger
    assert gateway.unmanaged_ev_kw(0.0, cfg, drawing=True) == cfg.max_charger_kw
    assert gateway.unmanaged_ev_kw(0.05, cfg, drawing=False) == 0.05   # full/tapered: as is
    assert gateway.unmanaged_ev_kw(12.0, cfg, drawing=True) == 12.0     # never below actual


# ---------------------------------------------------------------- SavingsTracker

def test_first_sample_counts_no_time(cfg):
    t = tracker(cfg)
    assert t.record(0.0, PEAK, True, 100.0, 50.0) == 0.0
    assert t.month_shifted_kwh == 0.0
    assert t.shaved_now_kw == 50.0


def test_peak_sample_counts_energy_and_demand(cfg):
    t = tracker(cfg)
    t.record(0.0, PEAK, True, 100.0, 50.0)
    hours = t.record(360.0, PEAK + timedelta(minutes=6), True, 100.0, 50.0)
    assert hours == pytest.approx(0.1)
    assert t.month_shifted_kwh == pytest.approx(5.0)                 # 50 kW x 0.1 h
    assert t.month_peaks_kw() == pytest.approx((100.0, 150.0))      # interval in progress counts
    snap = t.snapshot()
    month = snap["month"]
    assert month["month"] == "2026-11"
    assert month["peak_kw_shaved"] == 50.0
    assert month["demand_savings_usd"] == 1250.0                     # 50 kW x $25
    assert month["energy_savings_usd"] == 1.2                        # 5 kWh x $0.24
    assert month["total_usd"] == 1251.2
    assert snap["run"]["energy_shifted_kwh"] == 5.0 and snap["run"]["energy_savings_usd"] == 1.2
    assert snap["run"]["max_shaved_kw"] == 50.0 and snap["shaved_now_kw"] == 50.0


def test_off_peak_counts_nothing(cfg):
    t = tracker(cfg)
    t.record(0.0, OFF_PEAK, False, 100.0, 50.0)
    assert t.record(600.0, OFF_PEAK + timedelta(minutes=10), False, 100.0, 50.0) == 0.0
    assert t.month_shifted_kwh == 0.0 and t.month_peaks_kw() == (0.0, 0.0)
    assert t.shaved_now_kw == 0.0
    assert t.snapshot()["month"]["total_usd"] == 0.0


def test_stale_or_bad_ct_counts_nothing(cfg):
    t = tracker(cfg)
    t.record(0.0, PEAK, True, 100.0, 50.0)
    assert t.record(60.0, PEAK + timedelta(minutes=1), True, None, 50.0) == 0.0
    assert t.record(120.0, PEAK + timedelta(minutes=2), True, -5.0, 50.0) == 0.0
    assert t.record(180.0, PEAK + timedelta(minutes=3), True, float("nan"), 50.0) == 0.0
    assert t.month_shifted_kwh == 0.0


def test_nan_or_negative_extra_is_treated_as_zero(cfg):
    t = tracker(cfg)
    t.record(0.0, PEAK, True, 100.0, 0.0)
    t.record(360.0, PEAK + timedelta(minutes=6), True, 100.0, float("nan"))
    t.record(720.0, PEAK + timedelta(minutes=12), True, 100.0, -20.0)
    assert t.month_shifted_kwh == 0.0
    assert t.month_peaks_kw() == pytest.approx((100.0, 100.0))


def test_gap_is_capped(cfg):
    """A long pause (suspended machine, blocked loop) is not counted as constant load."""
    t = tracker(cfg, max_gap_s=10.0)
    t.record(0.0, PEAK, True, 100.0, 36.0)
    assert t.record(5000.0, PEAK + timedelta(seconds=10), True, 100.0, 36.0) == pytest.approx(10 / 3600)
    assert t.month_shifted_kwh == pytest.approx(0.1)


def test_backwards_monotonic_time_counts_nothing(cfg):
    t = tracker(cfg)
    t.record(100.0, PEAK, True, 100.0, 50.0)
    assert t.record(50.0, PEAK, True, 100.0, 50.0) == 0.0


def test_demand_is_the_highest_15_minute_average_each_way(cfg):
    """Billing uses each case's own monthly maximum, so the two maxima may come from
    different intervals."""
    t = tracker(cfg)
    t.record(0.0, PEAK, True, 100.0, 50.0)
    t.record(600.0, PEAK + timedelta(minutes=10), True, 100.0, 50.0)       # 17:00-17:15: 100 / 150
    t.record(1200.0, PEAK + timedelta(minutes=20), True, 200.0, 0.0)       # 17:15-17:30: 200 / 200
    assert t.month_peaks_kw() == pytest.approx((200.0, 200.0))
    assert t.snapshot()["month"]["peak_kw_shaved"] == 0.0
    assert t.snapshot()["month"]["demand_savings_usd"] == 0.0


def test_interval_average_is_time_weighted(cfg):
    t = tracker(cfg)
    t.record(0.0, PEAK, True, 100.0, 0.0)
    t.record(300.0, PEAK + timedelta(minutes=5), True, 100.0, 0.0)     # 5 min at 100 / 100
    t.record(600.0, PEAK + timedelta(minutes=10), True, 100.0, 30.0)   # 5 min at 100 / 130
    assert t.month_peaks_kw() == pytest.approx((100.0, 115.0))


def test_month_resets_in_site_local_time(cfg):
    # 2026-12-01 00:30 UTC is still November 30, 16:30 in Los Angeles.
    nov_30 = datetime(2026, 12, 1, 0, 30, tzinfo=timezone.utc)
    dec_1 = datetime(2026, 12, 2, 0, 30, tzinfo=timezone.utc)
    t = tracker(cfg, max_gap_s=360.0)
    t.record(0.0, nov_30, True, 100.0, 50.0)
    t.record(360.0, nov_30 + timedelta(minutes=6), True, 100.0, 50.0)
    assert t.snapshot()["month"]["month"] == "2026-11"
    t.record(86_400.0, dec_1, True, 100.0, 10.0)
    snap = t.snapshot()
    assert snap["month"]["month"] == "2026-12"
    # November's 5 kWh and 50 kW are gone; only December's first sample (10 kW over the
    # capped 360 s gap = 1 kWh) counts.
    assert snap["month"]["energy_shifted_kwh"] == 1.0 and snap["month"]["peak_kw_shaved"] == 10.0
    assert snap["run"]["energy_shifted_kwh"] == 6.0                 # the run keeps counting


def test_without_rates_dollars_are_none(cfg):
    cfg = dataclasses.replace(cfg, peak_demand_rate_per_kw=None, peak_energy_rate_per_kwh=None,
                              offpeak_energy_rate_per_kwh=None)
    t = tracker(cfg)
    t.record(0.0, PEAK, True, 100.0, 50.0)
    t.record(360.0, PEAK + timedelta(minutes=6), True, 100.0, 50.0)
    snap = t.snapshot()
    assert snap["enabled"] is False
    assert snap["month"]["energy_shifted_kwh"] == 5.0 and snap["month"]["peak_kw_shaved"] == 50.0
    assert snap["month"]["total_usd"] is None and snap["run"]["energy_savings_usd"] is None
    assert t.energy_usd(5.0) is None


# ---------------------------------------------------------------- in the CentralSystem

def two_cars(cfg, wall):
    from test_server import Clock
    cs, clock = make(cfg, Clock(wall))
    for cid, soc in (("CP_1", 20.0), ("CP_2", 60.0)):
        FakeCharger(cs, cid)
        plug(cs, cid, soc=soc)
    return cs, clock


def drive(cs, clock, cycles, other_kw=100.0):
    for _ in range(cycles):
        measure(cs, other_kw)
        run(cycle(cs))
        clock.advance(1.0)


def test_peak_window_cycles_record_savings(cfg):
    cs, clock = two_cars(cfg, PEAK)
    drive(cs, clock, 6)
    s1, s2 = session(cs, "CP_1"), session(cs, "CP_2")
    assert s1.accepted_w + s2.accepted_w <= 5000                    # held to the 5 kW peak cap
    assert cs.savings.run_shifted_kwh > 0.0
    # Split among the sessions exactly as counted for the site.
    assert s1.peak_shift_kwh + s2.peak_shift_kwh == pytest.approx(cs.savings.run_shifted_kwh)
    # Unmanaged both would draw 10 kW (20 kW); managed they draw at most 5 kW.
    assert cs.savings.shaved_now_kw >= 15.0
    assert cs.last_plan is not None and cs.last_plan.in_peak


def test_off_peak_cycles_record_no_savings(cfg):
    cs, clock = two_cars(cfg, OFF_PEAK)
    drive(cs, clock, 6)
    assert cs.savings.run_shifted_kwh == 0.0
    assert session(cs, "CP_1").peak_shift_kwh == 0.0
    assert not cs.last_plan.in_peak


def test_session_record_includes_shifted_energy(cfg):
    cs, clock = two_cars(cfg, PEAK)
    drive(cs, clock, 4)
    shifted = session(cs, "CP_1").peak_shift_kwh
    cs.close_all_sessions("Test")
    record = next(r for r in cs.completed_sessions if r["charger_id"] == "CP_1")
    assert record["peak_shifted_kwh"] == round(shifted, 3)


def test_savings_failure_never_stops_the_cycle(cfg, monkeypatch, caplog):
    cs, clock = two_cars(cfg, PEAK)

    def broken(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(cs.savings, "record", broken)
    measure(cs, 100.0)
    tasks = run(cs.run_cycle())
    assert tasks                                    # limits still went out
    assert "savings tracking failed" in caplog.text


# ---------------------------------------------------------------- peak override

def test_peak_override_forces_the_window(cfg):
    from test_server import Clock
    cs, clock = make(cfg, Clock(OFF_PEAK))
    measure(cs, 100.0)
    assert cs.plan().decision.cap_source is None
    cs.set_peak_override(True)
    plan = cs.plan()
    assert plan.in_peak and plan.decision.cap_source == "peak" and plan.decision.budget_kw == 5.0
    cs.set_peak_override(None)
    assert not cs.plan().in_peak


def test_peak_override_off_during_the_real_window(cfg):
    from test_server import Clock
    cs, clock = make(cfg, Clock(PEAK))
    measure(cs, 100.0)
    assert cs.plan().in_peak
    cs.set_peak_override(False)
    assert cs.plan().decision.cap_source is None


def test_live_grid_signal_still_wins_over_a_forced_peak(cfg):
    from test_server import Clock
    cs, clock = make(cfg, Clock(OFF_PEAK), grid_signal_cap_kw=50.0)
    measure(cs, 100.0)
    cs.set_peak_override(True)
    assert cs.plan().decision.cap_source == "grid"


def test_peak_override_rejects_non_bool(cfg):
    cs, _ = make(cfg)
    with pytest.raises(TypeError):
        cs.set_peak_override("yes")


def test_decide_ev_budget_peak_argument(cfg):
    assert gateway.decide_ev_budget(cfg, 100.0, 1.0, None, None, OFF_PEAK, peak=True).cap_source == "peak"
    assert gateway.decide_ev_budget(cfg, 100.0, 1.0, None, None, PEAK, peak=False).cap_source is None
    assert gateway.decide_ev_budget(cfg, 100.0, 1.0, None, None, PEAK).cap_source == "peak"
