"""The dashboard's JSON API and HTTP server, and the demo controls. Offline: the HTTP
tests use a real socket on 127.0.0.1 only."""
import asyncio
import json
import logging

import pytest

import dashboard
import server
from conftest import PEAK
from demo import DemoController
from sim_chargers import SimSiteMeter
from test_server import Clock, FakeCharger, cycle, make, measure, plug, power, run, session


def strict_json(data):
    """What the browser parses: JSON without NaN or Infinity."""
    return json.loads(json.dumps(data, allow_nan=False))


def site_with_cars(cfg, wall=PEAK):
    cs, clock = make(cfg, Clock(wall))
    for cid, soc in (("CP_10", 15.0), ("CP_2", 50.0), ("CP_1", None)):
        FakeCharger(cs, cid)
        plug(cs, cid, soc=soc)
    measure(cs, 100.0)
    run(cycle(cs))
    measure(cs, 100.0)                      # the cars now report drawing their accepted limits
    return cs, clock


def demo_for(cs, load_kw=420.0):
    meter = SimSiteMeter("SITE_METER", load_kw, quiet=True)
    return DemoController(cs, meter), meter


# ---------------------------------------------------------------- build_state

def test_state_before_any_cycle(cfg):
    cs, _ = make(cfg)
    state = strict_json(dashboard.build_state(cs))
    assert state["chargers"] == [] and state["alerts"] == []
    assert state["site"]["budget_kw"] is None and state["site"]["site_load_kw"] is None
    assert state["telemetry"]["stale"] is True
    assert state["demo"] == {"enabled": False}
    assert state["site"]["panel_limit_kw"] == 600.0 and state["site"]["safety_margin_kw"] == 120.0
    assert state["savings"]["enabled"] is True


def test_state_with_cars(cfg):
    cs, clock = site_with_cars(cfg)
    state = strict_json(dashboard.build_state(cs))
    cards = state["chargers"]
    assert [c["id"] for c in cards] == ["CP_1", "CP_2", "CP_10"]        # natural order
    by_id = {c["id"]: c for c in cards}
    cp10, cp2, cp1 = by_id["CP_10"], by_id["CP_2"], by_id["CP_1"]
    assert cp10["soc"] == 15.0 and cp10["priority"] == {"name": "High", "weight": 3.0, "basis": "SoC"}
    assert cp2["priority"]["name"] == "Medium"
    assert cp1["soc"] is None and cp1["priority"]["basis"] == "kWh delivered"
    for card in cards:
        assert card["online"] and card["car"] and card["status"] == "Preparing"
        assert card["limit_w"] == session(cs, card["charger_id"]).accepted_w
        assert card["actual_w"] == card["limit_w"]                     # measure() reported them
    assert sum(c["limit_w"] for c in cards) <= 5000                     # peak cap
    assert state["peak"]["active"] is True and state["site"]["cap_source"] == "peak"
    assert state["peak"]["windows"] == ["16:00-21:00"]
    assert state["telemetry"]["state"] == "ok" and state["telemetry"]["stale"] is False


def test_state_actual_draw_unknown_without_a_fresh_reading(cfg):
    cs, clock = site_with_cars(cfg)
    clock.advance(cfg.telemetry_stale_after_s + 1)
    cards = dashboard.build_state(cs)["chargers"]
    assert all(card["actual_w"] is None for card in cards)              # unknown, not 0


def test_state_offline_charger_and_charger_without_car(cfg):
    cs, _ = make(cfg)
    FakeCharger(cs, "CP_EMPTY")
    lost = FakeCharger(cs, "CP_LOST")
    plug(cs, "CP_LOST", soc=40.0)
    cs.detach("CP_LOST", lost.send)
    cards = {c["id"]: c for c in strict_json(dashboard.build_state(cs))["chargers"]}
    assert cards["CP_EMPTY"]["car"] is False and cards["CP_EMPTY"]["status"] == "Online"
    assert cards["CP_LOST"]["status"] == "Offline" and cards["CP_LOST"]["online"] is False
    assert "SITE_METER" not in cards


def test_state_never_contains_nan(cfg):
    cs, clock = site_with_cars(cfg)
    power(cs, "CP_2", float("nan"))        # ignored by the parser, but be sure
    cs.chargers["CP_2"].connectors[1].session.soc = float("nan")
    strict_json(dashboard.build_state(cs))


def test_alert_buffer_keeps_warnings_newest_first():
    buffer = dashboard.AlertBuffer(kept=3)
    logger = logging.getLogger("test_alert_buffer")
    logger.addHandler(buffer)
    logger.propagate = False
    try:
        logger.info("not an alert")
        for i in range(5):
            logger.warning("w%d", i)
        logger.critical("crit")
    finally:
        logger.removeHandler(buffer)
    recent = buffer.recent()
    assert [a["message"] for a in recent] == ["crit", "w4", "w3"]
    assert recent[0]["level"] == "CRITICAL" and recent[0]["time"].endswith("Z")


# ---------------------------------------------------------------- routing (no sockets)

def respond(app, method, path, body=b"", content_type="application/json"):
    headers = {"content-type": content_type} if content_type else {}
    status, kind, payload, extra = app.respond(method, path, headers, body)
    return status, kind, payload, extra


def test_routes_without_demo(cfg):
    cs, _ = make(cfg)
    app = dashboard.Dashboard(cs)
    status, kind, body, _ = respond(app, "GET", "/")
    assert status == 200 and kind.startswith("text/html") and b"/api/state" in body
    status, kind, body, _ = respond(app, "GET", "/api/state")
    assert status == 200 and kind == "application/json" and json.loads(body)["demo"]["enabled"] is False
    assert respond(app, "POST", "/api/state")[0] == 405
    assert respond(app, "GET", "/nope")[0] == 404
    for path in ("/api/demo/load", "/api/demo/spike", "/api/demo/toggle-peak"):
        status, _, body, _ = respond(app, "POST", path, b'{"kw": 1}')
        assert status == 404 and b"disabled" in body       # no way to fake the load on a live site


def test_demo_load(cfg):
    cs, _ = make(cfg)
    demo, meter = demo_for(cs)
    app = dashboard.Dashboard(cs, demo=demo)
    status, _, body, _ = respond(app, "POST", "/api/demo/load", b'{"kw": 470}')
    assert status == 200 and meter.load_kw == 470.0
    assert json.loads(body)["demo"]["base_load_kw"] == 470.0
    assert respond(app, "POST", "/api/demo/load", b'{"kw": 0}')[0] == 200 and meter.load_kw == 0.0


@pytest.mark.parametrize("body", [b'{"kw": 551}', b'{"kw": -1}', b'{"kw": "400"}', b'{"kw": true}',
                                  b'{"kw": NaN}', b'{"kw": Infinity}', b'{"kw": null}', b'{}'])
def test_demo_load_rejects_bad_values(cfg, body):
    cs, _ = make(cfg)
    demo, meter = demo_for(cs)
    app = dashboard.Dashboard(cs, demo=demo)
    assert respond(app, "POST", "/api/demo/load", body)[0] == 400
    assert meter.load_kw == 420.0                          # unchanged


@pytest.mark.parametrize("body, content_type, status", [
    (b'{"kw": 400}', "text/plain", 415),                   # a cross-site form cannot set JSON
    (b'{"kw": 400}', None, 415),
    (b'{"kw": 400}', "application/json; charset=utf-8", 200),
    (b'not json', "application/json", 400),
    (b'[400]', "application/json", 400),
    (b'\xff\xfe', "application/json", 400),
])
def test_demo_request_format(cfg, body, content_type, status):
    cs, _ = make(cfg)
    demo, _ = demo_for(cs)
    app = dashboard.Dashboard(cs, demo=demo)
    assert respond(app, "POST", "/api/demo/load", body, content_type)[0] == status


def test_demo_endpoints_need_post(cfg):
    cs, _ = make(cfg)
    demo, _ = demo_for(cs)
    status, _, _, extra = respond(dashboard.Dashboard(cs, demo=demo), "GET", "/api/demo/spike")
    assert status == 405 and extra == {"Allow": "POST"}


def test_demo_spike(cfg):
    cs, _ = make(cfg)
    demo, meter = demo_for(cs)
    app = dashboard.Dashboard(cs, demo=demo)
    before = meter.other_kw()
    assert respond(app, "POST", "/api/demo/spike", b"")[0] == 200     # an empty body is fine
    assert meter.spike_remaining_s() > 29.0
    assert meter.other_kw() == pytest.approx(before + 150.0)
    assert demo.state()["spike_remaining_s"] > 29.0


def test_demo_toggle_peak(cfg):
    cs, _ = make(cfg)                                       # OFF_PEAK clock
    demo, _ = demo_for(cs)
    app = dashboard.Dashboard(cs, demo=demo)
    respond(app, "POST", "/api/demo/toggle-peak")
    assert cs.peak_override is True and cs.peak_active(cs.wall_clock())
    respond(app, "POST", "/api/demo/toggle-peak")
    assert cs.peak_override is False
    respond(app, "POST", "/api/demo/toggle-peak", b'{"active": true}')
    assert cs.peak_override is True
    respond(app, "POST", "/api/demo/toggle-peak", b'{"active": null}')
    assert cs.peak_override is None                          # back to the schedule
    assert respond(app, "POST", "/api/demo/toggle-peak", b'{"active": "on"}')[0] == 400


def test_toggle_during_real_peak_turns_it_off(cfg):
    cs, _ = make(cfg, Clock(PEAK))
    demo, _ = demo_for(cs)
    demo.toggle_peak()
    assert cs.peak_override is False


def test_spike_raises_the_simulated_ct_reading():
    meter = SimSiteMeter("M", 100.0, quiet=True)
    meter.start_spike(150.0, 30.0)
    assert meter.total_kw() == pytest.approx(250.0)
    meter.start_spike(150.0, 0.0)                           # expired
    assert meter.spike_remaining_s() == 0.0 and meter.total_kw() == pytest.approx(100.0)


# ---------------------------------------------------------------- real HTTP over loopback

async def http(port, raw: bytes):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
    return status, headers, body


def with_server(cfg, test, demo=False):
    async def main():
        cs, _ = make(cfg)
        controller = demo_for(cs)[0] if demo else None
        web = await dashboard.start_dashboard(cs, "127.0.0.1", 0, demo=controller)
        try:
            return await test(cs, web)
        finally:
            await web.close()
    return run(main())


def test_http_get_state(cfg):
    async def test(cs, web):
        status, headers, body = await http(web.port, b"GET /api/state HTTP/1.1\r\nHost: x\r\n\r\n")
        assert status == 200
        assert headers["content-type"] == "application/json"
        assert headers["cache-control"] == "no-store" and headers["connection"] == "close"
        assert headers["x-content-type-options"] == "nosniff"
        assert int(headers["content-length"]) == len(body)
        assert "site" in json.loads(body)
    with_server(cfg, test)


def test_http_query_string_is_ignored(cfg):
    async def test(cs, web):
        status, _, body = await http(web.port, b"GET /api/state?t=1 HTTP/1.1\r\n\r\n")
        assert status == 200 and json.loads(body)
    with_server(cfg, test)


def test_http_dashboard_page(cfg):
    async def test(cs, web):
        status, headers, body = await http(web.port, b"GET / HTTP/1.1\r\n\r\n")
        assert status == 200 and headers["content-type"].startswith("text/html")
        assert headers["x-frame-options"] == "DENY"
        assert b"cdn.tailwindcss.com" in body
    with_server(cfg, test)


def test_http_demo_post(cfg):
    async def test(cs, web):
        body = b'{"kw": 300}'
        status, _, reply = await http(web.port, b"POST /api/demo/load HTTP/1.1\r\nContent-Type: application/json\r\n"
                                      b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
        assert status == 200 and json.loads(reply)["demo"]["base_load_kw"] == 300.0
    with_server(cfg, test, demo=True)


@pytest.mark.parametrize("raw, status", [
    (b"GARBAGE\r\n\r\n", 400),
    (b"GET / FTP/1.0\r\n\r\n", 400),
    (b"GET / HTTP/1.1\r\nno-colon-header\r\n\r\n", 400),
    (b"POST /api/demo/load HTTP/1.1\r\nContent-Length: abc\r\n\r\n", 400),
    (b"POST /api/demo/load HTTP/1.1\r\nContent-Length: -5\r\n\r\n", 400),
    (b"POST /api/demo/load HTTP/1.1\r\nContent-Length: 999999\r\n\r\n", 413),
    (b"POST /api/demo/load HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n", 501),
    (b"GET /" + b"a" * 20000 + b" HTTP/1.1\r\n\r\n", 400),
    (b"GET / HTTP/1.1\r\n" + b"X-A: b\r\n" * 100 + b"\r\n", 400),
])
def test_http_bad_requests(cfg, raw, status):
    async def test(cs, web):
        got, _, body = await http(web.port, raw)
        assert got == status and "error" in json.loads(body)
    with_server(cfg, test, demo=True)


def test_http_client_that_sends_nothing_is_dropped(cfg, monkeypatch):
    monkeypatch.setattr(dashboard, "READ_TIMEOUT_S", 0.2)

    async def test(cs, web):
        reader, writer = await asyncio.open_connection("127.0.0.1", web.port)
        data = await asyncio.wait_for(reader.read(), 3)     # closed without a reply
        writer.close()
        assert data == b""
        status, _, _ = await http(web.port, b"GET /api/state HTTP/1.1\r\n\r\n")   # still serving
        assert status == 200
    with_server(cfg, test)


def test_http_alerts_flow_into_state_and_handler_is_removed(cfg):
    async def test(cs, web):
        server.log.critical("CRITICAL ALERT: test alert")
        _, _, body = await http(web.port, b"GET /api/state HTTP/1.1\r\n\r\n")
        assert json.loads(body)["alerts"][0]["message"] == "CRITICAL ALERT: test alert"
        return web.alerts
    alerts = with_server(cfg, test)
    assert alerts not in server.log.handlers


def test_http_internal_error_is_a_500_not_a_crash(cfg, monkeypatch):
    async def test(cs, web):
        monkeypatch.setattr(dashboard, "build_state", lambda *a: 1 / 0)
        status, _, body = await http(web.port, b"GET /api/state HTTP/1.1\r\n\r\n")
        assert status == 500 and json.loads(body) == {"error": "internal error"}
        monkeypatch.undo()
        assert (await http(web.port, b"GET /api/state HTTP/1.1\r\n\r\n"))[0] == 200
    with_server(cfg, test)
