"""Power-allocation math. Pure: numbers in, numbers out. No I/O, no clock, no network.

Site limits come in through a SiteConfig (loaded from site_config.json), never from
constants in this file, so every function can be tested with any site's numbers.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from fractions import Fraction
from datetime import datetime, timezone

from config import SiteConfig

TX_DEFAULT_PROFILE = "TxDefaultProfile"
TX_PROFILE = "TxProfile"
CHARGE_POINT_MAX_PROFILE = "ChargePointMaxProfile"
# One id per purpose. OCPP 1.6 replaces an installed profile that has the same
# chargingProfileId, so sharing one id would let a ChargePointMaxProfile wipe a TxProfile.
PROFILE_IDS = {TX_DEFAULT_PROFILE: 1, TX_PROFILE: 2, CHARGE_POINT_MAX_PROFILE: 3}
# The site-wide default (TxDefaultProfile on connector 0) has its own id, and per-connector
# TxDefaultProfiles sit one stack level higher, so neither ever replaces the other and the
# connector-specific one takes precedence.
SITE_DEFAULT_PROFILE_ID = 4


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


def watts_floor(kw) -> int:
    """kW -> whole watts, rounded DOWN, so rounding can never push a total over a budget.

    Floats are binary: 1.4 is stored as 1.39999999999999991, and 600 - (470.1 + 120) comes
    out as 9.899999999999977. round(..., 6) first strips that representation noise
    (it moves a value by at most half a microwatt), then floor() rounds down.
    """
    return math.floor(round(kw * 1000, 6))


def watts_ceil(kw) -> int:
    """kW -> whole watts, rounded UP: for minimums, so 'at least the minimum' stays true."""
    return math.ceil(round(kw * 1000, 6))


def split_with_cap(active, pool_kw, caps):
    """Split pool_kw among the cars in `active` ({charger_id: weight}) by weight, never
    giving a car more than its own cap (caps: {charger_id: kW}).

    A car whose share is over its cap gets exactly the cap and leaves the game; the
    power it couldn't take is shared among the cars still in play. Repeat until
    nobody is over the cap. Power left over when every car is capped goes unused.
    Works on any number type; allocate_power passes exact integers and fractions.
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


def allocate_power(weights, budget_kw, cfg: SiteConfig, caps=None, arrival=None):
    """Decide each charger's power in kW.

    weights: {charger_id: priority weight} (see session_weight).
    budget_kw: total kW available to all EVs.
    caps: optional {charger_id: kW} per-car ceiling, e.g. min_charger_kw for a car that
          has tapered off; anything missing or higher is held to max_charger_kw.
    arrival: optional {charger_id: number}, e.g. plug-in time. When two cars have the
          same weight and only one can charge, the one that arrived FIRST keeps charging
          (first come, first served), so no car is starved by dictionary order and the
          choice does not flip back and forth between cycles. Without it: listing order.
    Returns {charger_id: kW}, every car included. Each value is a whole number of watts,
    at most its cap; the total never exceeds the budget; and a car never gets a share too
    small to charge with (under min_charger_kw): it gets 0.0 and its power goes to the others.

    The arithmetic is exact: watts are integers and weights are Fractions, so no binary
    rounding error can creep into a share. Each share is rounded down to a whole watt.
    """
    caps = caps or {}
    max_w = watts_floor(cfg.max_charger_kw)
    min_w = watts_ceil(cfg.min_charger_kw)
    car_caps = {cid: min(watts_floor(caps.get(cid, cfg.max_charger_kw)), max_w) for cid in weights}
    budget_w = watts_floor(budget_kw) if math.isfinite(budget_kw) and budget_kw > 0.0 else 0
    listed = {cid: i for i, cid in enumerate(weights)}
    # NaN, inf and <= 0 weights sit out, and so does a car whose cap is below the minimum.
    candidates = [cid for cid, w in weights.items()
                  if math.isfinite(w) and w > 0.0 and car_caps[cid] >= min_w]
    # Priority order: highest weight first; equal weights, first come first served.
    candidates.sort(key=lambda cid: (-weights[cid],
                                     arrival[cid] if arrival is not None else listed[cid]))

    def split(count):
        active = {cid: Fraction(weights[cid]) for cid in candidates[:count]}
        return {cid: math.floor(w) for cid, w in split_with_cap(active, budget_w, car_caps).items()}

    # A car whose share is too small to charge on (under min_charger_kw) sits out and its
    # power goes to the others. Shares are proportional to weight (or held at a cap that
    # is >= the minimum), so the cars that sit out are always the LAST ones in priority
    # order, and if the first k cars all get enough, so do the first k-1. So the cars that
    # charge are the longest prefix that works, found by binary search: the same answer
    # as dropping the smallest share one at a time, in O(log n) splits instead of O(n).
    low, high = 0, len(candidates)
    while low < high:
        middle = (low + high + 1) // 2
        if all(w >= min_w for w in split(middle).values()):
            low = middle
        else:
            high = middle - 1
    shares = split(low)

    power_reserved_kw = {cid: 0.0 for cid in weights}   # everyone appears, default 0.0
    power_reserved_kw.update({cid: w / 1000 for cid, w in shares.items()})
    return power_reserved_kw


def estimate_other_load_kw(site_total_kw, ev_draws_kw, ev_draws_high_kw=None, previous_kw=None):
    """Everything on the site that is not an EV, from the main-feeder CT reading.

    The CT measures the whole site INCLUDING the chargers, so the chargers' draw is
    subtracted. Each car's draw at the CT instant is only known within bounds: low
    (ev_draws_kw) and high (ev_draws_high_kw). They differ when a car's limit changed
    after its last power reading, since it may or may not have followed yet. So the
    other loads are known to lie in the band

        [site_total - sum(high), site_total - sum(low)].

    Without a previous estimate, the top of the band is used: the most the other loads
    can be, which is the safe side. With one, the previous estimate is kept but clamped
    into the band, so it moves only when the CT reading proves the other loads moved.
    A CT rise fully explained by our own raise changes nothing. When every reading is
    current, the band is a single value and the estimate is exact, with no lag.
    A negative result (meters briefly disagreeing) is treated as 0. The result is rounded
    UP to a whole watt: a bigger other load means a smaller EV budget, the safe direction.
    """
    upper = site_total_kw - sum(ev_draws_kw)
    lower = upper if ev_draws_high_kw is None else site_total_kw - sum(ev_draws_high_kw)
    estimate = upper if previous_kw is None else min(max(previous_kw, lower), upper)
    return max(0, watts_ceil(estimate)) / 1000


def hold_small_cuts(send_w, committed_w, accepted_w, tolerance_w):
    """Skip cuts too small to matter, bounded across the whole site.

    A cut smaller than the CT meter's own accuracy only chases measurement noise and
    costs a message per charger per reading. Cuts are held (the car keeps its accepted
    limit) smallest first, as long as the TOTAL held back across all chargers stays
    within tolerance_w (the meter's accuracy). Anything beyond that, including any cut
    that stops a car, goes out at once. Only a car whose accepted limit is known and
    has nothing newer in flight can be held. Integer watts.
    """
    result = dict(send_w)
    cuts = sorted((committed_w[key] - watts, str(key), key) for key, watts in send_w.items()
                  if watts < committed_w[key] and accepted_w.get(key) == committed_w[key])
    held = 0
    for size, _, key in cuts:
        if held + size > tolerance_w:
            break
        result[key] = committed_w[key]
        held += size
    return result


def transition_safe_limits(targets_w, committed_w, budget_w, reserved_w, priority, min_w):
    """The limits that are safe to send RIGHT NOW on the way to `targets_w`.

    A charger only lowers its draw once it has accepted a new limit, so until then every
    car may still draw what it is committed to (its accepted limit, or a pending higher
    one). Decreases are therefore sent at once and in full; increases are granted only
    from headroom that exists while every other car still draws its committed amount
    (pending decreases are NOT counted as freed). Power freed by a decrease is handed out
    on the next cycle, once the decrease is confirmed. Highest priority is served first;
    a partial increase is allowed, except one that would leave a car below min_w (it
    could not charge on it, so that headroom is left for the next car).

    All values are integer watts. Guarantees: sum(result) + reserved_w <= budget_w
    whenever sum(committed) + reserved_w <= budget_w, and no car's limit ever rises
    while that would not hold.
    """
    result, increases = {}, []
    for key, target in targets_w.items():
        if target <= committed_w[key]:
            result[key] = target                    # decrease (or no change): always safe
        else:
            increases.append(key)
            result[key] = committed_w[key]          # stays put unless headroom allows more
    headroom = budget_w - reserved_w - sum(committed_w.values())
    for key in sorted(increases, key=lambda k: (-priority[k], str(k))):
        if headroom <= 0:
            break
        grant = min(targets_w[key] - committed_w[key], headroom)
        if result[key] + grant < min_w:
            continue
        result[key] += grant
        headroom -= grant
    return result


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
    Absolute from start_schedule. A TxDefaultProfile on connector 0 is the site default
    every new session starts under (see SITE_DEFAULT_PROFILE_ID).
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
    watts = float(watts_floor(max(0.0, allocated_kw)))     # whole watts, rounded down

    # Below the minimum a charger can't regulate --> snaps cleanly to 0 W
    if watts < watts_ceil(cfg.min_charger_kw):
        watts = 0.0

    schedule = {
        "chargingRateUnit": "W",
        "chargingSchedulePeriod": [{"startPeriod": 0, "limit": watts}],
    }
    site_default = purpose == TX_DEFAULT_PROFILE and connector_id == 0
    profile = {
        "chargingProfileId": SITE_DEFAULT_PROFILE_ID if site_default else PROFILE_IDS[purpose],
        "stackLevel": 1 if purpose == TX_DEFAULT_PROFILE and not site_default else 0,
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
