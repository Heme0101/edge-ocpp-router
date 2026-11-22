import uuid

MIN_CHARGE_W = 1400.0
CHARGING_PROFILE_ID = 1

TRANSFORMER_LIMIT_KW = 600.0
ENERGY_RESERVE = 120.0
MAX_CHARGER_KW = 10.0

def available_ev_budget_kw(building_load_kw):
    if building_load_kw < 0.0:
        return 0.0
    if TRANSFORMER_LIMIT_KW - building_load_kw >= ENERGY_RESERVE:
        non_idle_energy = building_load_kw + ENERGY_RESERVE
        return TRANSFORMER_LIMIT_KW  - non_idle_energy
    else:
        return 0.0

def soc_weights(soc):
    if soc < 0:
        return 0.0
    if soc < 30.0:
        return 3.0
    elif soc < 80.0:
        return 1.5
    elif soc < 100.0:
        return 0.5
    else:
        return 0.0

def allocate_power(socs, ev_share):
    """Split ev_share among chargers by SoC weight, capping each at MAX_CHARGER_KW.

    Cars whose weighted share exceeds the cap are fixed at the cap and removed;
    the budget they could not take is redistributed among the rest, repeating
    until nobody exceeds the cap. Weight-0 cars get 0.0. Leftover power goes unused.
    """
    weights = {charger_id: soc_weights(soc) for charger_id, soc in socs.items()}
    power_reserved_kw = {charger_id: 0.0 for charger_id in weights}
    active = {charger_id: w for charger_id, w in weights.items() if w > 0}
    remaining_kw = max(ev_share, 0.0)

    while active:
        total_weight = sum(active.values())
        shares = {cid: remaining_kw * w / total_weight for cid, w in active.items()}
        capped = [cid for cid, share in shares.items() if share > MAX_CHARGER_KW]
        if not capped:
            power_reserved_kw.update(shares)
            break
        for cid in capped:
            power_reserved_kw[cid] = MAX_CHARGER_KW
            remaining_kw -= MAX_CHARGER_KW
            del active[cid]

    return power_reserved_kw

def build_charging_profile(connector_id: int, allocated_kw: float) -> list:
    watts = round(max(0.0, allocated_kw) * 1000, 1)

    # 2. Below the minimum a charger can't regulate --> snaps cleanly to 0 W
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
    building_load_kw = 200.0
    budget_kw = available_ev_budget_kw(building_load_kw)
    shares = allocate_power({'CP_1': 22.0, 'CP_2': 3.0, 'CP_3' : 67.0, 'CP_4' : 100.0}, budget_kw)
    print(f"{budget_kw} kW budget")
    print(shares)