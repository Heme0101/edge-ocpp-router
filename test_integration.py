"""End to end over real websockets on 127.0.0.1 (loopback only; no internet needed):
the real server, the real simulated chargers and the simulated CT meter."""
import asyncio
import dataclasses

import server
from conftest import OFF_PEAK
from sim_chargers import SimCharger, SimSiteMeter


def fast(cfg, **changes):
    return dataclasses.replace(cfg, allocation_interval_s=0.1, call_timeout_s=2.0, **changes)


async def run_site(cfg, chargers, meter, seconds):
    cs = server.CentralSystem(cfg, clock=lambda: OFF_PEAK)
    ws_server = await server.start_server(cs, "127.0.0.1", 0)
    url = f"ws://127.0.0.1:{ws_server.sockets[0].getsockname()[1]}"
    tasks = [asyncio.create_task(server.allocation_loop(cs)), asyncio.create_task(meter.run(url))]
    tasks += [asyncio.create_task(c.run(url)) for c in chargers]
    try:
        await asyncio.sleep(seconds)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        ws_server.close()
        await ws_server.wait_closed()
    return cs


def test_demo_split_end_to_end(cfg):
    cp1 = SimCharger("CP_1", 22.0, quiet=True)
    cp2 = SimCharger("CP_2", 67.0, quiet=True)
    meter = SimSiteMeter("SITE_METER", 470.0, interval_s=0.2, quiet=True)
    cs = asyncio.run(run_site(fast(cfg), [cp1, cp2], meter, 1.5))
    assert ("TxProfile", 6666.0) in cp1.received
    assert ("TxProfile", 3333.0) in cp2.received
    assert cp1.received[-1] == ("TxProfile", 6666.0)
    s = cs.chargers["CP_1"].connectors[1].session
    assert s is not None and s.transaction_id == cp1.transaction_id and s.soc is not None


def test_charger_rejecting_everything_is_remote_stopped(cfg):
    good = SimCharger("CP_1", 22.0, quiet=True)
    bad = SimCharger("CP_2", 67.0, quiet=True,
                     reject={"TxProfile", "TxDefaultProfile", "ChargePointMaxProfile"})
    meter = SimSiteMeter("SITE_METER", 470.0, interval_s=0.2, quiet=True)
    cs = asyncio.run(run_site(fast(cfg), [good, bad], meter, 1.5))
    assert bad.remote_stopped and not bad.charging
    stopped = cs.chargers["CP_2"].connectors[1].session
    assert stopped.charging_stopped and stopped.stop_reason == "Remote"
    assert good.received[-1] == ("TxProfile", 10000.0)     # all 10 kW go to the good charger


def test_charger_rejecting_tx_profile_uses_charge_point_max(cfg):
    picky = SimCharger("CP_1", 22.0, quiet=True, reject={"TxProfile", "TxDefaultProfile"})
    meter = SimSiteMeter("SITE_METER", 470.0, interval_s=0.2, quiet=True)
    cs = asyncio.run(run_site(fast(cfg), [picky], meter, 1.0))
    assert ("ChargePointMaxProfile", 10000.0) in picky.received
    assert picky.charging and cs.chargers["CP_1"].use_max_profile


def test_ct_dropout_throttles_to_floor(cfg):
    cp1 = SimCharger("CP_1", 22.0, quiet=True)
    meter = SimSiteMeter("SITE_METER", 470.0, interval_s=0.1, dropout_after_s=0.3, quiet=True)
    asyncio.run(run_site(fast(cfg, telemetry_stale_after_s=0.4), [cp1], meter, 1.6))
    assert ("TxProfile", 10000.0) in cp1.received
    assert cp1.received[-1] == ("TxProfile", 2000.0)


def test_battery_rises_with_power_received_and_not_without():
    car = SimCharger("CP_1", 50.0, battery_kwh=60.0, quiet=True)
    car.charging = True
    car.limits["TxProfile"] = 6000.0
    car.step(3600)                          # 6 kW for an hour into 60 kWh = +10 %
    assert abs(car.soc - 60.0) < 1e-9 and abs(car.energy_wh - 6000.0) < 1e-6
    car.limits["TxProfile"] = 0.0
    car.step(3600)                          # 0 W gains nothing
    assert abs(car.soc - 60.0) < 1e-9
    car.limits["TxProfile"] = 20000.0       # never more than the car's own maximum
    car.soc = 99.99
    car.step(3600)
    assert car.soc == 100.0 and car.power_w() == 0.0
