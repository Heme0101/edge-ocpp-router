TRANSFORMER_LIMIT_KW = 600.0
ENERGY_RESERVE = 120.0
ev_share = 0

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
    weights = {charger_id: soc_weights(soc) for charger_id, soc in socs.items()}
    total_weight = sum(weights.values())

    if total_weight == 0:
        return {charger_id: 0.0 for charger_id in weights}

    power_reserved_kw = {}
    for charger_id, weight in weights.items():
        power_reserved_kw[charger_id] = ev_share * weight / total_weight

    return power_reserved_kw

if __name__ == "__main__":
    building_load_kw = 200.0
    budget_kw = available_ev_budget_kw(building_load_kw)
    shares = allocate_power({'CP_1': 22.0, 'CP_2': 3.0, 'CP_3' : 67.0, 'CP_4' : 100.0}, budget_kw)
    print(f"{budget_kw} kW budget")
    print(shares)