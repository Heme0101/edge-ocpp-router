from itertools import chain

TRANSFORMER_LIMIT_KW = 600.0
ENERGY_RESERVE = 120.0

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
    elif soc < 99.0:
        return 0.5
    elif soc < 100.0:
        return 0.0
    else:
        return 0.0

def allocate_power(socs, budget_kw):
    weights_max = {}
    weights_middle = {}
    weights_low = {}
    power_reserved_kw = {}
    total_weight = 0

    for charger_id, soc in socs.items():
        weight = soc_weights(soc)
        if weight == 3.0:
            total_weight += 3.0
            weights_max.update({charger_id : soc})
        elif weight == 1.5:
            total_weight += 1.5
            weights_middle.update({charger_id : soc})
        elif weight == 0.5:
            weights_low.update({charger_id : soc})
            total_weight += 0.5

    ev_share = available_ev_budget_kw(budget_kw)

    ev_share_for_max_weight = ev_share * 3.0 / total_weight if len(weights_max) > 0 else 0
    ev_share_for_middle_weight = ev_share * 1.5 / total_weight if len(weights_middle) > 0 else 0
    ev_share_for_low_weight = ev_share * 0.5 / total_weight if len(weights_low) > 0 else 0

    for charger_id, soc in chain(weights_max.items(), weights_middle.items(), weights_low.items()):
        if soc in weights_max.values():
            power_reserved_kw[charger_id] = ev_share_for_max_weight
        elif soc in weights_middle.values():
            power_reserved_kw[charger_id] = ev_share_for_middle_weight
        elif soc in weights_low.values():
            power_reserved_kw[charger_id] = ev_share_for_low_weight

    return power_reserved_kw

if __name__ == "__main__":
    print(f"{available_ev_budget_kw(200.0)} kw is available for the EVs")