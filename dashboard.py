"""Live web dashboard and JSON API, served next to the OCPP websocket server.

    GET  /                       dashboard.html (polls /api/state every second)
    GET  /api/state              the whole site as JSON (see build_state)
    POST /api/demo/load          {"kw": 420}       building non-EV load (demo only)
    POST /api/demo/spike                           temporary HVAC spike (demo only)
    POST /api/demo/toggle-peak   {"active": true}  force the peak window; no body = flip,
                                                   null = follow the schedule (demo only)

The demo endpoints exist only when a demo controller is attached (demo.py, which runs
simulated chargers and a simulated CT meter). On a real site they answer 404: nothing on
the network can change what the controller believes the building draws.

A deliberately small HTTP/1.1 server on asyncio (one request per connection, bounded
sizes and timeouts), so the router needs no web framework. It is meant for a trusted
local network: there is no TLS and no login. POSTs must be application/json, which a
browser will not send cross-site without a CORS preflight this server never approves.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import gateway
import server

DASHBOARD_HTML = Path(__file__).with_name("dashboard.html")
ALERTS_KEPT = 50
MAX_BODY_BYTES = 4096
MAX_HEADER_LINES = 64
MAX_LINE_BYTES = 8192
READ_TIMEOUT_S = 10.0
TIERS = {3.0: "High", 1.5: "Medium", 0.5: "Low", 0.0: "None"}
REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
           411: "Length Required", 413: "Payload Too Large", 415: "Unsupported Media Type",
           500: "Internal Server Error", 501: "Not Implemented"}


class AlertBuffer(logging.Handler):
    """Keeps the most recent WARNING / ERROR / CRITICAL log lines for the dashboard."""

    def __init__(self, kept=ALERTS_KEPT):
        super().__init__(logging.WARNING)
        self.records = deque(maxlen=kept)

    def emit(self, record):
        try:
            self.records.append({
                "time": gateway.utc_iso(datetime.fromtimestamp(record.created, timezone.utc)),
                "level": record.levelname,
                "message": record.getMessage(),
            })
        except Exception:
            self.handleError(record)

    def recent(self):
        return list(reversed(self.records))        # newest first


def _num(value, digits=3):
    """A JSON-safe number: None for missing or non-finite values (JSON has no NaN)."""
    if value is None or isinstance(value, bool) or not math.isfinite(value):
        return None
    return round(value, digits)


def natural_key(text: str):
    """'CP_2' sorts before 'CP_10'."""
    return [(0, int(part), "") if part.isdigit() else (1, 0, part) for part in re.split(r"(\d+)", text)]


def priority_tier(session: server.Session) -> dict:
    weight = gateway.session_weight(session.soc, session.kwh_delivered)
    return {"name": TIERS.get(weight, f"{weight:g}"), "weight": weight,
            "basis": "SoC" if session.soc is not None else "kWh delivered"}


def build_state(cs: server.CentralSystem, alerts: AlertBuffer | None = None, demo=None) -> dict:
    """Everything the dashboard shows, from the controller's own state. Reads only:
    it never runs a planning cycle."""
    cfg, plan = cs.cfg, cs.last_plan
    now, wall = cs.monotonic(), cs.wall_clock()
    decision = plan.decision if plan is not None else None

    cards, ev_draw_kw, ev_limit_kw = [], 0.0, 0.0
    for charger_id, charger in sorted(cs.chargers.items(), key=lambda item: natural_key(item[0])):
        if charger_id == cfg.site_meter_id:
            continue
        connectors = sorted((cid, c) for cid, c in charger.connectors.items() if cid >= 1)
        if not connectors:
            cards.append({"id": charger_id, "charger_id": charger_id, "connector_id": None,
                          "online": charger.online,
                          "status": "Online" if charger.online else "Offline", "car": False})
            continue
        for connector_id, connector in connectors:
            session = connector.session
            card = {"id": charger_id if connector_id == 1 else f"{charger_id}/{connector_id}",
                    "charger_id": charger_id, "connector_id": connector_id,
                    "online": charger.online,
                    "status": (connector.status or "Unknown") if charger.online else "Offline",
                    "car": session is not None}
            if session is not None:
                key = (charger_id, connector_id)
                fresh = (session.power_w is not None and session.power_at is not None
                         and now - session.power_at <= cfg.telemetry_stale_after_s)
                if session.charging_stopped:
                    actual_w = 0.0              # transaction over: not drawing
                elif fresh:
                    actual_w = max(0.0, session.power_w)
                else:
                    actual_w = None             # no recent reading: unknown, not 0
                if actual_w is not None:
                    ev_draw_kw += actual_w / 1000.0
                if session.accepted_w is not None and not session.charging_stopped:
                    ev_limit_kw += session.accepted_w / 1000.0
                card.update({
                    "soc": _num(session.soc, 1),
                    "kwh_delivered": _num(session.kwh_delivered),
                    "priority": priority_tier(session),
                    "limit_w": session.accepted_w,
                    "target_w": plan.targets_w.get(key) if plan is not None else None,
                    "actual_w": _num(actual_w, 0),
                    "plugged_in_s": _num(now - session.plugged_in_mono, 0),
                    "transaction_id": session.transaction_id,
                    "charging_stopped": session.charging_stopped,
                    "tapered": plan is not None and key in plan.tapered,
                    "uncontrollable": session.uncontrollable,
                    "fallback_profile": session.used_fallback,
                    "peak_shifted_kwh": _num(session.peak_shift_kwh),
                    "savings_usd": cs.savings.energy_usd(session.peak_shift_kwh),
                })
            cards.append(card)

    age = None if cs.last_meter_timestamp is None else now - cs.last_meter_timestamp
    usable_kw = cfg.panel_limit_kw - cfg.safety_margin_kw
    return {
        "time": gateway.utc_iso(wall),
        "site": {
            "panel_limit_kw": cfg.panel_limit_kw,
            "safety_margin_kw": cfg.safety_margin_kw,
            "usable_kw": _num(usable_kw),
            "continuous_capacity_kw": _num(cfg.continuous_capacity_kw),
            "max_charger_kw": cfg.max_charger_kw,
            "site_load_kw": _num(cs.site_load_kw),
            "other_load_kw": _num(plan.other_load_kw) if plan is not None else None,
            "ev_draw_kw": _num(ev_draw_kw),
            "ev_limit_kw": _num(ev_limit_kw),
            "budget_kw": _num(decision.budget_kw) if decision else None,
            "reserved_kw": _num(plan.reserved_w / 1000.0) if plan is not None else None,
            "cap_source": decision.cap_source if decision else None,
            "cap_kw": _num(decision.cap_kw) if decision else None,
            "over_panel": cs.site_load_kw is not None and cs.site_load_kw > cfg.panel_limit_kw,
        },
        "telemetry": {
            "state": cs.telemetry_state,
            "stale": decision.telemetry_stale if decision else True,
            "age_s": _num(age, 1),
            "stale_after_s": cfg.telemetry_stale_after_s,
        },
        "peak": {
            "active": cs.peak_active(wall),
            "override": cs.peak_override,
            "windows": [f"{w.start_minute // 60:02d}:{w.start_minute % 60:02d}-"
                        f"{w.end_minute // 60:02d}:{w.end_minute % 60:02d}" for w in cfg.peak_windows],
            "budget_cap_kw": cfg.peak_budget_cap_kw,
            "timezone": cfg.timezone,
        },
        "savings": cs.savings.snapshot(),
        "chargers": cards,
        "alerts": alerts.recent() if alerts is not None else [],
        "demo": demo.state() if demo is not None else {"enabled": False},
    }


class Dashboard:
    """Routes requests. `respond` is pure (request in, response out), so it is tested
    without sockets; `handle` is the asyncio connection callback around it."""

    def __init__(self, cs: server.CentralSystem, *, demo=None, alerts: AlertBuffer | None = None,
                 html_path: Path = DASHBOARD_HTML):
        self.cs, self.demo, self.alerts = cs, demo, alerts
        self.html = Path(html_path).read_bytes()

    def respond(self, method: str, path: str, headers: dict, body: bytes):
        """-> (status, content type, body bytes, extra headers)"""
        if path in ("/", "/index.html"):
            if method != "GET":
                return _error(405, "use GET", allow="GET")
            return 200, "text/html; charset=utf-8", self.html, {}
        if path == "/api/state":
            if method != "GET":
                return _error(405, "use GET", allow="GET")
            return _json(200, build_state(self.cs, self.alerts, self.demo))
        if path in ("/api/demo/load", "/api/demo/spike", "/api/demo/toggle-peak"):
            if self.demo is None:
                return _error(404, "demo controls are disabled on a live site (run demo.py)")
            if method != "POST":
                return _error(405, "use POST", allow="POST")
            if headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                return _error(415, "send Content-Type: application/json")
            try:
                data = json.loads(body) if body.strip() else {}
            except (ValueError, UnicodeDecodeError):
                return _error(400, "the body is not valid JSON")
            if not isinstance(data, dict):
                return _error(400, "the body must be a JSON object")
            try:
                if path == "/api/demo/load":
                    if "kw" not in data:
                        return _error(400, "kw is required")
                    self.demo.set_base_load(data["kw"])
                elif path == "/api/demo/spike":
                    self.demo.spike()
                else:
                    if "active" in data:
                        self.demo.set_peak(data["active"])
                    else:
                        self.demo.toggle_peak()
            except ValueError as error:
                return _error(400, str(error))
            return _json(200, {"ok": True, "demo": self.demo.state()})
        return _error(404, "not found")

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            try:
                request = await asyncio.wait_for(_read_request(reader), READ_TIMEOUT_S)
            except _BadRequest as error:
                response = _error(error.status, error.message)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
                return
            else:
                try:
                    response = self.respond(*request)
                except Exception:
                    server.log.exception("dashboard request failed")
                    response = _error(500, "internal error")
            writer.write(_encode(*response))
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


class _BadRequest(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


async def _readline(reader):
    try:
        line = await reader.readuntil(b"\n")
    except asyncio.LimitOverrunError:
        raise _BadRequest(400, "line too long")
    if len(line) > MAX_LINE_BYTES:
        raise _BadRequest(400, "line too long")
    return line.decode("latin-1").rstrip("\r\n")


async def _read_request(reader):
    parts = (await _readline(reader)).split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise _BadRequest(400, "malformed request line")
    method, target = parts[0], parts[1]
    headers = {}
    for _ in range(MAX_HEADER_LINES + 1):
        line = await _readline(reader)
        if not line:
            break
        name, colon, value = line.partition(":")
        if not colon or not name.strip():
            raise _BadRequest(400, "malformed header")
        headers[name.strip().lower()] = value.strip()
    else:
        raise _BadRequest(400, "too many headers")
    if "transfer-encoding" in headers:
        raise _BadRequest(501, "chunked bodies are not supported; send Content-Length")
    length_text = headers.get("content-length", "0")
    if not length_text.isdigit():
        raise _BadRequest(400, "invalid Content-Length")
    length = int(length_text)
    if length > MAX_BODY_BYTES:
        raise _BadRequest(413, f"body larger than {MAX_BODY_BYTES} bytes")
    body = await reader.readexactly(length) if length else b""
    return method, urlsplit(target).path, headers, body


def _json(status, data):
    return status, "application/json", json.dumps(data, allow_nan=False).encode(), {}


def _error(status, message, allow=None):
    status, kind, body, extra = _json(status, {"error": message})
    if allow:
        extra = {"Allow": allow}
    return status, kind, body, extra


def _encode(status, content_type, body, extra):
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Connection": "close",
        **extra,
    }
    head = f"HTTP/1.1 {status} {REASONS.get(status, 'Error')}\r\n"
    head += "".join(f"{name}: {value}\r\n" for name, value in headers.items())
    return head.encode("latin-1") + b"\r\n" + body


class DashboardServer:
    """A running dashboard: the asyncio server plus the alert handler it installed."""

    def __init__(self, tcp_server, app: Dashboard, alerts: AlertBuffer):
        self.tcp_server, self.app, self.alerts = tcp_server, app, alerts

    @property
    def port(self) -> int:
        return self.tcp_server.sockets[0].getsockname()[1]

    async def close(self):
        server.log.removeHandler(self.alerts)
        self.tcp_server.close()
        await self.tcp_server.wait_closed()


async def start_dashboard(cs: server.CentralSystem, host: str, port: int, *, demo=None) -> DashboardServer:
    alerts = AlertBuffer()
    app = Dashboard(cs, demo=demo, alerts=alerts)      # reads dashboard.html: fails early if missing
    server.log.addHandler(alerts)
    try:
        tcp_server = await asyncio.start_server(app.handle, host, port, limit=MAX_LINE_BYTES)
    except BaseException:
        server.log.removeHandler(alerts)
        raise
    return DashboardServer(tcp_server, app, alerts)
