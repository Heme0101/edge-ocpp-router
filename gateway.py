import math
import uuid

MIN_CHARGE_W = 1400.0               # a charger can't regulate below ~6 A, so below this we send 0
MIN_CHARGE_KW = MIN_CHARGE_W / 1000.0
CHARGING_PROFILE_ID = 1             # reusing one id means a new profile replaces the old one

TRANSFORMER_LIMIT_KW = 600.0        # the most the whole building can draw
ENERGY_RESERVE = 120.0              # safety margin kept free below the limit
MAX_CHARGER_KW = 10.0               # the most one charger can deliver (the cap)


def available_ev_budget_kw(building_load_kw):
    """Total kW all the EVs may use: limit - building load - reserve, never negative."""
    if building_load_kw < 0.0:
        return 0.0                  # a negative load is a bad reading: assume no spare power
    if TRANSFORMER_LIMIT_KW - building_load_kw >= ENERGY_RESERVE:
        non_idle_energy = building_load_kw + ENERGY_RESERVE
        return TRANSFORMER_LIMIT_KW - non_idle_energy
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


def floor_to_watt(kw):
    """Round DOWN to a whole watt so rounding can never push the total over the budget.

    round(..., 6) first removes binary floating-point noise. The split computes
    1.4 * 3.0 / 3.0, which comes out as 1.3999999999999997, not exactly 1.4. That would
    floor to 1399 W and wrongly fall under the 1400 W minimum.
    """
    return math.floor(round(kw * 1000, 6)) / 1000


def split_with_cap(active, pool_kw):
    """Split pool_kw among the cars in `active` ({charger_id: weight}) by weight,
    never giving one car more than MAX_CHARGER_KW.

    A car whose share is over the cap gets exactly the cap and leaves the game; the
    power it couldn't take is shared among the cars still in play. Repeat until
    nobody is over the cap. Power left over when every car is capped goes unused.
    """
    active = dict(active)           # work on a copy, so the caller's dict isn't changed
    result = {}
    while active:
        total_weight = sum(active.values())
        shares = {cid: pool_kw * w / total_weight for cid, w in active.items()}
        capped = [cid for cid, share in shares.items() if share > MAX_CHARGER_KW]
        if not capped:
            # nobody overflows: these shares are final (pool_kw isn't used again)
            result.update(shares)
            break
        for cid in capped:
            result[cid] = MAX_CHARGER_KW    # fill to the top
            pool_kw -= MAX_CHARGER_KW       # take that power out of the pool
            del active[cid]                 # and stop sharing out to this car
    return result


def allocate_power(socs, budget_kw):
    """Decide each charger's power in kW.

    socs: {charger_id: battery %}.  budget_kw: total kW available to all EVs.
    Returns {charger_id: kW}, every car included. Each value is at most MAX_CHARGER_KW,
    the total never exceeds the budget, and a car never gets a share too small to
    charge with (under MIN_CHARGE_KW): it gets 0.0 and its power goes to the others.
    """
    weights = {charger_id: soc_weights(soc) for charger_id, soc in socs.items()}
    active = {cid: w for cid, w in weights.items() if w > 0.0}
    budget_kw = max(budget_kw, 0.0)

    while True:
        shares = split_with_cap(active, budget_kw)
        shares = {cid: floor_to_watt(kw) for cid, kw in shares.items()}
        too_small = {cid: kw for cid, kw in shares.items() if kw < MIN_CHARGE_KW}
        if not too_small:
            break
        # Drop only the car with the smallest share, then redo the split: its power
        # may lift the others above the minimum, so don't drop them all at once.
        del active[min(too_small, key=too_small.get)]

    power_reserved_kw = {cid: 0.0 for cid in weights}   # everyone appears, default 0.0
    power_reserved_kw.update(shares)
    return power_reserved_kw


def build_charging_profile(connector_id: int, allocated_kw: float) -> list:
    watts = round(max(0.0, allocated_kw) * 1000, 1)

    if watts < MIN_CHARGE_W:
        watts = 0.0

    payload = {
        "connectorId": connector_id,
        "csChargingProfiles": {
            "chargingProfileId": CHARGING_PROFILE_ID,
            "stackLevel": 0,
            "chargingProfilePurpose": "TxDefaultProfile",
            "chargingProfileKind": "Relative",
            "chargingSchedule": {
                "chargingRateUnit": "W",
                "chargingSchedulePeriod": [{"startPeriod": 0, "limit": watts}],
            },
        },
    }

    return [2, str(uuid.uuid4()), "SetChargingProfile", payload]


if __name__ == "__main__":
    building_load_kw = 470.0
    budget_kw = available_ev_budget_kw(building_load_kw)
    shares = allocate_power({'CP_1': 22.0, 'CP_2': 67.0}, budget_kw)
    unused_kw = budget_kw - sum(shares.values())
    print(f"{budget_kw} kW reserved for EVs.")
    print(shares)
    print(f"Energy for EVs left over: {unused_kw:.3f} kW")