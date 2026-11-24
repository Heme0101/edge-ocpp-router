"""Simulated OCPP 1.6J chargers and a simulated building CT meter, for demos and tests.

Each charger plugs a car in, starts a transaction, obeys the limits it is sent, and
charges a battery with exactly the power it is allowed: SoC rises by
watts x seconds / battery size, so a car given 0 W gains nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import datetime, timezone

from websockets.asyncio.client import connect

from config import DEFAULT_CONFIG_PATH, load_site_config
from gateway import CHARGE_POINT_MAX_PROFILE, TX_DEFAULT_PROFILE, TX_PROFILE

LINE_VOLTAGE = 240.0                # US Level-2 AC, used to report Current.Import
TICK_S = 0.5                        # physics step (real seconds)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class OcppClient:
    """Minimal OCPP-J client: send CALLs and await replies; answer the server's CALLs."""

    def __init__(self, ws, on_call):
        self.ws, self.on_call, self.pending = ws, on_call, {}

    async def call(self, action, payload, timeout=10.0):
        message_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[message_id] = future
        await self.ws.send(json.dumps([2, message_id, action, payload]))
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(message_id, None)

    async def receive_forever(self):
        async for raw in self.ws:
            message = json.loads(raw)
            if message[0] == 2:
                _, message_id, action, payload = message
                result = self.on_call(action, payload)
                if result is None:
                    reply = [4, message_id, "NotImplemented", f"{action} not simulated", {}]
                else:
                    reply = [3, message_id, result]
                await self.ws.send(json.dumps(reply))
            elif message[1] in self.pending:
                future = self.pending.pop(message[1])
                if message[0] == 3:
                    future.set_result(message[2])
                else:
                    future.set_exception(RuntimeError(f"CALLERROR {message[2]}: {message[3]}"))


class SimCharger:
    def __init__(self, charger_id, soc, *, reports_soc=True, battery_kwh=60.0, car_max_kw=11.5,
                 time_scale=1.0, meter_interval_s=5.0, reject=(), quiet=False):
        self.charger_id = charger_id
        self.soc = soc                      # the battery's true level, tracked even if unreported
        self.reports_soc = reports_soc      # False: a J1772 car that does not tell its SoC
        self.battery_kwh, self.car_max_w = battery_kwh, car_max_kw * 1000.0
        self.time_scale, self.meter_interval_s = time_scale, meter_interval_s
        self.reject = set(reject)           # profile purposes this charger answers Rejected to
        self.quiet = quiet
        self.energy_wh = 0.0                # energy register
        self.limits = {}                    # purpose -> W
        self.transaction_id = None
        self.charging = False
        self.status = None
        self.received = []                  # (purpose, W) of every profile, for tests
        self.remote_stopped = False
        self.client = None
        self._followups = set()

    @property
    def label(self):
        level = f"{self.soc:.1f}% SoC" if self.reports_soc else "no SoC"
        return f"[{self.charger_id} | {level}]"

    def say(self, text):
        if not self.quiet:
            print(f"{self.label} {text}", flush=True)

    def effective_limit_w(self):
        """OCPP stacking, simplified to one connector: a TxProfile overrides the
        TxDefaultProfile, and a ChargePointMaxProfile caps whatever is left."""
        tx = self.limits.get(TX_PROFILE, self.limits.get(TX_DEFAULT_PROFILE))
        limits = [w for w in (tx, self.limits.get(CHARGE_POINT_MAX_PROFILE)) if w is not None]
        return min(limits) if limits else self.car_max_w     # no profile yet: car's own max

    def power_w(self):
        if not self.charging or self.soc >= 100.0:
            return 0.0                      # stopped, or full: the car tapers to nothing
        return min(self.effective_limit_w(), self.car_max_w)

    def step(self, real_seconds):
        seconds = real_seconds * self.time_scale
        watts = self.power_w()
        self.energy_wh += watts * seconds / 3600.0
        # watts x seconds / battery size (kWh -> watt-seconds), as a percentage
        self.soc = min(100.0, self.soc + watts * seconds / (self.battery_kwh * 3_600_000.0) * 100.0)

    def on_call(self, action, payload):
        if action == "SetChargingProfile":
            profile = payload["csChargingProfiles"]
            purpose = profile["chargingProfilePurpose"]
            limit = profile["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"]
            if purpose in self.reject:
                self.say(f"Rejected {purpose} ({limit:.1f} W)")
                return {"status": "Rejected"}
            if purpose == TX_PROFILE and profile.get("transactionId") != self.transaction_id:
                return {"status": "Rejected"}       # real chargers refuse a wrong transaction
            self.limits[purpose] = limit
            self.received.append((purpose, limit))
            suffix = "" if purpose == TX_PROFILE else f" ({purpose})"
            self.say(f"Received limit: {limit:.1f} W{suffix}")
            return {"status": "Accepted"}
        if action == "RemoteStopTransaction":
            if payload.get("transactionId") != self.transaction_id or not self.charging:
                return {"status": "Rejected"}
            self.say("RemoteStopTransaction accepted: stopping")
            self._later(self._stop_transaction("Remote"))
            return {"status": "Accepted"}
        return None

    def _later(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._followups.add(task)
        task.add_done_callback(self._followups.discard)

    async def _set_status(self, status):
        self.status = status
        await self.client.call("StatusNotification",
                               {"connectorId": 1, "errorCode": "NoError", "status": status,
                                "timestamp": now_iso()})

    async def _stop_transaction(self, reason):
        self.charging = False
        self.remote_stopped = reason == "Remote"
        await self.client.call("StopTransaction", {
            "transactionId": self.transaction_id, "meterStop": int(self.energy_wh),
            "timestamp": now_iso(), "reason": reason})
        self.limits.pop(TX_PROFILE, None)           # a TxProfile ends with its transaction
        await self._set_status("Finishing")

    async def _send_meter_values(self):
        watts = self.power_w()
        samples = [
            {"value": f"{self.energy_wh:.1f}", "measurand": "Energy.Active.Import.Register", "unit": "Wh"},
            {"value": f"{watts:.1f}", "measurand": "Power.Active.Import", "unit": "W"},
            {"value": f"{watts / LINE_VOLTAGE:.2f}", "measurand": "Current.Import", "unit": "A"},
        ]
        if self.reports_soc:
            samples.insert(0, {"value": f"{self.soc:.2f}", "measurand": "SoC", "unit": "Percent",
                               "location": "EV"})
        payload = {"connectorId": 1, "meterValue": [{"timestamp": now_iso(), "sampledValue": samples}]}
        if self.transaction_id is not None:
            payload["transactionId"] = self.transaction_id
        await self.client.call("MeterValues", payload)

    async def run(self, url):
        async with connect(f"{url}/{self.charger_id}", subprotocols=["ocpp1.6"]) as ws:
            self.client = OcppClient(ws, self.on_call)
            receiver = asyncio.create_task(self.client.receive_forever())
            physics = None
            try:
                await self.client.call("BootNotification",
                                       {"chargePointVendor": "Sim", "chargePointModel": "Sim-L2"})
                await self._set_status("Preparing")
                reply = await self.client.call("StartTransaction", {
                    "connectorId": 1, "idTag": f"SIM-{self.charger_id}",
                    "meterStart": int(self.energy_wh), "timestamp": now_iso()})
                self.transaction_id, self.charging = reply["transactionId"], True
                await self._set_status("Charging")
                await self._send_meter_values()
                physics = asyncio.create_task(self._physics())
                await receiver                      # until the server closes the connection
            finally:
                for task in (receiver, physics, *self._followups):
                    if task is not None:
                        task.cancel()

    async def _physics(self):
        loop = asyncio.get_running_loop()
        last_tick = last_meter = loop.time()
        while True:
            await asyncio.sleep(min(TICK_S, self.meter_interval_s))
            now = loop.time()
            self.step(now - last_tick)
            last_tick = now
            if self.charging and self.soc >= 100.0 and self.status != "SuspendedEV":
                self.say("battery full")
                await self._set_status("SuspendedEV")
            if now - last_meter >= self.meter_interval_s:
                last_meter = now
                await self._send_meter_values()


class SimSiteMeter:
    """The building's CT meter: reports the non-EV building load as Power.Active.Import on
    connector 0 (OCPP's main-meter connector). Stops reporting after dropout_after_s, if
    set, to demonstrate the stale-telemetry floor."""

    def __init__(self, meter_id, load_kw, *, interval_s=5.0, dropout_after_s=None, quiet=False):
        self.meter_id, self.load_kw = meter_id, load_kw
        self.interval_s, self.dropout_after_s, self.quiet = interval_s, dropout_after_s, quiet

    async def run(self, url):
        async with connect(f"{url}/{self.meter_id}", subprotocols=["ocpp1.6"]) as ws:
            client = OcppClient(ws, lambda action, payload: None)
            receiver = asyncio.create_task(client.receive_forever())
            try:
                await client.call("BootNotification",
                                  {"chargePointVendor": "Sim", "chargePointModel": "Sim-CT"})
                started = asyncio.get_running_loop().time()
                while True:
                    elapsed = asyncio.get_running_loop().time() - started
                    if self.dropout_after_s is not None and elapsed >= self.dropout_after_s:
                        if not self.quiet:
                            print(f"[{self.meter_id}] CT telemetry dropout: no more readings", flush=True)
                        await receiver
                        return
                    await client.call("MeterValues", {"connectorId": 0, "meterValue": [{
                        "timestamp": now_iso(),
                        "sampledValue": [{"value": f"{self.load_kw:.1f}",
                                          "measurand": "Power.Active.Import", "unit": "kW"}]}]})
                    await asyncio.sleep(self.interval_s)
            finally:
                receiver.cancel()


def parse_cars(text):
    """'CP_1:22,CP_2:67,CP_3:none' -> [('CP_1', 22.0), ('CP_2', 67.0), ('CP_3', None)]"""
    cars = []
    for item in text.split(","):
        charger_id, _, soc = item.strip().partition(":")
        if not charger_id or not soc:
            raise argparse.ArgumentTypeError(f"expected ID:SOC, got {item!r}")
        if soc.lower() == "none":
            cars.append((charger_id, None))
            continue
        try:
            value = float(soc)
        except ValueError:
            raise argparse.ArgumentTypeError(f"SoC must be a number or 'none', got {soc!r}")
        cars.append((charger_id, value))
    return cars


async def main(args):
    url = args.url.rstrip("/")
    tasks = [SimSiteMeter(args.site_meter_id, args.building_load_kw, interval_s=args.meter_interval,
                          dropout_after_s=args.ct_dropout_after).run(url)]
    for charger_id, soc in args.cars:
        reject = set()
        if charger_id in args.reject_tx or charger_id in args.reject_all:
            reject |= {TX_PROFILE, TX_DEFAULT_PROFILE}
        if charger_id in args.reject_all:
            reject.add(CHARGE_POINT_MAX_PROFILE)
        charger = SimCharger(charger_id, soc if soc is not None else args.hidden_soc,
                             reports_soc=soc is not None, battery_kwh=args.battery_kwh,
                             car_max_kw=args.car_max_kw, time_scale=args.time_scale,
                             meter_interval_s=args.meter_interval, reject=reject)
        tasks.append(charger.run(url))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    cfg = load_site_config(DEFAULT_CONFIG_PATH)
    parser = argparse.ArgumentParser(description="Simulated OCPP 1.6J chargers + building CT meter")
    parser.add_argument("--url", default=f"ws://{cfg.host}:{cfg.port}")
    parser.add_argument("--cars", type=parse_cars, default=parse_cars("CP_1:22,CP_2:67"),
                        help="ID:SOC pairs; SOC 'none' = a car that reports no SoC (J1772)")
    parser.add_argument("--building-load-kw", type=float, default=470.0)
    parser.add_argument("--site-meter-id", default=cfg.site_meter_id)
    parser.add_argument("--battery-kwh", type=float, default=60.0)
    parser.add_argument("--car-max-kw", type=float, default=11.5, help="the car's own onboard limit")
    parser.add_argument("--hidden-soc", type=float, default=50.0,
                        help="true starting level of 'none' cars (never reported)")
    parser.add_argument("--meter-interval", type=float, default=5.0, help="seconds between MeterValues")
    parser.add_argument("--time-scale", type=float, default=1.0,
                        help="simulated seconds per real second (e.g. 600 to watch cars fill up)")
    parser.add_argument("--reject-tx", nargs="*", default=[], metavar="ID",
                        help="these chargers reject TxProfile/TxDefaultProfile")
    parser.add_argument("--reject-all", nargs="*", default=[], metavar="ID",
                        help="these chargers reject every profile (triggers RemoteStopTransaction)")
    parser.add_argument("--ct-dropout-after", type=float, default=None, metavar="SECONDS",
                        help="stop the CT meter's readings after this long")
    try:
        asyncio.run(main(parser.parse_args()))
    except KeyboardInterrupt:
        pass
