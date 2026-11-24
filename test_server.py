"""CentralSystem driven entirely in memory: fake clock, fake chargers, no sockets."""
import asyncio
import dataclasses
import json
import logging
import re

import pytest

import gateway
import server
from conftest import OFF_PEAK, PEAK

ACCEPT = {"status": "Accepted"}
REJECT = {"status": "Rejected"}


class Clock:
    def __init__(self, wall=OFF_PEAK):
        self.t, self.wall = 1000.0, wall

    def monotonic(self):
        return self.t

    def now(self):
        return self.wall

    def advance(self, seconds):
        self.t += seconds


class FakeCharger:
    """Stands in for a websocket. Records every frame the server sends and answers it via
    `script(message) -> reply payload | ("error", code) | None (no reply)`."""

    def __init__(self, cs, charger_id, script=lambda message: ACCEPT):
        self.cs, self.charger_id, self.script, self.sent = cs, charger_id, script, []
        cs.attach(charger_id, self.send)

    async def send(self, text):
        message = json.loads(text)
        self.sent.append(message)
        reply = self.script(message)
        if reply is None:
            return
        if isinstance(reply, tuple):
            frame = [4, message[1], reply[1], "scripted error", {}]
        else:
            frame = [3, message[1], reply]
        asyncio.get_running_loop().call_soon(self.cs.on_message, self.charger_id, json.dumps(frame))

    def profiles(self):
        return [m[3] for m in self.sent if m[2] == "SetChargingProfile"]

    def limits(self):
        return [(p["csChargingProfiles"]["chargingProfilePurpose"], p["connectorId"],
                 gateway.profile_limit_w(["", "", "", p])) for p in self.profiles()]


def make(cfg, clock=None, **kwargs):
    clock = clock or Clock()
    cs = server.CentralSystem(cfg, monotonic=clock.monotonic, clock=clock.now, **kwargs)
    return cs, clock


def call(cs, charger_id, action, payload):
    reply = cs.on_message(charger_id, json.dumps([2, "m1", action, payload]))
    return json.loads(reply) if reply is not None else None


def result(cs, charger_id, action, payload):
    reply = call(cs, charger_id, action, payload)
    assert reply[0] == 3, reply
    return reply[2]


def site_load(cs, kw):
    result(cs, "SITE_METER", "MeterValues", {"connectorId": 0, "meterValue": [{
        "timestamp": "t", "sampledValue": [{"value": str(kw), "measurand": "Power.Active.Import", "unit": "kW"}]}]})


def plug(cs, charger_id, soc=None, transaction=True, connector=1, meter_start=0):
    result(cs, charger_id, "StatusNotification", {"connectorId": connector, "errorCode": "NoError", "status": "Preparing"})
    tx = None
    if transaction:
        tx = result(cs, charger_id, "StartTransaction", {"connectorId": connector, "idTag": "TAG",
                                                         "meterStart": meter_start, "timestamp": "t"})["transactionId"]
    if soc is not None:
        result(cs, charger_id, "MeterValues", {"connectorId": connector, "meterValue": [{
            "timestamp": "t", "sampledValue": [{"value": str(soc), "measurand": "SoC", "unit": "Percent"}]}]})
    return tx


def power(cs, charger_id, watts, connector=1):
    result(cs, charger_id, "MeterValues", {"connectorId": connector, "meterValue": [{
        "timestamp": "t", "sampledValue": [{"value": str(watts), "measurand": "Power.Active.Import", "unit": "W"}]}]})


async def cycle(cs):
    await asyncio.gather(*await cs.run_cycle())


def run(coroutine):
    return asyncio.run(coroutine)


def session(cs, charger_id, connector=1):
    return cs.chargers[charger_id].connectors[connector].session


# ---------------------------------------------------------------- basic OCPP replies

def test_boot_notification_reply(cfg):
    cs, _ = make(cfg)
    reply = result(cs, "CP_1", "BootNotification", {"chargePointVendor": "V", "chargePointModel": "M"})
    assert reply == {"status": "Accepted", "currentTime": "2026-11-23T18:00:00Z", "interval": 10}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", reply["currentTime"])


def test_heartbeat_and_status_notification_replies(cfg):
    cs, _ = make(cfg)
    assert result(cs, "CP_1", "Heartbeat", {}) == {"currentTime": "2026-11-23T18:00:00Z"}
    assert result(cs, "CP_1", "StatusNotification",
                  {"connectorId": 1, "errorCode": "NoError", "status": "Available"}) == {}
    assert result(cs, "CP_1", "StatusNotification",
                  {"connectorId": 0, "errorCode": "NoError", "status": "Available"}) == {}


def test_unknown_action_is_not_implemented(cfg):
    cs, _ = make(cfg)
    assert call(cs, "CP_1", "UnlockConnector", {})[:3] == [4, "m1", "NotImplemented"]


@pytest.mark.parametrize("raw, expected", [
    ("not json", None),
    ('{"a": 1}', None),
    ("[]", None),
    ("[2]", None),                                         # CALL without a message id
    ('[9, "x"]', None),
    ('[2, "m1", "Heartbeat"]', "FormationViolation"),
    ('[2, "m1", "Heartbeat", []]', "FormationViolation"),
    ('[2, "m1", 5, {}]', "FormationViolation"),
])
def test_malformed_frames(cfg, raw, expected):
    cs, _ = make(cfg)
    reply = cs.on_message("CP_1", raw)
    if expected is None:
        assert reply is None
    else:
        assert json.loads(reply)[2] == expected


@pytest.mark.parametrize("payload, code", [
    ({"connectorId": 1, "meterStart": 0, "timestamp": "t"}, "OccurenceConstraintViolation"),
    ({"connectorId": 1, "idTag": "T", "meterStart": "0", "timestamp": "t"}, "TypeConstraintViolation"),
    ({"connectorId": True, "idTag": "T", "meterStart": 0, "timestamp": "t"}, "TypeConstraintViolation"),
    ({"connectorId": 0, "idTag": "T", "meterStart": 0, "timestamp": "t"}, "PropertyConstraintViolation"),
])
def test_start_transaction_validation(cfg, payload, code):
    cs, _ = make(cfg)
    assert call(cs, "CP_1", "StartTransaction", payload)[2] == code


def test_handler_crash_returns_internal_error_and_server_survives(cfg, monkeypatch):
    cs, _ = make(cfg)
    monkeypatch.setitem(cs._handlers, "Heartbeat", lambda cid, payload: 1 / 0)
    assert call(cs, "CP_1", "Heartbeat", {})[2] == "InternalError"
    assert result(cs, "CP_1", "StatusNotification", {"connectorId": 1, "errorCode": "NoError", "status": "Available"}) == {}


def test_site_meter_only_sends_meter_data(cfg):
    cs, _ = make(cfg)
    assert call(cs, "SITE_METER", "StartTransaction", {})[2] == "NotSupported"


# ---------------------------------------------------------------- sessions

def test_plug_in_time_duration_and_energy_tracked(cfg, tmp_path):
    cfg = dataclasses.replace(cfg, session_log_path=str(tmp_path / "sessions.jsonl"))
    cs, clock = make(cfg)
    tx = plug(cs, "CP_1", soc=20.0, meter_start=1000)
    s = session(cs, "CP_1")
    assert s.plugged_in_at == OFF_PEAK and s.transaction_id == tx and s.start_soc == 20.0

    clock.advance(3600)
    result(cs, "CP_1", "MeterValues", {"connectorId": 1, "transactionId": tx, "meterValue": [{
        "timestamp": "t", "sampledValue": [{"value": "4500", "measurand": "Energy.Active.Import.Register"}]}]})
    assert s.kwh_delivered == 3.5
    result(cs, "CP_1", "StopTransaction", {"transactionId": tx, "meterStop": 6000, "timestamp": "t", "reason": "EVDisconnected"})
    assert s.charging_stopped and s.kwh_delivered == 5.0
    clock.advance(60)
    result(cs, "CP_1", "StatusNotification", {"connectorId": 1, "errorCode": "NoError", "status": "Available"})

    assert session(cs, "CP_1") is None
    record = cs.completed_sessions[-1]
    assert record["plugged_in_at"] == "2026-11-23T18:00:00Z"
    assert record["plugged_in_duration_s"] == 3660.0
    assert record["energy_kwh"] == 5.0
    assert (record["end_reason"], record["stop_reason"]) == ("Unplugged", "EVDisconnected")
    assert json.loads((tmp_path / "sessions.jsonl").read_text().strip()) == record


def test_energy_register_going_backwards_keeps_delivered(cfg):
    cs, _ = make(cfg)
    plug(cs, "CP_1", meter_start=5000)
    s = session(cs, "CP_1")
    s.record_energy(8000)
    s.record_energy(100)                    # meter swapped
    assert s.kwh_delivered == 3.0
    s.record_energy(1100)
    assert s.kwh_delivered == 4.0


def test_simple_soc_json_opens_session_and_bad_soc_ignored(cfg):
    cs, _ = make(cfg)
    assert result(cs, "CP_1", "MeterValues", {"soc": 55}) == {}
    assert session(cs, "CP_1").soc == 55.0
    result(cs, "CP_1", "MeterValues", {"soc": 400})
    assert session(cs, "CP_1").soc == 55.0
    assert result(cs, "CP_1", "DataTransfer", {"vendorId": "x", "data": '{"soc": 41}'}) == ACCEPT
    assert session(cs, "CP_1").soc == 41.0
    assert result(cs, "CP_1", "DataTransfer", {"vendorId": "x", "data": "hello"}) == {"status": "UnknownMessageId"}


def test_meter_values_without_a_car_open_no_session(cfg):
    cs, _ = make(cfg)
    power(cs, "CP_1", 0)
    assert "CP_1" not in cs.chargers or not cs.chargers["CP_1"].sessions()


def test_unknown_running_transaction_is_adopted_after_restart(cfg):
    cs, _ = make(cfg)
    site_load(cs, 470.0)
    cp = FakeCharger(cs, "CP_1")
    result(cs, "CP_1", "MeterValues", {"connectorId": 1, "transactionId": 777, "meterValue": [{
        "timestamp": "t", "sampledValue": [{"value": "5000", "measurand": "Energy.Active.Import.Register"},
                                           {"value": "7000", "measurand": "Power.Active.Import"}]}]})
    s = session(cs, "CP_1")
    assert s.transaction_id == 777 and s.power_w == 7000.0
    run(cycle(cs))
    assert cp.limits() == [("TxProfile", 1, 10000.0)]
    assert cp.profiles()[0]["csChargingProfiles"]["transactionId"] == 777
    result(cs, "CP_1", "StopTransaction", {"transactionId": 777, "meterStop": 6000, "timestamp": "t"})
    assert s.charging_stopped and s.kwh_delivered == 1.0


def test_new_transaction_closes_unstopped_one(cfg):
    cs, _ = make(cfg)
    first = plug(cs, "CP_1")
    second = result(cs, "CP_1", "StartTransaction", {"connectorId": 1, "idTag": "T", "meterStart": 0, "timestamp": "t"})["transactionId"]
    assert second != first
    assert cs.completed_sessions[-1]["end_reason"] == "NewTransaction"
    assert session(cs, "CP_1").transaction_id == second


def test_stop_unknown_transaction_is_accepted_and_ignored(cfg):
    cs, _ = make(cfg)
    assert result(cs, "CP_1", "StopTransaction", {"transactionId": 999, "meterStop": 0, "timestamp": "t"}) == \
        {"idTagInfo": {"status": "Accepted"}}


# ---------------------------------------------------------------- allocation

def test_demo_split_sent_as_tx_profiles(cfg, caplog):
    caplog.set_level(logging.INFO, logger="edge_ocpp_router")
    cs, _ = make(cfg)
    site_load(cs, 470.0)
    cp1, cp2 = FakeCharger(cs, "CP_1"), FakeCharger(cs, "CP_2")
    tx1, tx2 = plug(cs, "CP_1", soc=22.0), plug(cs, "CP_2", soc=67.0)
    run(cycle(cs))
    assert cp1.limits() == [("TxProfile", 1, 6666.0)]
    assert cp2.limits() == [("TxProfile", 1, 3333.0)]
    assert cp1.profiles()[0]["csChargingProfiles"]["transactionId"] == tx1
    assert cp2.profiles()[0]["csChargingProfiles"]["transactionId"] == tx2
    assert "CP_1 22.0% -> 6666 W" in caplog.text and "CP_2 67.0% -> 3333 W" in caplog.text


def test_tx_default_profile_without_transaction(cfg):
    cs, _ = make(cfg)
    site_load(cs, 470.0)
    cp = FakeCharger(cs, "CP_1")
    plug(cs, "CP_1", soc=22.0, transaction=False)
    run(cycle(cs))
    assert cp.limits() == [("TxDefaultProfile", 1, 10000.0)]


def test_no_resend_until_change_or_refresh(cfg):
    cs, clock = make(cfg)
    site_load(cs, 470.0)
    cp = FakeCharger(cs, "CP_1")
    plug(cs, "CP_1", soc=22.0)

    async def scenario():
        await cycle(cs)
        await cycle(cs)                                    # unchanged: nothing sent
        assert len(cp.profiles()) == 1
        clock.advance(cfg.profile_refresh_s)
        site_load(cs, 470.0)
        await cycle(cs)                                    # refresh interval reached
        assert len(cp.profiles()) == 2
        site_load(cs, 474.0)                               # budget 6 kW: limit changes
        await cycle(cs)
        assert cp.limits()[-1] == ("TxProfile", 1, 6000.0)
    run(scenario())


def test_stale_telemetry_drops_to_floor(cfg, caplog):
    cs, clock = make(cfg)
    site_load(cs, 470.0)
    cp1, cp2 = FakeCharger(cs, "CP_1"), FakeCharger(cs, "CP_2")
    plug(cs, "CP_1", soc=22.0)
    plug(cs, "CP_2", soc=67.0)

    async def scenario():
        clock.advance(15.0)                                # exactly 15 s: still fresh
        await cycle(cs)
        assert cp1.limits()[-1][2] == 6666.0
        clock.advance(0.5)
        await cycle(cs)
        assert cp1.limits()[-1][2] == 2000.0 and cp2.limits()[-1][2] == 0.0
        assert "stale" in caplog.text
        site_load(cs, 470.0)                               # telemetry back
        await cycle(cs)
        assert cp1.limits()[-1][2] == 6666.0
    run(scenario())


def test_no_telemetry_ever_uses_floor(cfg):
    cs, _ = make(cfg)
    cp = FakeCharger(cs, "CP_1")
    plug(cs, "CP_1", soc=22.0)
    run(cycle(cs))
    assert cp.limits() == [("TxProfile", 1, 2000.0)]


def test_kwh_weighting_for_car_without_soc(cfg):
    cs, _ = make(cfg)
    site_load(cs, 470.0)
    a, b = FakeCharger(cs, "CP_A"), FakeCharger(cs, "CP_B")
    plug(cs, "CP_A")                                       # 0 kWh so far -> weight 3.0
    plug(cs, "CP_B", meter_start=0)
    session(cs, "CP_B").record_energy(15000)               # 15 kWh -> weight 1.5
    run(cycle(cs))
    assert a.limits()[-1][2] == 6666.0 and b.limits()[-1][2] == 3333.0


def test_tapered_car_capped_at_minimum_and_headroom_redistributed(cfg):
    cs, clock = make(cfg)
    site_load(cs, 470.0)
    cp1, cp2 = FakeCharger(cs, "CP_1"), FakeCharger(cs, "CP_2")
    plug(cs, "CP_1", soc=22.0)
    plug(cs, "CP_2", soc=67.0)

    async def scenario():
        await cycle(cs)
        clock.advance(cfg.taper_grace_s - 1)
        site_load(cs, 470.02)                              # the CT sees CP_1's 20 W too
        power(cs, "CP_1", 20)                              # near 0 W, but still in the grace period
        await cycle(cs)
        assert cp1.limits()[-1][2] == 6666.0
        clock.advance(1)
        power(cs, "CP_1", 20)
        await cycle(cs)                                    # CP_1 cut at once; CP_2 must wait...
        assert cp1.limits()[-1][2] == 1440.0
        assert cp2.limits()[-1][2] <= 3334.0               # (only the 1 W that is free now)
        await cycle(cs)                                    # ...until the cut is confirmed
        assert cp2.limits()[-1][2] == 8560.0
        clock.advance(5)
        site_load(cs, 471.35)
        power(cs, "CP_1", 1350)                            # drawing again: back to its full share
        await cycle(cs)                                    # CP_2 down first,
        assert cp2.limits()[-1][2] == 3333.0 and cp1.limits()[-1][2] == 1440.0
        await cycle(cs)                                    # then CP_1 up
        assert cp1.limits()[-1][2] == 6666.0
    run(scenario())


def test_stale_power_reading_does_not_count_as_tapered(cfg):
    cs, clock = make(cfg)
    site_load(cs, 470.0)
    cp1 = FakeCharger(cs, "CP_1")
    plug(cs, "CP_1", soc=22.0)

    async def scenario():
        await cycle(cs)
        power(cs, "CP_1", 0)
        clock.advance(cfg.taper_grace_s + cfg.telemetry_stale_after_s + 1)
        site_load(cs, 470.0)
        await cycle(cs)
        assert cp1.limits()[-1][2] == 10000.0
    run(scenario())


def test_peak_window_caps_budget(cfg):
    cs, _ = make(cfg, Clock(wall=PEAK))
    site_load(cs, 0.0)
    cp = FakeCharger(cs, "CP_1")
    plug(cs, "CP_1", soc=22.0)
    run(cycle(cs))
    assert cp.limits() == [("TxProfile", 1, 5000.0)]


def test_injected_grid_signal_caps_then_falls_back_when_old(cfg):
    cs, clock = make(cfg, Clock(wall=PEAK), grid_signal_cap_kw=3.0)
    site_load(cs, 0.0)
    cp = FakeCharger(cs, "CP_1")
    plug(cs, "CP_1", soc=22.0)

    async def scenario():
        await cycle(cs)
        assert cp.limits()[-1][2] == 3000.0                # grid signal wins over the tariff
        clock.advance(cfg.grid_signal_max_age_s + 1)
        site_load(cs, 0.0)
        await cycle(cs)
        assert cp.limits()[-1][2] == 5000.0                # offline: tariff peak cap
    run(scenario())


def test_invalid_grid_signal_ignored(cfg):
    cs, _ = make(cfg)
    cs.set_grid_signal("garbage")
    assert cs.grid_signal_cap_kw is None
    cs.set_grid_signal(-5)
    assert cs.grid_signal_cap_kw is None
    cs.set_grid_signal(12)
    assert cs.grid_signal_cap_kw == 12.0


def test_faulted_connector_gets_nothing(cfg):
    cs, _ = make(cfg)
    site_load(cs, 470.0)
    cp1, cp2 = FakeCharger(cs, "CP_1"), FakeCharger(cs, "CP_2")
    plug(cs, "CP_1", soc=22.0)
    plug(cs, "CP_2", soc=67.0)
    result(cs, "CP_1", "StatusNotification", {"connectorId": 1, "errorCode": "GroundFailure", "status": "Faulted"})
    run(cycle(cs))
    assert cp1.limits() == [] and cp2.limits() == [("TxProfile", 1, 10000.0)]


def test_two_connectors_on_one_charger(cfg):
    cs, _ = make(cfg)
    site_load(cs, 470.0)
    cp = FakeCharger(cs, "CP_1")
    plug(cs, "CP_1", soc=22.0, connector=1)
    plug(cs, "CP_1", soc=67.0, connector=2)
    run(cycle(cs))
    assert sorted(cp.limits()) == [("TxProfile", 1, 6666.0), ("TxProfile", 2, 3333.0)]


# ---------------------------------------------------------------- rejection and escalation

def test_rejected_tx_profile_falls_back_to_charge_point_max(cfg, caplog):
    cs, _ = make(cfg)
    site_load(cs, 470.0)
    cp = FakeCharger(cs, "CP_1", lambda m: REJECT if m[3]["csChargingProfiles"]["chargingProfilePurpose"] == "TxProfile" else ACCEPT)
    plug(cs, "CP_1", soc=22.0)
    FakeCharger(cs, "CP_2")
    plug(cs, "CP_2", soc=67.0)

    async def scenario():
        await cycle(cs)
        assert cp.limits() == [("TxProfile", 1, 6666.0), ("ChargePointMaxProfile", 0, 6666.0)]
        profile = cp.profiles()[1]["csChargingProfiles"]
        assert profile["stackLevel"] == 0 and profile["chargingProfileKind"] == "Absolute"
        assert cs.chargers["CP_1"].use_max_profile and session(cs, "CP_1").used_fallback
        assert not any(m[2] == "RemoteStopTransaction" for m in cp.sent)
        site_load(cs, 474.0)                               # change: only ChargePointMax is used now
        await cycle(cs)
        assert cp.limits()[-1] == ("ChargePointMaxProfile", 0, 4000.0)
    run(scenario())
    assert "retrying once with ChargePointMaxProfile" in caplog.text


@pytest.mark.parametrize("tx_reply", [REJECT, {"status": "NotSupported"}, ("error", "NotSupported")])
def test_everything_rejected_sends_remote_stop_and_critical_alert(cfg, caplog, tx_reply):
    cs, _ = make(cfg)
    site_load(cs, 470.0)

    def script(message):
        return ACCEPT if message[2] == "RemoteStopTransaction" else tx_reply
    cp = FakeCharger(cs, "CP_1", script)
    tx = plug(cs, "CP_1", soc=22.0)
    run(cycle(cs))

    actions = [m[2] for m in cp.sent]
    assert actions == ["SetChargingProfile", "SetChargingProfile", "RemoteStopTransaction"]
    assert cp.sent[2][3] == {"transactionId": tx}
    assert session(cs, "CP_1").uncontrollable
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert critical and "CRITICAL ALERT" in critical[0].getMessage()

    plan = cs.plan()                                       # its full cap is held in reserve
    assert plan.reserved_w == 10000 and not plan.sessions
    result(cs, "CP_1", "StopTransaction", {"transactionId": tx, "meterStop": 10, "timestamp": "t", "reason": "Remote"})
    assert cs.plan().reserved_w == 0                       # stopped: reservation released


def test_rejected_without_transaction_alerts_without_remote_stop(cfg, caplog):
    cs, _ = make(cfg)
    site_load(cs, 470.0)
    cp = FakeCharger(cs, "CP_1", lambda m: REJECT)
    plug(cs, "CP_1", soc=22.0, transaction=False)
    run(cycle(cs))
    assert [m[2] for m in cp.sent] == ["SetChargingProfile", "SetChargingProfile"]
    assert "no transaction to stop" in caplog.text


def test_remote_stop_refused_is_critical(cfg, caplog):
    cs, _ = make(cfg)
    site_load(cs, 470.0)
    FakeCharger(cs, "CP_1", lambda m: REJECT)
    plug(cs, "CP_1", soc=22.0)
    run(cycle(cs))
    assert "answered Rejected to RemoteStopTransaction; manual intervention required" in caplog.text


def test_no_answer_is_not_escalated_and_retried_next_cycle(cfg):
    cfg = dataclasses.replace(cfg, call_timeout_s=0.05)
    cs, _ = make(cfg)
    site_load(cs, 470.0)
    replies = iter([None])                                 # first call unanswered, then Accepted
    cp = FakeCharger(cs, "CP_1", lambda m: next(replies, ACCEPT))
    plug(cs, "CP_1", soc=22.0)

    async def scenario():
        await cycle(cs)
        assert [m[2] for m in cp.sent] == ["SetChargingProfile"]          # no fallback, no stop
        assert not session(cs, "CP_1").uncontrollable
        await cycle(cs)
        assert len(cp.profiles()) == 2 and session(cs, "CP_1").accepted_w == 10000
    run(scenario())


def test_charge_point_max_rejected_later_escalates_and_stops_using_it(cfg, caplog):
    cs, _ = make(cfg)
    site_load(cs, 470.0)
    state = {"max_ok": True}

    def script(message):
        if message[2] == "RemoteStopTransaction":
            return ACCEPT
        purpose = message[3]["csChargingProfiles"]["chargingProfilePurpose"]
        return ACCEPT if purpose == "ChargePointMaxProfile" and state["max_ok"] else REJECT
    FakeCharger(cs, "CP_1", script)
    plug(cs, "CP_1", soc=22.0)

    async def scenario():
        await cycle(cs)
        assert cs.chargers["CP_1"].use_max_profile
        state["max_ok"] = False
        site_load(cs, 474.0)
        await cycle(cs)
        assert not cs.chargers["CP_1"].use_max_profile and session(cs, "CP_1").uncontrollable
    run(scenario())
    assert "CRITICAL ALERT" in caplog.text


def test_boot_resets_fallback_mode(cfg):
    cs, _ = make(cfg)
    cs.chargers["CP_1"] = server.Charger("CP_1", use_max_profile=True)
    result(cs, "CP_1", "BootNotification", {"chargePointVendor": "V", "chargePointModel": "M"})
    assert not cs.chargers["CP_1"].use_max_profile


# ---------------------------------------------------------------- connections

def test_offline_charger_keeps_its_limit_reserved_then_expires(cfg):
    cs, clock = make(cfg)
    site_load(cs, 470.0)
    cp1, cp2 = FakeCharger(cs, "CP_1"), FakeCharger(cs, "CP_2")
    plug(cs, "CP_1", soc=22.0)
    plug(cs, "CP_2", soc=67.0)

    async def scenario():
        await cycle(cs)
        cs.detach("CP_1", cp1.send)
        await cycle(cs)                                    # CP_1 may still draw 6.666 kW
        assert cs.plan().reserved_w == 6666
        assert cp2.limits()[-1][2] == 3333.0               # +1 W is below min_increase_w: not sent
        clock.advance(cfg.offline_session_timeout_s + 1)
        site_load(cs, 470.0)
        await cycle(cs)
        assert cp2.limits()[-1][2] == 10000.0
        assert cs.completed_sessions[-1]["end_reason"] == "ChargerOffline"
    run(scenario())


def test_reconnect_replaces_connection_and_resends(cfg):
    cs, _ = make(cfg)
    site_load(cs, 470.0)
    old = FakeCharger(cs, "CP_1")
    plug(cs, "CP_1", soc=22.0)

    async def scenario():
        await cycle(cs)
        new = FakeCharger(cs, "CP_1")
        cs.detach("CP_1", old.send)                        # the old socket closing late
        assert cs.chargers["CP_1"].online
        await cycle(cs)
        assert new.limits() == [("TxProfile", 1, 10000.0)]
    run(scenario())


def test_disconnect_fails_pending_call_fast(cfg):
    cs, _ = make(cfg)
    cp = FakeCharger(cs, "CP_1", lambda m: None)

    async def scenario():
        task = asyncio.create_task(cs.call(cs.chargers["CP_1"], [2, "x1", "Heartbeat", {}]))
        await asyncio.sleep(0)
        cs.detach("CP_1", cp.send)
        with pytest.raises(server.NoResponse):
            await task
    run(scenario())


def test_reply_to_unknown_message_ignored(cfg):
    cs, _ = make(cfg)
    FakeCharger(cs, "CP_1")
    assert cs.on_message("CP_1", json.dumps([3, "never-sent", {}])) is None


@pytest.mark.parametrize("path, expected", [
    ("/CP_1", "CP_1"), ("/ocpp/CP_1", "CP_1"), ("/CP_1/", "CP_1"), ("/CP%201?x=1", "CP 1"),
    ("/", None), ("", None), ("/CP_1%0AFAKE", None),
])
def test_charger_id_from_path(path, expected):
    assert server.charger_id_from_path(path) == expected


# ---------------------------------------------------------------- background loops

def test_allocation_loop_survives_a_failing_cycle(cfg, caplog):
    cfg = dataclasses.replace(cfg, allocation_interval_s=0.01)
    cs, _ = make(cfg)
    calls = []

    async def flaky_cycle():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return []
    cs.run_cycle = flaky_cycle

    async def scenario():
        loop = asyncio.create_task(server.allocation_loop(cs))
        while len(calls) < 3:
            await asyncio.sleep(0.01)
        loop.cancel()
    run(scenario())
    assert "allocation cycle failed; continuing" in caplog.text


def test_grid_poller_sets_signal_and_survives_errors(cfg):
    cfg = dataclasses.replace(cfg, grid_signal_poll_interval_s=0.01)
    cs, _ = make(cfg)
    values = iter([7.5, RuntimeError("offline"), 8.0])
    seen = []

    async def fetch():
        value = next(values, 8.0)
        if isinstance(value, Exception):
            raise value
        return value

    async def scenario():
        poller = asyncio.create_task(server.grid_signal_poller(cs, fetch))
        while len(seen) < 3:
            await asyncio.sleep(0.01)
            seen.append(cs.grid_signal_cap_kw)
        poller.cancel()
    run(scenario())
    assert 7.5 in seen and 8.0 in seen


def test_grid_stub_makes_no_call_and_reports_no_signal():
    assert run(server.fetch_grid_signal_stub()) is None


def test_close_all_sessions_on_shutdown(cfg):
    cs, _ = make(cfg)
    plug(cs, "CP_1", soc=22.0)
    cs.close_all_sessions("ServerShutdown")
    assert cs.completed_sessions[-1]["end_reason"] == "ServerShutdown"
    assert session(cs, "CP_1") is None


# ---------------------------------------------------------------- CT on the main feeder (EVs included)

def test_ev_draw_is_subtracted_from_the_ct_reading(cfg):
    cs, _ = make(cfg)
    FakeCharger(cs, "CP_1"), FakeCharger(cs, "CP_2")
    plug(cs, "CP_1", soc=22.0)
    plug(cs, "CP_2", soc=67.0)
    site_load(cs, 470.0)
    run(cycle(cs))                                         # 6666 / 3333 accepted
    power(cs, "CP_1", 6666)
    power(cs, "CP_2", 3333)
    site_load(cs, 479.999)                                 # 470 other + 9.999 EV
    plan = cs.plan()
    assert plan.other_load_kw == pytest.approx(470.0)
    assert plan.targets_w == {("CP_1", 1): 6666, ("CP_2", 1): 3333}


def test_ramp_down_lag_is_counted_conservatively(cfg):
    """After a cut the car still draws its old power for a few seconds. Only the accepted
    (lower) limit is subtracted, so the other loads look bigger, never smaller."""
    cs, _ = make(cfg)
    FakeCharger(cs, "CP_1")
    plug(cs, "CP_1", soc=22.0)
    site_load(cs, 470.0)
    run(cycle(cs))                                         # accepted 10000
    cs._record_limit(session(cs, "CP_1"), 2000)            # just cut to 2 kW...
    power(cs, "CP_1", 10000)                               # ...but still drawing 10 kW
    site_load(cs, 480.0)
    assert cs.plan().other_load_kw == pytest.approx(478.0)  # 480 - 2, not 480 - 10


def test_stale_or_suspended_ev_readings_are_not_subtracted(cfg):
    cs, clock = make(cfg)
    FakeCharger(cs, "CP_1")
    plug(cs, "CP_1", soc=22.0)
    site_load(cs, 470.0)
    run(cycle(cs))
    power(cs, "CP_1", 5000)
    clock.advance(cfg.telemetry_stale_after_s + 0.1)
    site_load(cs, 475.0)
    assert cs.plan().other_load_kw == 475.0                # reading too old to trust
    power(cs, "CP_1", 5000)
    result(cs, "CP_1", "StatusNotification", {"connectorId": 1, "errorCode": "NoError", "status": "SuspendedEV"})
    assert cs.plan().other_load_kw == 475.0                # car says it is not drawing


def test_unknown_car_reading_is_subtracted_until_it_has_a_limit(cfg):
    cs, _ = make(cfg)
    FakeCharger(cs, "CP_1")
    plug(cs, "CP_1", soc=22.0)
    power(cs, "CP_1", 9000)                                # charger default before any profile
    site_load(cs, 479.0)
    assert cs.plan().other_load_kw == pytest.approx(470.0)


def test_unknown_car_reading_capped_at_max_charger(cfg):
    """A charger default above the site cap, or a garbage reading, is only subtracted up to
    max_charger_kw: the rest stays counted as other load (safe direction)."""
    cs, _ = make(cfg)
    FakeCharger(cs, "CP_1")
    plug(cs, "CP_1", soc=22.0)
    power(cs, "CP_1", 11500)
    site_load(cs, 481.5)
    assert cs.plan().other_load_kw == pytest.approx(471.5)
    power(cs, "CP_1", 1e9)
    site_load(cs, 481.5)
    assert cs.plan().other_load_kw == pytest.approx(471.5)


def test_building_spike_cuts_power_in_the_same_cycle(cfg):
    cs, _ = make(cfg)
    cp1, cp2 = FakeCharger(cs, "CP_1"), FakeCharger(cs, "CP_2")
    plug(cs, "CP_1", soc=22.0)
    plug(cs, "CP_2", soc=67.0)

    async def scenario():
        site_load(cs, 470.0)
        await cycle(cs)
        power(cs, "CP_1", 6666)
        power(cs, "CP_2", 3333)
        site_load(cs, 485.999)                             # other loads jump to 476 kW
        await cycle(cs)
        assert cp1.limits()[-1][2] == 2666.0 and cp2.limits()[-1][2] == 1333.0 or \
            sum(limit for _, _, limit in (cp1.limits()[-1], cp2.limits()[-1])) <= 4000.0
    run(scenario())


def test_over_limit_ct_reading_wakes_the_loop_and_alerts(cfg, caplog):
    cs, _ = make(cfg)

    async def scenario():
        waiter = asyncio.create_task(cs.wait_for_next_cycle())
        await asyncio.sleep(0)
        site_load(cs, 470.0)                               # inside the limit: keep sleeping
        await asyncio.sleep(0.01)
        assert not waiter.done()
        site_load(cs, 481.0)                               # into the safety margin: wake now
        await asyncio.wait_for(waiter, 0.5)
        site_load(cs, 601.0)
    run(scenario())
    assert "above the 600.0 kW panel limit" in caplog.text


# ---------------------------------------------------------------- chatter, without hysteresis

def test_small_increase_skipped_small_decrease_sent_refresh_catches_up(cfg):
    cs, clock = make(cfg)
    cp = FakeCharger(cs, "CP_1")
    plug(cs, "CP_1", soc=22.0)

    async def scenario():
        site_load(cs, 475.0)                               # budget 5000
        await cycle(cs)
        assert cp.limits()[-1][2] == 5000.0
        site_load(cs, 474.95)                              # budget 5050: +50 W, below 100 W
        await cycle(cs)
        assert len(cp.profiles()) == 1
        site_load(cs, 475.001)                             # budget 4999: -1 W goes out at once
        await cycle(cs)
        assert cp.limits()[-1][2] == 4999.0
        site_load(cs, 474.9)                               # budget 5100: +101 W goes out
        await cycle(cs)
        assert cp.limits()[-1][2] == 5100.0
        site_load(cs, 474.85)                              # +50 W: held...
        await cycle(cs)
        assert cp.limits()[-1][2] == 5100.0
        clock.advance(cfg.profile_refresh_s)
        site_load(cs, 474.85)
        await cycle(cs)                                    # ...until the 30 s refresh
        assert cp.limits()[-1][2] == 5150.0
    run(scenario())


# ---------------------------------------------------------------- never over budget, mid-transition

def test_worst_case_draw_never_exceeds_budget_through_reallocation(cfg):
    """A busy site: priorities and building load change every cycle. At every moment the
    worst case (each car at the higher of its old and new limit) must fit the budget."""
    rng = __import__("random").Random(7)
    cs, clock = make(cfg)
    cars = [f"CP_{i}" for i in range(8)]
    for cid in cars:
        FakeCharger(cs, cid)
    for cid in cars:
        plug(cs, cid, soc=rng.choice([10.0, 50.0, 90.0]))

    async def scenario():
        site_load(cs, 470.0)
        for _ in range(3):                                 # settle from "unknown" to known limits
            await cycle(cs)
        for step in range(300):
            clock.advance(2)
            for cid in cars:
                if rng.random() < 0.3:
                    result(cs, cid, "MeterValues", {"soc": rng.choice([5.0, 45.0, 85.0, 99.0])})
            other = rng.uniform(430.0, 485.0)
            before = {cid: session(cs, cid).accepted_w for cid in cars}
            ev = sum(before.values()) / 1000
            site_load(cs, other + ev)
            plan = cs.plan()
            budget_w = gateway.watts_floor(plan.decision.budget_kw)
            await cycle(cs)
            after = {cid: session(cs, cid).accepted_w for cid in cars}
            worst = sum(max(before[c], after[c]) for c in cars)
            assert worst <= max(budget_w, sum(before.values())), (step, worst, budget_w)
            if sum(before.values()) <= budget_w:
                assert worst <= budget_w
    run(scenario())


def test_unanswered_increase_is_assumed_in_force(cfg):
    cfg = dataclasses.replace(cfg, call_timeout_s=0.05)
    cs, _ = make(cfg)
    answer = {"on": True}
    FakeCharger(cs, "CP_A", lambda m: ACCEPT if answer["on"] else None)
    FakeCharger(cs, "CP_B")
    plug(cs, "CP_A", soc=22.0)
    plug(cs, "CP_B", soc=22.0)

    async def scenario():
        site_load(cs, 476.0)                               # budget 4000: 2000 each
        await cycle(cs)
        answer["on"] = False
        session(cs, "CP_B").accepted_w = 0                 # B stops (e.g. car paused)...
        site_load(cs, 476.0)
        result(cs, "CP_B", "MeterValues", {"soc": 99.0})   # ...and drops to low priority
        await cycle(cs)                                    # A is offered more but never answers
        assert session(cs, "CP_A").uncertain_w is not None
        assert cs._committed_w(session(cs, "CP_A")) >= session(cs, "CP_A").uncertain_w
    run(scenario())


def test_new_car_with_unknown_limit_blocks_increases_for_one_cycle(cfg):
    cs, _ = make(cfg)
    a = FakeCharger(cs, "CP_A")
    plug(cs, "CP_A", soc=22.0)
    site_load(cs, 476.0)                                   # budget 4000

    async def scenario():
        await cycle(cs)
        assert a.limits()[-1][2] == 4000.0
        site_load(cs, 470.0)                               # budget 10000 ...
        FakeCharger(cs, "CP_B")
        plug(cs, "CP_B", soc=22.0)                         # ... but a new car may draw up to 10 kW
        await cycle(cs)
        assert a.limits()[-1][2] == 4000.0                 # A may not rise yet; B is set to 5000
        assert session(cs, "CP_B").accepted_w == 5000
        await cycle(cs)
        assert a.limits()[-1][2] == 5000.0
    run(scenario())


# ---------------------------------------------------------------- meter configuration

def test_meter_configuration_pushed_once_after_boot(cfg):
    cs, _ = make(cfg)
    cp = FakeCharger(cs, "CP_1")
    result(cs, "CP_1", "BootNotification", {"chargePointVendor": "V", "chargePointModel": "M"})

    async def scenario():
        await cycle(cs)
        await cycle(cs)
    run(scenario())
    configs = [m[3] for m in cp.sent if m[2] == "ChangeConfiguration"]
    assert configs == [{"key": "MeterValueSampleInterval", "value": "5"},
                       {"key": "MeterValuesSampledData",
                        "value": "Energy.Active.Import.Register,Power.Active.Import,Current.Import,SoC"}]


def test_meter_configuration_retries_without_soc(cfg, caplog):
    cs, _ = make(cfg)

    def script(message):
        if message[2] == "ChangeConfiguration" and "SoC" in message[3]["value"]:
            return REJECT
        return ACCEPT
    cp = FakeCharger(cs, "CP_1", script)
    result(cs, "CP_1", "BootNotification", {"chargePointVendor": "V", "chargePointModel": "M"})
    run(cycle(cs))
    values = [m[3]["value"] for m in cp.sent if m[2] == "ChangeConfiguration"]
    assert values[-1] == "Energy.Active.Import.Register,Power.Active.Import,Current.Import"
    assert not cs.chargers["CP_1"].needs_meter_config


def test_meter_configuration_unanswered_is_retried(cfg):
    cfg = dataclasses.replace(cfg, call_timeout_s=0.05)
    cs, _ = make(cfg)
    replies = iter([None])
    cp = FakeCharger(cs, "CP_1", lambda m: next(replies, ACCEPT))
    result(cs, "CP_1", "BootNotification", {"chargePointVendor": "V", "chargePointModel": "M"})

    async def scenario():
        await cycle(cs)
        assert [m[2] for m in cp.sent] == ["ChangeConfiguration"]
        assert cs.chargers["CP_1"].needs_meter_config
        await cycle(cs)
        assert not cs.chargers["CP_1"].needs_meter_config
    run(scenario())


def test_site_meter_is_not_reconfigured(cfg):
    cs, _ = make(cfg)
    meter = FakeCharger(cs, "SITE_METER")
    result(cs, "SITE_METER", "BootNotification", {"chargePointVendor": "V", "chargePointModel": "CT"})
    run(cycle(cs))
    assert meter.sent == []


def test_ct_and_charger_readings_are_paired_by_time(cfg):
    """CT read before a cut, charger read after it: the charger's pre-cut reading (nearest
    the CT instant) must be used, or a load spike that never happened is invented."""
    cs, clock = make(cfg)
    FakeCharger(cs, "CP_1")
    plug(cs, "CP_1", soc=22.0)
    site_load(cs, 470.0)
    run(cycle(cs))                                         # 10000 accepted
    clock.advance(0.5)
    power(cs, "CP_1", 10000)
    site_load(cs, 480.0)                                   # CT: 470 other + 10 EV
    clock.advance(0.5)
    cs._record_limit(session(cs, "CP_1"), 4000)            # cut after the CT reading...
    clock.advance(0.5)
    power(cs, "CP_1", 4000)                                # ...charger now reports 4 kW
    assert cs.plan().other_load_kw == pytest.approx(470.0)  # not 480 - 4 = 476


def test_no_charger_reading_near_the_ct_instant_counts_as_zero(cfg):
    cs, clock = make(cfg)
    FakeCharger(cs, "CP_1")
    plug(cs, "CP_1", soc=22.0)
    power(cs, "CP_1", 7000)
    clock.advance(cfg.telemetry_stale_after_s + 1)
    site_load(cs, 477.0)
    assert cs.plan().other_load_kw == 477.0


def test_negative_ct_reading_is_a_fault_with_zero_budget(cfg, caplog):
    """A clamp installed backwards reads negative. Trusting it would hand the EVs the whole
    panel; it must give a 0 budget and a single critical alert."""
    cs, _ = make(cfg)
    cp = FakeCharger(cs, "CP_1")
    plug(cs, "CP_1", soc=22.0)

    async def scenario():
        site_load(cs, -470.0)
        site_load(cs, -470.0)
        await cycle(cs)
        assert cp.limits()[-1][2] == 0.0
        site_load(cs, 470.0)
        await cycle(cs)
        assert cp.limits()[-1][2] == 10000.0
    run(scenario())
    alerts = [r for r in caplog.records if "negative" in r.getMessage() and r.levelno == logging.CRITICAL]
    assert len(alerts) == 1


def test_over_panel_alert_once_per_episode(cfg, caplog):
    cs, _ = make(cfg)
    for _ in range(5):
        site_load(cs, 650.0)
    site_load(cs, 470.0)
    site_load(cs, 650.0)
    alerts = [r for r in caplog.records if "above the 600.0 kW panel limit" in r.getMessage()]
    assert len(alerts) == 2


def test_equal_cars_on_a_tight_budget_first_come_first_served(cfg):
    """Budget for only one car: the car that plugged in first keeps charging, whatever the
    dictionary order, and the choice does not flip between cycles."""
    cs, clock = make(cfg)
    late, early = FakeCharger(cs, "CP_A"), FakeCharger(cs, "CP_Z")
    plug(cs, "CP_Z", soc=50.0)                             # arrives first
    clock.advance(60)
    plug(cs, "CP_A", soc=50.0)

    async def scenario():
        for _ in range(5):
            site_load(cs, 478.0)                           # 2.0 kW: 1000 W each is too little
            await cycle(cs)
            clock.advance(2)
            assert early.limits()[-1][2] == 2000.0 and late.limits()[-1][2] == 0.0
    run(scenario())


def test_charger_ignoring_its_limit_is_reported_once(cfg, caplog):
    cs, clock = make(cfg)
    FakeCharger(cs, "CP_1")
    plug(cs, "CP_1", soc=22.0)
    site_load(cs, 476.0)
    run(cycle(cs))                                         # 4000 accepted
    power(cs, "CP_1", 7000)                                # still ramping down: no report yet
    assert "over the 4000 W limit" not in caplog.text
    clock.advance(11)
    power(cs, "CP_1", 7000)
    power(cs, "CP_1", 7000)
    assert caplog.text.count("over the 4000 W limit") == 1


def test_many_chargers_stay_fast(cfg):
    """A large lot: 300 cars, every SoC tier, allocation well under one cycle."""
    import random
    import time as _time
    rng = random.Random(5)
    cs, _ = make(cfg)
    for i in range(300):
        FakeCharger(cs, f"CP_{i:03d}")
        plug(cs, f"CP_{i:03d}", soc=rng.uniform(0, 100))
    site_load(cs, 100.0)                                   # 380 kW for EVs
    started = _time.perf_counter()
    plan = cs.plan()
    elapsed = _time.perf_counter() - started
    assert sum(plan.targets_w.values()) <= 380000
    assert elapsed < 0.5, elapsed


def test_car_that_started_after_the_ct_reading_is_not_subtracted(cfg):
    """The CT was read before the car began drawing: the car's power is not in that
    reading, so subtracting it would invent spare capacity (the bug this guards)."""
    cs, clock = make(cfg)
    FakeCharger(cs, "CP_1")
    plug(cs, "CP_1", soc=22.0)
    site_load(cs, 470.0)
    clock.advance(0.2)
    power(cs, "CP_1", 11500)                               # starts at its default after the CT read
    assert cs.plan().other_load_kw == 470.0


def test_car_that_stopped_on_its_own_after_the_ct_reading_counts_the_lower_value(cfg):
    cs, clock = make(cfg)
    FakeCharger(cs, "CP_1")
    plug(cs, "CP_1", soc=22.0)
    site_load(cs, 470.0)
    run(cycle(cs))                                         # 10000 accepted
    clock.advance(0.5)
    power(cs, "CP_1", 9000)
    clock.advance(0.1)
    site_load(cs, 479.0)                                   # CT: 470 + 9
    clock.advance(0.1)
    power(cs, "CP_1", 1000)                                # tapered by itself, no cut from us
    assert cs.plan().other_load_kw == pytest.approx(478.0)  # 479 - 1: the safe side
