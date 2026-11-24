"""Power-allocation math. Pure: numbers in, numbers out. No I/O, no clock, no network.

Site limits come in through a SiteConfig (loaded from site_config.json), never from
constants in this file, so every function can be tested with any site's numbers.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from config import SiteConfig

TX_DEFAULT_PROFILE = "TxDefaultProfile"
TX_PROFILE = "TxProfile"
CHARGE_POINT_MAX_PROFILE = "ChargePointMaxProfile"
# One id per purpose. OCPP 1.6 replaces an installed profile that has the same
# chargingProfileId, so sharing one id would let a ChargePointMaxProfile wipe a TxProfile.
PROFILE_IDS = {TX_DEFAULT_PROFILE: 1, TX_PROFILE: 2, CHARGE_POINT_MAX_PROFILE: 3}


def available_ev_budget_kw(building_load_kw, cfg: SiteConfig):
    """Total kW all the EVs may use: limit - building load - reserve, never negative."""
    if not math.isfinite(building_load_kw) or building_load_kw < 0.0:
        return 0.0                  # a negative or garbled load is a bad reading: no spare power
    if cfg.panel_limit_kw - building_load_kw >= cfg.safety_margin_kw:
        non_idle_energy = building_load_kw + cfg.safety_margin_kw
        return cfg.panel_limit_kw - non_idle_energy
    else:
        return 0.0


def soc_weights(soc):
    """Battery % -> how urgently the car needs power (bigger = bigger share)."""
    if soc < 0:
        return 0.0                  # invalid reading: no power
    if soc < 30.0:
        return 3.0
    elif soc < 80.0:
        return 1.5
    elif soc < 100.0:
        return 0.5
    else:
        return 0.0                  # full (or above 100, invalid): no power


def energy_weights(kwh_delivered):
    """For cars that report no SoC (e.g. J1772 Level-2 AC): energy already delivered this
    session -> priority. A car that has received little is probably the emptiest."""
    if not math.isfinite(kwh_delivered) or kwh_delivered < 0.0:
        return 1.5                  # unreadable: middle priority, neither starved nor favoured
    if kwh_delivered < 10.0:
        return 3.0
    elif kwh_delivered <= 30.0:
        return 1.5
    else:
        return 0.5


def session_weight(soc, kwh_delivered):
    """SoC when the car reports it, otherwise energy delivered this session."""
    return soc_weights(soc) if soc is not None else energy_weights(kwh_delivered)


def floor_to_watt(kw):
    """Round DOWN to a whole watt so rounding can never push the total over the budget.

    round(..., 6) first removes binary floating-point noise. The split computes
    1.4 * 3.0 / 3.0, which comes out as 1.3999999999999997, not exactly 1.4. That would
    floor to 1399 W and wrongly fall under the 1400 W minimum.
    """
    return math.floor(round(kw * 1000, 6)) / 1000


def split_with_cap(active, pool_kw, caps):
    """Split pool_kw among the cars in `active` ({charger_id: weight}) by weight, never
    giving a car more than its own cap (caps: {charger_id: kW}).

    A car whose share is over its cap gets exactly the cap and leaves the game; the
    power it couldn't take is shared among the cars still in play. Repeat until
    nobody is over the cap. Power left over when every car is capped goes unused.
    """
    active = dict(active)           # work on a copy, so the caller's dict isn't changed
    result = {}
    while active:
        total_weight = sum(active.values())
        shares = {cid: pool_kw * w / total_weight for cid, w in active.items()}
        capped = [cid for cid, share in shares.items() if share > caps[cid]]
        if not capped:
            # nobody overflows: these shares are final (pool_kw isn't used again)
            result.update(shares)
            break
        for cid in capped:
            result[cid] = caps[cid]         # fill to the top
            pool_kw -= caps[cid]            # take that power out of the pool
            del active[cid]                 # and stop sharing out to this car
    return result


def allocate_power(weights, budget_kw, cfg: SiteConfig, caps=None):
    """Decide each charger's power in kW.

    weights: {charger_id: priority weight} (see session_weight).
    budget_kw: total kW available to all EVs.
    caps: optional {charger_id: kW} per-car ceiling, e.g. min_charger_kw for a car that
          has tapered off; anything missing or higher is held to max_charger_kw.
    Returns {charger_id: kW}, every car included. Each value is at most its cap, the
    total never exceeds the budget, and a car never gets a share too small to charge
    with (under min_charger_kw): it gets 0.0 and its power goes to the others.
    """
    caps = caps or {}
    car_caps = {cid: min(caps.get(cid, cfg.max_charger_kw), cfg.max_charger_kw) for cid in weights}
    active = {cid: w for cid, w in weights.items() if w > 0.0}     # NaN and <= 0 sit out
    budget_kw = budget_kw if math.isfinite(budget_kw) and budget_kw > 0.0 else 0.0

    while True:
        shares = split_with_cap(active, budget_kw, car_caps)
        shares = {cid: floor_to_watt(kw) for cid, kw in shares.items()}
        too_small = {cid: kw for cid, kw in shares.items() if kw < cfg.min_charger_kw}
        if not too_small:
            break
        # Drop only the car with the smallest share, then redo the split: its power
        # may lift the others above the minimum, so don't drop them all at once.
        del active[min(too_small, key=too_small.get)]

    power_reserved_kw = {cid: 0.0 for cid in weights}   # everyone appears, default 0.0
    power_reserved_kw.update(shares)
    return power_reserved_kw


@dataclass(frozen=True)
class BudgetDecision:
    budget_kw: float            # what the EVs may use in total this cycle
    telemetry_stale: bool       # True: building load unknown, budget held to the safe floor
    cap_kw: float | None        # grid or tariff cap that applied, if any
    cap_source: str | None      # "grid", "peak" or None


def in_peak_window(cfg: SiteConfig, now: datetime) -> bool:
    """Is `now` (timezone-aware, any zone) inside a tariff peak window, in site-local time?"""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(cfg.tz)
    minute = local.hour * 60 + local.minute
    return any(window.contains(minute) for window in cfg.peak_windows)


def decide_ev_budget(cfg: SiteConfig, building_load_kw, telemetry_age_s,
                     grid_signal_cap_kw, grid_signal_age_s, now: datetime) -> BudgetDecision:
    """This cycle's total EV budget.

    1. Building load: fresh CT reading -> panel limit - load - margin. No reading, or one
       older than stale_after_s -> stale_floor_kw: not 0 W (which can fault EVs) and
       not full power (which can trip breakers).
    2. Cap: a live grid signal no older than max_age_s wins; without one (offline),
       the local tariff peak windows apply. The lower of (1) and the cap is used.
    """
    stale = (building_load_kw is None or telemetry_age_s is None
             or telemetry_age_s > cfg.telemetry_stale_after_s)
    base_kw = cfg.stale_floor_kw if stale else available_ev_budget_kw(building_load_kw, cfg)

    grid_ok = (grid_signal_cap_kw is not None and grid_signal_age_s is not None
               and math.isfinite(grid_signal_cap_kw) and grid_signal_cap_kw >= 0.0
               and 0.0 <= grid_signal_age_s <= cfg.grid_signal_max_age_s)
    if grid_ok:
        cap_kw, source = grid_signal_cap_kw, "grid"
    elif in_peak_window(cfg, now):
        cap_kw, source = cfg.peak_budget_cap_kw, "peak"
    else:
        cap_kw, source = None, None

    budget_kw = base_kw if cap_kw is None else min(base_kw, cap_kw)
    return BudgetDecision(budget_kw, stale, cap_kw, source)


def build_charging_profile(connector_id: int, allocated_kw: float, cfg: SiteConfig,
                           purpose: str = TX_DEFAULT_PROFILE, transaction_id: int | None = None,
                           start_schedule: datetime | None = None) -> list:
    """An OCPP 1.6J SetChargingProfile CALL that limits the charger to allocated_kw.

    TxProfile needs the running transaction's id. ChargePointMaxProfile limits the whole
    charger, so it goes to connector 0 and, having no transaction to be relative to, is
    Absolute from start_schedule.
    """
    if purpose not in PROFILE_IDS:
        raise ValueError(f"unknown charging profile purpose {purpose!r}")
    if purpose == TX_PROFILE and transaction_id is None:
        raise ValueError("a TxProfile needs the transaction_id it applies to")
    if purpose == CHARGE_POINT_MAX_PROFILE:
        if connector_id != 0:
            raise ValueError("a ChargePointMaxProfile must go to connector 0")
        if start_schedule is None or start_schedule.tzinfo is None:
            raise ValueError("a ChargePointMaxProfile needs a timezone-aware start_schedule")
    elif connector_id < (1 if purpose == TX_PROFILE else 0):
        raise ValueError(f"invalid connector_id {connector_id} for {purpose}")

    if not math.isfinite(allocated_kw):
        allocated_kw = 0.0              # never put Infinity/NaN on the wire
    watts = round(max(0.0, allocated_kw) * 1000, 1)

    # Below the minimum a charger can't regulate --> snaps cleanly to 0 W
    if watts < round(cfg.min_charger_kw * 1000, 6):
        watts = 0.0

    schedule = {
        "chargingRateUnit": "W",
        "chargingSchedulePeriod": [{"startPeriod": 0, "limit": watts}],
    }
    profile = {
        "chargingProfileId": PROFILE_IDS[purpose],
        "stackLevel": 0,
        "chargingProfilePurpose": purpose,
        "chargingProfileKind": "Relative",
        "chargingSchedule": schedule,
    }
    if purpose == TX_PROFILE:
        profile["transactionId"] = transaction_id
    if purpose == CHARGE_POINT_MAX_PROFILE:
        profile["chargingProfileKind"] = "Absolute"
        schedule["startSchedule"] = utc_iso(start_schedule)

    payload = {"connectorId": connector_id, "csChargingProfiles": profile}
    return [2, str(uuid.uuid4()), "SetChargingProfile", payload]


def profile_limit_w(message) -> float:
    """The watt limit inside a message built by build_charging_profile."""
    schedule = message[3]["csChargingProfiles"]["chargingSchedule"]
    return schedule["chargingSchedulePeriod"][0]["limit"]


def utc_iso(moment: datetime) -> str:
    """2026-11-23T21:04:05Z"""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
