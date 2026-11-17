TRANSFORMER_LIMIT_KW = 50.0
ENERGY_RESERVE = 2.0

def available_ev_budget_kw(building_load_kw):
    if TRANSFORMER_LIMIT_KW - building_load_kw >= ENERGY_RESERVE:
        non_idle_energy = building_load_kw + ENERGY_RESERVE
        return TRANSFORMER_LIMIT_KW  - non_idle_energy
    else:
        return 0

if __name__ == "__main__":
    print(f"{available_ev_budget_kw(35.0)} kw is available for the EVs")