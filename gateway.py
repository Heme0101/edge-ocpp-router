TRANSFORMER_LIMIT_KW = 50.0
ENERGY_RESERVE = 2.0

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


if __name__ == "__main__":
    print(f"{available_ev_budget_kw(35.0)} kw is available for the EVs")