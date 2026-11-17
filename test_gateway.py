from gateway import available_ev_budget_kw

def test_quiet_building_leaves_budget():
    assert available_ev_budget_kw(35.0) == 13.0

def test_spare_power_smaller_than_reserve():
    assert available_ev_budget_kw(49.0) == 0

def test_over_power_limit():
    assert available_ev_budget_kw(55.0) == 0