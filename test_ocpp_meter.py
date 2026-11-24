import pytest

from ocpp_meter import MeterReading, parse_data_transfer, parse_meter_values


def mv(*samples, more=()):
    return {"connectorId": 1, "meterValue": [{"timestamp": "2026-11-23T18:00:00Z",
                                              "sampledValue": list(samples)}, *more]}


def test_simple_test_json():
    assert parse_meter_values({"soc": 42}).soc == 42.0
    assert parse_meter_values({"SoC": "55.5"}).soc == 55.5


def test_all_four_standard_measurands():
    reading = parse_meter_values(mv(
        {"value": "61.5", "measurand": "SoC", "unit": "Percent", "location": "EV"},
        {"value": "12345", "measurand": "Energy.Active.Import.Register", "unit": "Wh"},
        {"value": "6650.0", "measurand": "Power.Active.Import", "unit": "W"},
        {"value": "27.7", "measurand": "Current.Import", "unit": "A"},
    ))
    assert reading == MeterReading(soc=61.5, energy_wh=12345.0, power_w=6650.0, current_a=27.7)


def test_kilo_units_converted():
    reading = parse_meter_values(mv({"value": "12.5", "measurand": "Energy.Active.Import.Register", "unit": "kWh"},
                                    {"value": "470.0", "measurand": "Power.Active.Import", "unit": "kW"}))
    assert (reading.energy_wh, reading.power_w) == (12500.0, 470000.0)


def test_defaults_measurand_is_energy_in_wh():
    assert parse_meter_values(mv({"value": "900"})).energy_wh == 900.0


def test_per_phase_power_and_energy_add_up_current_takes_highest():
    reading = parse_meter_values(mv(
        {"value": "2000", "measurand": "Power.Active.Import", "phase": "L1"},
        {"value": "2100", "measurand": "Power.Active.Import", "phase": "L2"},
        {"value": "1900", "measurand": "Power.Active.Import", "phase": "L3-N"},
        {"value": "8.7", "measurand": "Current.Import", "phase": "L1"},
        {"value": "9.1", "measurand": "Current.Import", "phase": "L2"},
        {"value": "3.0", "measurand": "Current.Import", "phase": "N"},           # neutral: ignored
    ))
    assert reading.power_w == 6000.0
    assert reading.current_a == 9.1


def test_total_beats_phases():
    reading = parse_meter_values(mv({"value": "2000", "measurand": "Power.Active.Import", "phase": "L1"},
                                    {"value": "6100", "measurand": "Power.Active.Import"}))
    assert reading.power_w == 6100.0


@pytest.mark.parametrize("sample", [
    {"value": "abc", "measurand": "SoC"},
    {"value": "NaN", "measurand": "SoC"},
    {"value": "inf", "measurand": "Power.Active.Import"},
    {"value": "5", "measurand": "Power.Active.Import", "unit": "VA"},        # not active power
    {"value": "5", "measurand": "Power.Active.Import", "unit": ["W"]},
    {"value": "5", "measurand": ["SoC"]},
    {"value": "5", "measurand": "Voltage", "unit": "V"},
    {"value": "a1b2", "measurand": "SoC", "format": "SignedData"},
    {"value": True, "measurand": "SoC"},
    {"measurand": "SoC"},
    "not a sample",
])
def test_unreadable_samples_skipped(sample):
    assert parse_meter_values(mv(sample)) == MeterReading()


def test_latest_meter_value_wins():
    payload = mv({"value": "50", "measurand": "SoC"},
                 more=[{"timestamp": "t2", "sampledValue": [{"value": "51", "measurand": "SoC"}]}])
    assert parse_meter_values(payload).soc == 51.0


@pytest.mark.parametrize("payload", [None, [], "x", {"meterValue": "x"}, {"meterValue": [1, {"sampledValue": 5}]}])
def test_malformed_payloads_give_empty_reading(payload):
    assert parse_meter_values(payload) == MeterReading()


def test_data_transfer_forms():
    assert parse_data_transfer({"vendorId": "x", "data": '{"soc": 40}'}).soc == 40.0
    assert parse_data_transfer({"vendorId": "x", "data": {"soc": 41}}).soc == 41.0
    assert parse_data_transfer({"soc": 42}).soc == 42.0
    assert parse_data_transfer({"vendorId": "x", "data": "not json"}).soc is None
    assert parse_data_transfer("x") == MeterReading()
