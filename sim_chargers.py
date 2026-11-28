"""Simulated OCPP 1.6J chargers and a simulated building CT meter, for demos and tests.

Each charger plugs a car in, starts a transaction, obeys the limits it is sent, and
charges a battery with exactly the power it is allowed: SoC rises by
watts x seconds / battery size, so a car given 0 W gains nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
import uuid
from datetime import datetime, timezone

from websockets.asyncio.client import connect

from config import DEFAULT_CONFIG_PATH, load_site_config
from gateway import CHARGE_POINT_MAX_PROFILE, TX_DEFAULT_PROFILE, TX_PROFILE

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
                 evse_max_kw=None, voltage_v=208.0, time_scale=1.0, meter_interval_s=5.0,
                 plug_in_delay_s=0.0, reject=(), quiet=False):
        self.plug_in_delay_s = plug_in_delay_s    # boot first, then the car arrives this much later
        self.charger_id = charger_id
        self.soc = soc                      # the battery's true level, tracked even if unreported
        self.reports_soc = reports_soc      # False: a J1772 car that does not tell its SoC
        self.battery_kwh, self.car_max_w = battery_kwh, car_max_kw * 1000.0
        # The charger's own hardware rating (e.g. 32 A x 208 V); None = no limit of its own.
        self.evse_max_w = float("inf") if evse_max_kw is None else evse_max_kw * 1000.0
        self.voltage_v = voltage_v          # used to report Current.Import
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
        self.configuration = {}             # keys set by ChangeConfiguration
        self._followups = set()

    @property
    def label(self):
        level = f"{self.soc:.1f}% SoC" if self.reports_soc else "no SoC"
        return f"[{self.charger_id} | {level}]"

    def say(self, text):
        if not self.quiet:
            print(f"{self.label} {text}", flush=True)

    def effective_limit_w(self):
        """OCPP stacking, simplified to one connector: a TxProfile overrides a TxDefaultProfile
        on the connector, which overrides the site default on connector 0, and a
        ChargePointMaxProfile caps whatever is left."""
        tx = self.limits.get((TX_PROFILE, 1))
        if tx is None:
            tx = self.limits.get((TX_DEFAULT_PROFILE, 1), self.limits.get((TX_DEFAULT_PROFILE, 0)))
        limits = [w for w in (tx, self.limits.get((CHARGE_POINT_MAX_PROFILE, 0))) if w is not None]
        return min(limits) if limits else float("inf")     # no profile yet: hardware limits only

    def power_w(self):
        if not self.charging or self.soc >= 100.0:
            return 0.0                      # stopped, or full: the car tapers to nothing
        # The lowest of: the limit we were sent, the charger's rating, the car's onboard charger.
        return min(self.effective_limit_w(), self.evse_max_w, self.car_max_w)

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
            self.limits[(purpose, payload["connectorId"])] = limit
            self.received.append((purpose, limit))
            if purpose == TX_DEFAULT_PROFILE and payload["connectorId"] == 0:
                self.say(f"Default limit for new sessions: {limit:.1f} W")
            else:
                suffix = "" if purpose == TX_PROFILE else f" ({purpose})"
                self.say(f"Received limit: {limit:.1f} W{suffix}")
            return {"status": "Accepted"}
        if action == "ChangeConfiguration":
            if payload.get("key") == "MeterValueSampleInterval":
                self.meter_interval_s = float(payload["value"])
            self.configuration[payload.get("key")] = payload.get("value")
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
        self.limits.pop((TX_PROFILE, 1), None)      # a TxProfile ends with its transaction
        await self._set_status("Finishing")

    async def _send_meter_values(self):
        watts = self.power_w()
        samples = [
            {"value": f"{self.energy_wh:.1f}", "measurand": "Energy.Active.Import.Register", "unit": "Wh"},
            {"value": f"{watts:.1f}", "measurand": "Power.Active.Import", "unit": "W"},
            {"value": f"{watts / self.voltage_v:.2f}", "measurand": "Current.Import", "unit": "A"},
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
                await asyncio.sleep(self.plug_in_delay_s)
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
    """The CT clamp on the building's main feeder. Like the real one it measures the WHOLE
    site: the other loads plus whatever the simulated chargers draw right now. Reports
    Power.Active.Import on connector 0 (OCPP's main-meter connector). Stops reporting
    after dropout_after_s, if set, to demonstrate the stale-telemetry floor.

    The other loads are load_kw (the building's base load), plus an HVAC compressor of
    hvac_kw that cycles on for hvac_period_s and off for hvac_period_s, plus random
    variation of up to +/- noise_pct % of the base load, drawn anew for each reading,
    plus any temporary spike started with start_spike()."""

    def __init__(self, meter_id, load_kw, *, chargers=(), interval_s=5.0, dropout_after_s=None,
                 hvac_kw=0.0, hvac_period_s=None, noise_pct=0.0, seed=None, quiet=False):
        self.meter_id, self.load_kw, self.chargers = meter_id, load_kw, list(chargers)
        self.interval_s, self.dropout_after_s, self.quiet = interval_s, dropout_after_s, quiet
        self.hvac_kw, self.hvac_period_s, self.noise_pct = hvac_kw, hvac_period_s, noise_pct
        self._rng = random.Random(seed)
        self._noise_kw = 0.0
        self._started = time.monotonic()
        self.spike_kw, self.spike_until = 0.0, None

    def start_spike(self, kw, seconds):
        """Add kw to the other loads for the next `seconds` (e.g. a large HVAC unit starting)."""
        self.spike_kw, self.spike_until = kw, time.monotonic() + seconds

    def spike_remaining_s(self):
        if self.spike_until is None:
            return 0.0
        return max(0.0, self.spike_until - time.monotonic())

    def hvac_on(self):
        if not self.hvac_kw or not self.hvac_period_s:
            return False
        return (time.monotonic() - self._started) % (2 * self.hvac_period_s) < self.hvac_period_s

    def other_kw(self):
        spike = self.spike_kw if self.spike_remaining_s() > 0.0 else 0.0
        return max(0.0, self.load_kw + (self.hvac_kw if self.hvac_on() else 0.0) + spike + self._noise_kw)

    def total_kw(self):
        return self.other_kw() + sum(charger.power_w() for charger in self.chargers) / 1000.0

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
                    self._noise_kw = self._rng.uniform(-1.0, 1.0) * self.noise_pct / 100 * self.load_kw
                    await client.call("MeterValues", {"connectorId": 0, "meterValue": [{
                        "timestamp": now_iso(),
                        "sampledValue": [{"value": f"{self.total_kw():.3f}",
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
    chargers = []
    for charger_id, soc in args.cars:
        reject = set()
        if charger_id in args.reject_tx or charger_id in args.reject_all:
            reject |= {TX_PROFILE, TX_DEFAULT_PROFILE}
        if charger_id in args.reject_all:
            reject.add(CHARGE_POINT_MAX_PROFILE)
        charger = SimCharger(charger_id, soc if soc is not None else args.hidden_soc,
                             reports_soc=soc is not None, battery_kwh=args.battery_kwh,
                             car_max_kw=args.car_max_kw, evse_max_kw=args.charger_kw,
                             voltage_v=args.charger_voltage, time_scale=args.time_scale,
                             meter_interval_s=args.meter_interval, reject=reject,
                             plug_in_delay_s=random.uniform(0.0, args.arrival_spread))
        chargers.append(charger)
    meter = SimSiteMeter(args.site_meter_id, args.building_load_kw, chargers=chargers,
                         interval_s=args.ct_interval, dropout_after_s=args.ct_dropout_after,
                         hvac_kw=args.hvac_kw, hvac_period_s=args.hvac_period, noise_pct=args.noise_pct)
    await asyncio.gather(meter.run(url), *(charger.run(url) for charger in chargers))


if __name__ == "__main__":
    cfg = load_site_config(DEFAULT_CONFIG_PATH)
    parser = argparse.ArgumentParser(description="Simulated OCPP 1.6J chargers + building CT meter")
    parser.add_argument("--url", default=f"ws://{cfg.host}:{cfg.port}")
    parser.add_argument("--cars", type=parse_cars,
                        default=parse_cars("CP_1:15,CP_2:22,CP_3:48,CP_4:67,CP_5:none,CP_6:85"),
                        help="ID:SOC pairs; SOC 'none' = a car that reports no SoC (J1772)")
    parser.add_argument("--building-load-kw", type=float, default=55.0,
                        help="the building's base load (non-EV); the CT reports the total")
    parser.add_argument("--hvac-kw", type=float, default=15.0,
                        help="an HVAC compressor that cycles on and off on top of the base load")
    parser.add_argument("--hvac-period", type=float, default=60.0, help="seconds on, then seconds off")
    parser.add_argument("--noise-pct", type=float, default=2.0,
                        help="random variation of each CT reading, +/- %% of the base load")
    parser.add_argument("--charger-kw", type=float,
                        default=cfg.charger_voltage_v * cfg.charger_max_current_a / 1000,
                        help="each charger's hardware rating (default from site_config.json)")
    parser.add_argument("--charger-voltage", type=float, default=cfg.charger_voltage_v)
    parser.add_argument("--arrival-spread", type=float, default=20.0,
                        help="cars plug in at random times within this many seconds of startup")
    parser.add_argument("--ct-interval", type=float, default=1.0, help="seconds between CT readings")
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
