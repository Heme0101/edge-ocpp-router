import gateway
import pytest

def test_quiet_building_leaves_budget():
    assert gateway.available_ev_budget_kw(35.0) == 445.0

def test_spare_power_smaller_than_reserve():
    assert gateway.available_ev_budget_kw(500.0) == 0.0

def test_over_power_limit():
    assert gateway.available_ev_budget_kw(750.0) == 0.0

def test_negative_building_load_gives_zero_budget():
    assert gateway.available_ev_budget_kw(-35.0) == 0.0

def test_zero_building_load_gives_full_budget():
    assert gateway.available_ev_budget_kw(0.0) == 480.0

def test_soc_weights_max():
    assert gateway.soc_weights(100.0) == 0.0

def test_soc_weights_middle():
    assert gateway.soc_weights(50.0) == 1.5

def test_soc_weights_out_of_boundary():
    assert gateway.soc_weights(297.0) == 0.0

def test_allocate_power_shares_add_up_to_budget():
    shares = gateway.allocate_power({'CP_1': 22.0, 'CP_2': 3.0, 'CP_3' : 67.0, 'CP_4' : 91.0}, 280.0)
    assert sum(shares.values()) == 280.0