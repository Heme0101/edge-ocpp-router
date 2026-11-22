import pytest
import gateway

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

def test_allocate_power_all_capped():
    shares = gateway.allocate_power({'CP_1': 10.0, 'CP_2': 50.0, 'CP_3': 50.0}, 30.0)
    assert shares == {'CP_1': 10.0, 'CP_2': 10.0, 'CP_3': 10.0}

def test_allocate_power_redistributes_after_cap():
    shares = gateway.allocate_power({'CP_1': 10.0, 'CP_2': 50.0, 'CP_3': 50.0}, 25.0)
    assert shares == {'CP_1': 10.0, 'CP_2': pytest.approx(7.5), 'CP_3': pytest.approx(7.5)}

def test_allocate_power_leftover_unused():
    shares = gateway.allocate_power({'CP_1': 10.0, 'CP_2': 50.0}, 30.0)
    assert shares == {'CP_1': 10.0, 'CP_2': 10.0}
    assert sum(shares.values()) < 30.0

def test_allocate_power_empty():
    assert gateway.allocate_power({}, 30.0) == {}

def test_allocate_power_all_zero_weight():
    assert gateway.allocate_power({'CP_1': 100.0, 'CP_2': -1.0}, 30.0) == {'CP_1': 0.0, 'CP_2': 0.0}

@pytest.mark.parametrize("budget", [0.0, 5.0, 25.0, 30.0, 100.0])
def test_allocate_power_invariants(budget):
    socs = {'CP_1': 10.0, 'CP_2': 50.0, 'CP_3': 90.0, 'CP_4': 20.0}
    shares = gateway.allocate_power(socs, budget)
    assert sum(shares.values()) <= budget + 1e-9
    assert all(v <= gateway.MAX_CHARGER_KW for v in shares.values())
    assert shares['CP_1'] >= shares['CP_2'] >= shares['CP_3']

def limit_of(message):
    schedule = message[3]["csChargingProfiles"]["chargingSchedule"]
    return schedule["chargingSchedulePeriod"][0]["limit"]

def test_negative_allocation_converts_kw_to_watts():
    assert limit_of(gateway.build_charging_profile(1, -70.0)) == 0.0

def test_zero_kw_conversion_to_watt():
    assert limit_of(gateway.build_charging_profile(1, 0.0)) == 0.0

def test_normal_allocation_converts_kw_to_watts():
    assert limit_of(gateway.build_charging_profile(1, 11.2)) == 11200.0

def test_appropriate_split_cap_allocations():
    active = {"CP_1":3.0, "CP_2":1.5, "CP_3":0.5}
    results = gateway.split_with_cap(active, 150.0)
    assert results == {"CP_1":10.0, "CP_2":10.0, "CP_3":10.0}
    assert 150.0 - sum(results.values()) == 120