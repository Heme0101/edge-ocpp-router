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
        site_load(cs, 470.0)
        power(cs, "CP_1", 20)                              # near 0 W, but still in the grace period
        await cycle(cs)
        assert cp1.limits()[-1][2] == 6666.0
        clock.advance(1)
        power(cs, "CP_1", 20)
        await cycle(cs)
        assert cp1.limits()[-1][2] == 1400.0 and cp2.limits()[-1][2] == 8600.0
        clock.advance(5)
        site_load(cs, 470.0)
        power(cs, "CP_1", 1350)                            # drawing again: back to its full share
        await cycle(cs)
        assert cp1.limits()[-1][2] == 6666.0 and cp2.limits()[-1][2] == 3333.0
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
    assert cp.limits() == [("TxProfile", 1, 6000.0)]


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
        assert cp.limits()[-1][2] == 6000.0                # offline: tariff peak cap
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
    assert plan.reserved_kw == cfg.max_charger_kw and not plan.sessions
    result(cs, "CP_1", "StopTransaction", {"transactionId": tx, "meterStop": 10, "timestamp": "t", "reason": "Remote"})
    assert cs.plan().reserved_kw == 0.0                    # stopped: reservation released


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
        assert len(cp.profiles()) == 2 and session(cs, "CP_1").limit_kw == 10.0
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
        assert cp2.limits()[-1][2] == 3334.0
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
