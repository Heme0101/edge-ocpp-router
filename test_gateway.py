import gateway

def test_quiet_building_leaves_budget():
    assert gateway.available_ev_budget_kw(35.0) == 13.0

def test_spare_power_smaller_than_reserve():
    assert gateway.available_ev_budget_kw(49.0) == 0.0

def test_over_power_limit():
    assert gateway.available_ev_budget_kw(55.0) == 0.0

def test_negative_building_load_gives_zero_budget():
    assert gateway.available_ev_budget_kw(-35.0) == 0.0

def test_zero_building_load_gives_full_budget():
    assert gateway.available_ev_budget_kw(0.0) == 48.0

def test_soc_weights_max():
    assert gateway.soc_weights(100.0) == 0.0

def test_soc_weights_middle():
    assert gateway.soc_weights(50.0) == 1.5

def test_soc_weights_out_of_boundary():
    assert gateway.soc_weights(297.0) == 0.0