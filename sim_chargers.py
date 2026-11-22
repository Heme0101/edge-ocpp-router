import asyncio
import json
import uuid
from datetime import datetime, timezone

from websockets.asyncio.client import connect

URL = "ws://localhost:9000"
CHARGERS = {"CP_1": 100.0, "CP_2": 0.0, "CP_3" : 400.0, "CP_4" : 83.1}
step = 2.0


async def call(ws, action, payload):
    await ws.send(json.dumps([2, str(uuid.uuid4()), action, payload]))


async def run_charger(charger_id: str, soc, start_soc: float, step : float):
    soc = start_soc
    async with connect(f"{URL}/{charger_id}", subprotocols=["ocpp1.6"]) as ws:
        await call(ws, "BootNotification",
                   {"chargePointVendor": "Sim", "chargePointModel": "Sim-1"})
        await call(ws, "MeterValues", {
            "connectorId": 1,
            "meterValue": [{
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "sampledValue": [{"value": str(soc), "measurand": "SoC", "unit": "Percent"}],
            }],
        })
        async for raw in ws:
            message = json.loads(raw)
            if message[0] != 2:
                continue                    # replies to our own calls
            _, msg_id, action, payload = message
            if action == "SetChargingProfile":
                period = payload["csChargingProfiles"]["chargingSchedule"]["chargingSchedulePeriod"][0]
                print(f"[{charger_id} | {soc:.1f}% SoC] Received limit: {period['limit']:.1f} W", flush=True)
                await ws.send(json.dumps([3, msg_id, {"status": "Accepted"}]))
                soc = min(100.0, soc + step)
                await ws.send(json.dumps([2, str(uuid.uuid4()), "MeterValues", {"soc":soc}]))
            else:
                await ws.send(json.dumps([4, msg_id, "NotImplemented", "", {}]))


async def main():
    await asyncio.gather(*(run_charger(cid, soc, soc, 2.0) for cid, soc in CHARGERS.items()))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass