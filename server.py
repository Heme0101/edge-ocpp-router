import asyncio
import json
from datetime import datetime, timezone

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

import gateway

HOST = "localhost"
PORT = 9000
BUILDING_LOAD_KW = 200.0
LOOP_SECONDS = 2
CONNECTOR_ID = 1

chargers = {}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def extract_soc(payload):
    """Find a battery % in a MeterValues / DataTransfer payload, or None.

    Accepts a plain "soc" key (also inside a DataTransfer "data" JSON string) and the
    standard OCPP form: meterValue[].sampledValue[] with measurand "SoC".
    """
    if not isinstance(payload, dict):
        return None
    for key, value in payload.items():
        if key.lower() == "soc":
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    data = payload.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = None
    if isinstance(data, dict):
        return extract_soc(data)
    for meter_value in payload.get("meterValue", []):
        for sample in meter_value.get("sampledValue", []):
            if str(sample.get("measurand", "")).lower() == "soc":
                try:
                    return float(sample["value"])
                except (KeyError, TypeError, ValueError):
                    return None
    return None


async def handle_call(charger_id, msg_id, action, payload):
    """Return the CALLRESULT for one incoming CALL."""
    if action == "BootNotification":
        return [3, msg_id, {"status": "Accepted", "currentTime": now_iso(), "interval": 10}]
    if action in ("MeterValues", "DataTransfer"):
        soc = extract_soc(payload)
        if soc is not None:
            chargers[charger_id]["soc"] = soc
        return [3, msg_id, {"status": "Accepted"} if action == "DataTransfer" else {}]
    if action == "Heartbeat":
        return [3, msg_id, {"currentTime": now_iso()}]
    return [4, msg_id, "NotImplemented", f"{action} is not supported", {}]


async def on_connect(ws):
    charger_id = ws.request.path.strip("/")
    chargers[charger_id] = {"ws": ws, "soc": None}
    print(f"[server] {charger_id} connected")
    try:
        async for raw in ws:
            try:
                message = json.loads(raw)
                message_type = message[0]
            except (ValueError, IndexError, TypeError):
                continue
            if message_type == 2:
                _, msg_id, action, payload = message
                await ws.send(json.dumps(await handle_call(charger_id, msg_id, action, payload)))
            # 3 / 4 are replies to our SetChargingProfile: nothing to do
    except ConnectionClosed:
        pass                                # charger dropped: normal
    finally:
        if chargers.get(charger_id, {}).get("ws") is ws:
            del chargers[charger_id]
        print(f"[server] {charger_id} disconnected")


async def allocation_loop():
    while True:
        await asyncio.sleep(LOOP_SECONDS)
        connected_socs = {cid: c["soc"] for cid, c in chargers.items() if c["soc"] is not None}
        if not connected_socs:
            continue
        budget_kw = gateway.available_ev_budget_kw(BUILDING_LOAD_KW)
        allocations = gateway.allocate_power(connected_socs, budget_kw)

        parts = []
        for cid, allocated_kw in allocations.items():
            message = gateway.build_charging_profile(CONNECTOR_ID, allocated_kw)
            watts = message[3]["csChargingProfiles"]["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"]
            parts.append(f"{cid} {connected_socs[cid]:.1f}% -> {watts:.0f} W")
            try:
                await chargers[cid]["ws"].send(json.dumps(message))
            except Exception as error:      # charger dropped mid-send
                print(f"[server] could not send to {cid}: {error}")
        print(f"[budget {budget_kw:.1f} kW] " + " | ".join(parts), flush=True)


async def main():
    async with serve(on_connect, HOST, PORT, subprotocols=["ocpp1.6"]):
        print(f"[server] OCPP 1.6J listening on ws://{HOST}:{PORT}")
        await allocation_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass