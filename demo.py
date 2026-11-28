"""Interactive demo: the real controller, simulated chargers and CT meter, and the live
dashboard with demo controls, all in one process.

    python demo.py              then open http://localhost:8000

The dashboard's controls change the SIMULATED building: the base (non-EV) load, a
temporary HVAC spike, and the tariff peak window. The controller is not told about any
of it directly; it sees only what the simulated CT meter reports, exactly as on a real
site, and throttles the chargers from that.

The default site (site_config.demo.json) is a mid-size commercial building: 480 V
3-phase, 1200 A main breaker, 750 kVA transformer (675 kW continuous at power factor
0.9), a 600 kW limit with a 120 kW safety margin, and ten 48 A / 208 V chargers.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
from pathlib import Path

import dashboard
import server
from config import ConfigError, load_site_config
from sim_chargers import SimCharger, SimSiteMeter, parse_cars

DEMO_CONFIG_PATH = Path(__file__).with_name("site_config.demo.json")
DEFAULT_CARS = "CP_1:12,CP_2:25,CP_3:38,CP_4:47,CP_5:none,CP_6:58,CP_7:66,CP_8:74,CP_9:83,CP_10:none"


class DemoController:
    """What the dashboard's demo controls act on: the simulated meter and the peak window."""

    def __init__(self, cs: server.CentralSystem, meter: SimSiteMeter, *, max_base_load_kw=550.0,
                 spike_kw=150.0, spike_s=30.0):
        self.cs, self.meter = cs, meter
        self.max_base_load_kw, self.spike_kw, self.spike_s = max_base_load_kw, spike_kw, spike_s

    def set_base_load(self, kw):
        if isinstance(kw, bool) or not isinstance(kw, (int, float)) or not math.isfinite(kw):
            raise ValueError("kw must be a number")
        if not 0.0 <= kw <= self.max_base_load_kw:
            raise ValueError(f"kw must be from 0 to {self.max_base_load_kw:g}")
        self.meter.load_kw = float(kw)
        server.log.info("demo: building non-EV load set to %.1f kW", kw)

    def spike(self):
        self.meter.start_spike(self.spike_kw, self.spike_s)
        server.log.warning("demo: simulated %.0f kW HVAC spike for %.0f s", self.spike_kw, self.spike_s)

    def toggle_peak(self):
        self.set_peak(not self.cs.peak_active(self.cs.wall_clock()))

    def set_peak(self, active):
        if active is not None and not isinstance(active, bool):
            raise ValueError("active must be true, false or null")
        self.cs.set_peak_override(active)

    def state(self) -> dict:
        return {
            "enabled": True,
            "base_load_kw": round(self.meter.load_kw, 3),
            "max_base_load_kw": self.max_base_load_kw,
            "spike_kw": self.spike_kw,
            "spike_s": self.spike_s,
            "spike_remaining_s": round(self.meter.spike_remaining_s(), 1),
            "simulated_other_load_kw": round(self.meter.other_kw(), 3),
        }


def make_chargers(cfg, cars, time_scale):
    rating_kw = cfg.charger_voltage_v * cfg.charger_max_current_a / 1000
    return [SimCharger(charger_id, 50.0 if soc is None else soc, reports_soc=soc is not None,
                       battery_kwh=75.0, car_max_kw=11.5, evse_max_kw=rating_kw,
                       voltage_v=cfg.charger_voltage_v, time_scale=time_scale,
                       meter_interval_s=cfg.meter_sample_interval_s,
                       plug_in_delay_s=1.0 + 1.5 * i, quiet=True)
            for i, (charger_id, soc) in enumerate(cars)]


async def run(cfg, args):
    cs = server.CentralSystem(cfg)
    ws_server = await server.start_server(cs, cfg.host, cfg.port)
    url = f"ws://{cfg.host}:{ws_server.sockets[0].getsockname()[1]}"
    chargers = make_chargers(cfg, args.cars, args.time_scale)
    meter = SimSiteMeter(cfg.site_meter_id, args.base_load_kw, chargers=chargers, interval_s=1.0,
                         noise_pct=args.noise_pct)
    demo = DemoController(cs, meter, max_base_load_kw=args.max_base_load_kw,
                          spike_kw=args.spike_kw, spike_s=args.spike_s)
    web = await dashboard.start_dashboard(cs, cfg.host, cfg.dashboard_port, demo=demo)
    print(f"Dashboard: http://{cfg.host}:{web.port}   (Ctrl+C to stop)", flush=True)
    tasks = [asyncio.create_task(server.allocation_loop(cs)), asyncio.create_task(meter.run(url))]
    tasks += [asyncio.create_task(charger.run(url)) for charger in chargers]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await web.close()
        ws_server.close()
        await ws_server.wait_closed()
        cs.close_all_sessions("DemoStopped")


def main():
    parser = argparse.ArgumentParser(description="Live dashboard demo with simulated chargers")
    parser.add_argument("--config", default=str(DEMO_CONFIG_PATH))
    parser.add_argument("--cars", type=parse_cars, default=parse_cars(DEFAULT_CARS),
                        help="ID:SOC pairs; SOC 'none' = a car that reports no SoC")
    parser.add_argument("--base-load-kw", type=float, default=420.0, help="starting non-EV load")
    parser.add_argument("--max-base-load-kw", type=float, default=550.0, help="slider maximum")
    parser.add_argument("--spike-kw", type=float, default=150.0)
    parser.add_argument("--spike-s", type=float, default=30.0, help="how long a spike lasts")
    parser.add_argument("--noise-pct", type=float, default=0.5,
                        help="random variation of each CT reading, +/- %% of the base load")
    parser.add_argument("--time-scale", type=float, default=10.0,
                        help="simulated battery seconds per real second (SoC visibly rises)")
    parser.add_argument("--verbose", action="store_true", help="log every allocation cycle")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)-8s %(message)s")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    try:
        cfg = load_site_config(args.config)
    except ConfigError as error:
        raise SystemExit(f"config error: {error}")
    if not 0.0 <= args.base_load_kw <= args.max_base_load_kw:
        raise SystemExit("--base-load-kw must be from 0 to --max-base-load-kw")
    try:
        asyncio.run(run(cfg, args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
