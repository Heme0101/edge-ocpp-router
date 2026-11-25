"""End to end over real websockets on 127.0.0.1 (loopback only; no internet needed):
the real server, the real simulated chargers and the simulated CT meter."""
import asyncio
import dataclasses

import server
from conftest import OFF_PEAK
from sim_chargers import SimCharger, SimSiteMeter


CT_RESOLUTION_KW = 0.001                    # the simulated CT reports whole watts (3 decimals of kW)


def fast(cfg, **changes):
    changes.setdefault("meter_sample_interval_s", 1)       # fresh EV readings, as on a tuned site
    return dataclasses.replace(cfg, allocation_interval_s=0.1, call_timeout_s=2.0, **changes)


def quick_cars(*cars):
    for car in cars:
        car.meter_interval_s = 0.1          # fresh EV power readings for the CT subtraction
    return cars


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
    cp1, cp2 = quick_cars(SimCharger("CP_1", 22.0, quiet=True), SimCharger("CP_2", 67.0, quiet=True))
    meter = SimSiteMeter("SITE_METER", 470.0, chargers=[cp1, cp2], interval_s=0.2, quiet=True)
    cs = asyncio.run(run_site(fast(cfg), [cp1, cp2], meter, 2.0))
    assert ("TxProfile", 6666.0) in cp1.received
    assert ("TxProfile", 3333.0) in cp2.received
    assert cp1.received[-1] == ("TxProfile", 6666.0)
    s = cs.chargers["CP_1"].connectors[1].session
    assert s is not None and s.transaction_id == cp1.transaction_id and s.soc is not None


def test_charger_rejecting_everything_is_remote_stopped(cfg):
    good = SimCharger("CP_1", 22.0, quiet=True)
    bad = SimCharger("CP_2", 67.0, quiet=True,
                     reject={"TxProfile", "TxDefaultProfile", "ChargePointMaxProfile"})
    meter = SimSiteMeter("SITE_METER", 470.0, chargers=[good, bad], interval_s=0.2, quiet=True)
    cs = asyncio.run(run_site(fast(cfg), [good, bad], meter, 2.0))
    assert bad.remote_stopped and not bad.charging
    stopped = cs.chargers["CP_2"].connectors[1].session
    assert stopped.charging_stopped and stopped.stop_reason == "Remote"
    assert good.received[-1] == ("TxProfile", 10000.0)     # all 10 kW go to the good charger


def test_charger_rejecting_tx_profile_uses_charge_point_max(cfg):
    picky = SimCharger("CP_1", 22.0, quiet=True, reject={"TxProfile", "TxDefaultProfile"})
    meter = SimSiteMeter("SITE_METER", 470.0, chargers=[picky], interval_s=0.2, quiet=True)
    cs = asyncio.run(run_site(fast(cfg), [picky], meter, 1.5))
    assert ("ChargePointMaxProfile", 10000.0) in picky.received
    assert picky.charging and cs.chargers["CP_1"].use_max_profile


def test_ct_dropout_throttles_to_floor(cfg):
    cp1 = SimCharger("CP_1", 22.0, quiet=True)
    meter = SimSiteMeter("SITE_METER", 470.0, chargers=[cp1], interval_s=0.1, dropout_after_s=0.3, quiet=True)
    asyncio.run(run_site(fast(cfg, telemetry_stale_after_s=1.5, meter_sample_interval_s=1), [cp1], meter, 3.0))
    assert ("TxProfile", 10000.0) in cp1.received
    assert cp1.received[-1] == ("TxProfile", 2000.0)


def test_battery_rises_with_power_received_and_not_without():
    car = SimCharger("CP_1", 50.0, battery_kwh=60.0, quiet=True)
    car.charging = True
    car.limits[("TxProfile", 1)] = 6000.0
    car.step(3600)                          # 6 kW for an hour into 60 kWh = +10 %
    assert abs(car.soc - 60.0) < 1e-9 and abs(car.energy_wh - 6000.0) < 1e-6
    car.limits[("TxProfile", 1)] = 0.0
    car.step(3600)                          # 0 W gains nothing
    assert abs(car.soc - 60.0) < 1e-9
    car.limits[("TxProfile", 1)] = 20000.0       # never more than the car's own maximum
    car.soc = 99.99
    car.step(3600)
    assert car.soc == 100.0 and car.power_w() == 0.0


def test_building_spike_throttles_sharply_end_to_end(cfg):
    cp1, cp2 = quick_cars(SimCharger("CP_1", 22.0, quiet=True), SimCharger("CP_2", 67.0, quiet=True))
    meter = SimSiteMeter("SITE_METER", 470.0, chargers=[cp1, cp2], interval_s=0.1, quiet=True)

    async def scenario():
        cs = server.CentralSystem(fast(cfg), clock=lambda: OFF_PEAK)
        ws_server = await server.start_server(cs, "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{ws_server.sockets[0].getsockname()[1]}"
        tasks = [asyncio.create_task(server.allocation_loop(cs)), asyncio.create_task(meter.run(url)),
                 asyncio.create_task(cp1.run(url)), asyncio.create_task(cp2.run(url))]
        try:
            await asyncio.sleep(1.5)
            assert (cp1.power_w(), cp2.power_w()) == (6666.0, 3333.0)
            meter.load_kw = 476.0                          # other loads jump by 6 kW
            await asyncio.sleep(1.0)
            # New 4 kW budget: 3:1.5 would give CP_2 1333 W, under the 1440 W minimum,
            # so the higher-priority CP_1 gets all of it.
            assert (cp1.power_w(), cp2.power_w()) == (4000.0, 0.0)
            meter.load_kw = 470.0                          # load drops again
            await asyncio.sleep(1.5)
            assert (cp1.power_w(), cp2.power_w()) == (6666.0, 3333.0)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            ws_server.close()
            await ws_server.wait_closed()
    asyncio.run(scenario())


def test_ct_never_exceeds_usable_capacity_under_random_load(cfg):
    """Other loads wander randomly near the limit; the CT total (EVs included) is sampled
    the whole time and must stay within panel - margin, apart from the instant of a jump."""
    import random
    rng = random.Random(3)
    cars = quick_cars(*(SimCharger(f"CP_{i}", rng.choice([10.0, 50.0, 90.0]), quiet=True) for i in range(4)))
    meter = SimSiteMeter("SITE_METER", 470.0, chargers=cars, interval_s=0.05, quiet=True)
    usable = cfg.panel_limit_kw - cfg.safety_margin_kw

    async def scenario():
        cs = server.CentralSystem(fast(cfg), clock=lambda: OFF_PEAK)
        ws_server = await server.start_server(cs, "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{ws_server.sockets[0].getsockname()[1]}"
        tasks = [asyncio.create_task(server.allocation_loop(cs)), asyncio.create_task(meter.run(url))]
        tasks += [asyncio.create_task(c.run(url)) for c in cars]
        longest, streak, sampled, worst_kw, biggest_step = 0.0, 0.0, 0, 0.0, 0.0
        try:
            await asyncio.sleep(1.5)
            loop = asyncio.get_running_loop()
            for _ in range(40):
                before = meter.load_kw
                meter.load_kw = rng.uniform(455.0, 479.0)  # always leaves some EV budget
                biggest_step = max(biggest_step, meter.load_kw - before)
                end = loop.time() + 0.4
                while loop.time() < end:
                    await asyncio.sleep(0.01)
                    sampled += 1
                    over = meter.total_kw() - usable
                    worst_kw = max(worst_kw, over)
                    streak = streak + 0.01 if over > CT_RESOLUTION_KW else 0.0
                    longest = max(longest, streak)
            # A jump in the other loads overshoots by the jump itself until the CT reports it:
            # no controller can prevent that. What the controller must guarantee is that it
            # never ADDS to it, and that it never persists: a jump that coincides with one of
            # our own raises cannot be told apart from the cars ramping up until their next
            # power reading (1 s here), so it may last up to that long. The safety margin
            # must therefore be at least the largest load step (see README).
            assert sampled > 1000
            assert worst_kw <= biggest_step + CT_RESOLUTION_KW, (worst_kw, biggest_step)
            assert longest <= 1.0 + 0.25, longest
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            ws_server.close()
            await ws_server.wait_closed()
    asyncio.run(scenario())


def test_charger_hardware_rating_caps_the_draw():
    car = SimCharger("CP_1", 20.0, evse_max_kw=6.656, car_max_kw=11.5, quiet=True)
    car.charging = True
    assert car.power_w() == 6656.0                  # no profile yet: the charger's own rating
    car.limits[("TxProfile", 1)] = 9000.0
    assert car.power_w() == 6656.0
    car.limits[("TxProfile", 1)] = 3000.0
    assert car.power_w() == 3000.0


def test_simulated_building_load_cycles_hvac_and_varies():
    import time
    meter = SimSiteMeter("M", 55.0, hvac_kw=15.0, hvac_period_s=0.2, noise_pct=2.0, seed=1, quiet=True)
    seen_on, seen_off = set(), set()
    end = time.monotonic() + 0.5
    while time.monotonic() < end:
        (seen_on if meter.hvac_on() else seen_off).add(round(meter.other_kw(), 3))
        time.sleep(0.01)
    assert seen_on == {70.0} and seen_off == {55.0}             # noise is drawn per reading
    for _ in range(1000):
        meter._noise_kw = meter._rng.uniform(-1.0, 1.0) * meter.noise_pct / 100 * meter.load_kw
        assert 53.9 <= meter.other_kw() - (15.0 if meter.hvac_on() else 0.0) <= 56.1
