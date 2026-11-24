"""Read numbers out of OCPP 1.6J MeterValues and DataTransfer payloads. Pure: no I/O.

Understands the standard form, meterValue[] -> sampledValue[] with the measurands SoC,
Energy.Active.Import.Register, Power.Active.Import and Current.Import, and the simple
test form {"soc": 42}. Every value comes back in one fixed unit (Percent, Wh, W, A);
anything that cannot be read safely (unknown unit, signed data, NaN) is skipped, never
guessed.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass

SOC = "SoC"
ENERGY = "Energy.Active.Import.Register"
POWER = "Power.Active.Import"
CURRENT = "Current.Import"

# OCPP 1.6: a sampledValue without a measurand is Energy.Active.Import.Register, and a
# value without a unit is in the measurand's base unit.
_DEFAULT_MEASURAND = ENERGY
_UNIT_SCALE = {
    SOC: {"Percent": 1.0},
    ENERGY: {"Wh": 1.0, "kWh": 1000.0},
    POWER: {"W": 1.0, "kW": 1000.0},
    CURRENT: {"A": 1.0},
}
_DEFAULT_UNIT = {SOC: "Percent", ENERGY: "Wh", POWER: "W", CURRENT: "A"}
_FIELD = {SOC: "soc", ENERGY: "energy_wh", POWER: "power_w", CURRENT: "current_a"}


@dataclass
class MeterReading:
    soc: float | None = None          # battery %, as reported (range is checked by the caller)
    energy_wh: float | None = None    # energy register, Wh
    power_w: float | None = None      # power drawn right now, W
    current_a: float | None = None    # current drawn right now, A (highest phase)


def parse_meter_values(payload) -> MeterReading:
    """Readings from a MeterValues payload (or a StopTransaction's transactionData)."""
    reading = MeterReading()
    if not isinstance(payload, dict):
        return reading
    simple_soc = _simple_soc(payload)
    if simple_soc is not None:
        reading.soc = simple_soc
    meter_values = payload.get("meterValue")
    if isinstance(meter_values, list):
        # Entries are applied in order, so the last entry carrying a measurand wins.
        for meter_value in meter_values:
            if isinstance(meter_value, dict) and isinstance(meter_value.get("sampledValue"), list):
                _apply_samples(reading, meter_value["sampledValue"])
    return reading


def parse_data_transfer(payload) -> MeterReading:
    """Readings from a DataTransfer payload: {"soc": ..} at the top or inside "data"
    (which OCPP defines as a string, so a JSON-encoded string is decoded first)."""
    if not isinstance(payload, dict):
        return MeterReading()
    reading = parse_meter_values(payload)
    data = payload.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = None
    if isinstance(data, dict):
        inner = parse_meter_values(data)
        for name in _FIELD.values():
            if getattr(inner, name) is not None:
                setattr(reading, name, getattr(inner, name))
    return reading


def to_number(value) -> float | None:
    """A finite float, or None. true/false are not numbers here."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _simple_soc(payload):
    for key, value in payload.items():
        if isinstance(key, str) and key.lower() == "soc":
            return to_number(value)
    return None


def _phase_key(phase):
    """None for a total value; "L1".."L3" for a per-phase value; "skip" for N or line-to-line."""
    if phase is None:
        return None
    if phase in ("L1", "L2", "L3"):
        return phase
    if phase in ("L1-N", "L2-N", "L3-N"):
        return phase[:2]
    return "skip"


def _apply_samples(reading, samples):
    totals, phases = {}, {}
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        measurand = sample.get("measurand", _DEFAULT_MEASURAND)
        if not isinstance(measurand, str) or measurand not in _UNIT_SCALE:
            continue
        if sample.get("format", "Raw") != "Raw":
            continue                                    # SignedData is not a plain number
        unit = sample.get("unit", _DEFAULT_UNIT[measurand])
        scale = _UNIT_SCALE[measurand].get(unit) if isinstance(unit, str) else None
        value = to_number(sample.get("value"))
        if scale is None or value is None:
            continue
        value *= scale
        phase = _phase_key(sample.get("phase"))
        if phase is None:
            totals[measurand] = value
        elif phase != "skip":
            phases.setdefault(measurand, {})[phase] = value

    for measurand in _UNIT_SCALE:
        if measurand in totals:
            value = totals[measurand]                   # a total always beats per-phase values
        elif measurand in phases and measurand != SOC:
            per_phase = phases[measurand].values()
            # Energy and power add up across phases; current is per conductor, so the
            # highest phase is the one that matters for breakers.
            value = max(per_phase) if measurand == CURRENT else sum(per_phase)
        else:
            continue
        setattr(reading, _FIELD[measurand], value)
