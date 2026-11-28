"""OCPP 1.6J central system for one site.

Tracks every charger, connector and charging session; every allocation cycle it decides
each car's power with gateway.py and pushes it as a SetChargingProfile.

CentralSystem holds all of the logic and never touches a socket: it is given a `send`
coroutine per charger and fed raw frames through on_message(), so pytest can drive it
completely offline. The websocket glue lives at the bottom of the file.
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import gateway
from config import DEFAULT_CONFIG_PATH, ConfigError, SiteConfig, load_site_config
from ocpp_meter import MeterReading, parse_data_transfer, parse_meter_values, to_number

log = logging.getLogger("edge_ocpp_router")

CALL, CALLRESULT, CALLERROR = 2, 3, 4
HEARTBEAT_INTERVAL_S = 10
PLUGGED_IN_STATUSES = {"Preparing", "Charging", "SuspendedEV", "SuspendedEVSE"}
UNPLUGGED_STATUSES = {"Available", "Unavailable"}
NOT_DRAWING_STATUSES = {"SuspendedEV", "SuspendedEVSE"}
METER_MEASURANDS = ["Energy.Active.Import.Register", "Power.Active.Import", "Current.Import", "SoC"]
COMPLETED_SESSIONS_KEPT = 1000
HISTORY_KEPT = 64                           # per car: recent power readings / accepted limits
J1772_RESPONSE_S = 5.0                      # SAE J1772 gives an EV 5 s to follow a new pilot
OVERDRAW_SETTLE_S = 10.0                    # report overdraw only after twice that


class OcppError(Exception):
    """Answer the incoming CALL with this CALLERROR."""

    def __init__(self, code: str, description: str = ""):
        super().__init__(f"{code}: {description}")
        self.code, self.description = code, description


class CallFailed(Exception):
    """The charger answered our CALL with a CALLERROR."""

    def __init__(self, code: str, description: str = ""):
        super().__init__(f"{code}: {description}")
        self.code = code


class NoResponse(Exception):
    """The charger did not answer in time, or disconnected first."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_duration(seconds: float) -> str:
    return str(timedelta(seconds=int(max(0.0, seconds))))


@dataclass
class Session:
    """One car, from plug-in to unplug."""
    session_id: str
    charger_id: str
    connector_id: int
    plugged_in_at: datetime
    plugged_in_mono: float
    transaction_id: int | None = None
    id_tag: str | None = None
    transaction_started_at: datetime | None = None
    transaction_stopped_at: datetime | None = None
    stop_reason: str | None = None
    charging_stopped: bool = False          # StopTransaction received: no longer drawing
    energy_start_wh: float | None = None
    energy_last_wh: float | None = None
    delivered_wh: float = 0.0
    start_soc: float | None = None
    soc: float | None = None
    power_w: float | None = None
    power_at: float | None = None           # monotonic time of the last power reading
    power_history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_KEPT))   # (t, W)
    limit_history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_KEPT))   # (t, W) accepted
    current_a: float | None = None
    accepted_w: int | None = None           # limit the charger last accepted (None: unknown)
    inflight_w: int | None = None           # limit sent, answer not in yet
    uncertain_w: int | None = None          # sent but never answered: may be in force
    limit_active_since: float | None = None  # when the accepted limit last rose from 0 to >= min
    sent_purpose: str | None = None
    sent_w: int | None = None
    sent_at: float | None = None
    uncontrollable: bool = False            # rejected every limit: power reserved, alert raised
    overdraw_reported: bool = False         # drew over its accepted limit (logged once)
    under_default: bool = False             # began under the accepted 0 W site default
    used_fallback: bool = False             # limited through ChargePointMaxProfile
    peak_shift_kwh: float = 0.0             # energy moved out of peak windows (savings estimate)

    @property
    def label(self) -> str:
        return self.charger_id if self.connector_id == 1 else f"{self.charger_id}/{self.connector_id}"

    @property
    def kwh_delivered(self) -> float:
        return self.delivered_wh / 1000.0

    def record_energy(self, register_wh: float):
        if self.energy_start_wh is None:
            self.energy_start_wh = self.energy_last_wh = register_wh
            return
        if self.energy_last_wh is not None and register_wh < self.energy_last_wh:
            # The register went backwards: meter replaced or reset. Keep what was already
            # delivered and count on from the new value.
            log.warning("%s energy register went backwards (%.0f -> %.0f Wh); rebasing",
                        self.label, self.energy_last_wh, register_wh)
            self.energy_start_wh = register_wh - self.delivered_wh
        self.energy_last_wh = register_wh
        self.delivered_wh = max(0.0, register_wh - self.energy_start_wh)


@dataclass
class Connector:
    status: str | None = None
    error_code: str | None = None
    session: Session | None = None


@dataclass
class Charger:
    charger_id: str
    send: object = None                     # async callable(str), None while offline
    online: bool = False
    offline_since: float | None = None
    connectors: dict = field(default_factory=dict)      # connector_id -> Connector
    use_max_profile: bool = False           # TxProfile was rejected; limiting via ChargePointMax
    max_profile_w: int | None = None
    needs_meter_config: bool = False        # push MeterValueSampleInterval/SampledData after boot
    needs_default_profile: bool = False     # install the 0 W site default for new sessions
    default_accepted_at: float | None = None  # when the charger accepted that default
    max_profile_at: float | None = None
    busy: bool = False                      # a command exchange is still running
    pending: dict = field(default_factory=dict)         # message id -> Future

    def connector(self, connector_id: int) -> Connector:
        return self.connectors.setdefault(connector_id, Connector())

    def sessions(self):
        return [c.session for c in self.connectors.values() if c.session is not None]


@dataclass
class Plan:
    decision: gateway.BudgetDecision
    site_load_kw: float | None              # CT reading: the whole site, EVs included
    other_load_kw: float | None             # everything that is not an EV
    reserved_w: int                         # held for offline / uncontrollable cars
    targets_w: dict                         # (charger_id, connector_id) -> W the car should get
    send_w: dict                            # (charger_id, connector_id) -> W safe to send now
    sessions: dict                          # (charger_id, connector_id) -> Session
    tapered: set
    in_peak: bool = False                   # a tariff peak window is in effect


class CentralSystem:
    def __init__(self, cfg: SiteConfig, *, grid_signal_cap_kw: float | None = None,
                 monotonic=time.monotonic, clock=utc_now):
        self.cfg = cfg
        self.chargers: dict[str, Charger] = {}
        self.site_load_kw: float | None = None             # main-feeder CT: EVs included
        self.last_meter_timestamp: float | None = None     # monotonic time of last CT reading
        self._wake: asyncio.Event | None = None
        self._over_panel = False
        self._other_load_prev: float | None = None     # last other-loads estimate (kW)
        self._ct_negative = False
        self.grid_signal_cap_kw: float | None = None
        self.grid_signal_at: float | None = None
        self.completed_sessions = deque(maxlen=COMPLETED_SESSIONS_KEPT)
        self._monotonic, self._clock = monotonic, clock
        # OCPP transaction ids are integers. Starting from the Unix time keeps them unique
        # across restarts (and below 2**31, which many chargers store, until 2038).
        self._transaction_ids = itertools.count(int(time.time()))
        self._tasks: set[asyncio.Task] = set()
        self._telemetry_state: str | None = None   # "waiting", "missing", "stale" or "ok"
        self._started = monotonic()
        self._last_cap_source: str | None = None
        self.peak_override: bool | None = None     # demo: force the peak window on/off
        self.last_plan: Plan | None = None
        self.savings = gateway.SavingsTracker(cfg, max_gap_s=max(10.0, 3 * cfg.allocation_interval_s))
        self._handlers = {
            "BootNotification": self._on_boot,
            "Heartbeat": self._on_heartbeat,
            "StatusNotification": self._on_status,
            "Authorize": self._on_authorize,
            "StartTransaction": self._on_start_transaction,
            "StopTransaction": self._on_stop_transaction,
            "MeterValues": self._on_meter_values,
            "DataTransfer": self._on_data_transfer,
            "FirmwareStatusNotification": lambda cid, payload: {},
            "DiagnosticsStatusNotification": lambda cid, payload: {},
        }
        if grid_signal_cap_kw is not None:
            self.set_grid_signal(grid_signal_cap_kw)

    # ---------------------------------------------------------------- connections

    def attach(self, charger_id: str, send) -> Charger:
        """A charger connected. A second connection with the same id replaces the first."""
        charger = self.chargers.get(charger_id)
        if charger is None:
            charger = self.chargers[charger_id] = Charger(charger_id)
        elif charger.online:
            log.warning("%s connected again; replacing the previous connection", charger_id)
        self._fail_pending(charger, "connection replaced")
        charger.send, charger.online, charger.offline_since = send, True, None
        self._forget_sent_limits(charger)       # re-send everything on the new connection
        if charger_id != self.cfg.site_meter_id:
            # Its stored default may or may not have survived: count it unknown until it is
            # accepted again, and install it right away.
            charger.needs_default_profile, charger.default_accepted_at = True, None
            self.wake_now()
        log.info("%s connected", charger_id)
        return charger

    def detach(self, charger_id: str, send):
        charger = self.chargers.get(charger_id)
        # == not `is`: two lookups of the same bound method are equal but not identical.
        if charger is None or charger.send != send:
            return                                  # an older, already replaced connection
        charger.send, charger.online = None, False
        charger.offline_since = self._monotonic()
        self._fail_pending(charger, "charger disconnected")
        if charger_id == self.cfg.site_meter_id:
            log.warning("site meter disconnected; EV budget drops to the %.1f kW floor once "
                        "its last reading is %.0f s old", self.cfg.stale_floor_kw,
                        self.cfg.telemetry_stale_after_s)
        elif charger.sessions():
            log.warning("%s disconnected with a car plugged in; its last limit stays reserved "
                        "for up to %.0f s", charger_id, self.cfg.offline_session_timeout_s)
        else:
            log.info("%s disconnected", charger_id)

    def _fail_pending(self, charger: Charger, why: str):
        for future in charger.pending.values():
            if not future.done():
                future.set_exception(NoResponse(why))
        charger.pending.clear()

    def _forget_sent_limits(self, charger: Charger):
        charger.max_profile_w = charger.max_profile_at = None
        for session in charger.sessions():
            session.sent_purpose = session.sent_w = session.sent_at = None

    # ---------------------------------------------------------------- incoming frames

    def on_message(self, charger_id: str, raw) -> str | None:
        """Handle one frame from a charger. Returns the reply frame to send, if any."""
        try:
            message = json.loads(raw)
        except (ValueError, TypeError):
            log.warning("%s sent a frame that is not JSON; ignored", charger_id)
            return None
        if not isinstance(message, list) or not message or message[0] not in (CALL, CALLRESULT, CALLERROR) \
                or isinstance(message[0], bool):
            log.warning("%s sent a frame that is not an OCPP message; ignored", charger_id)
            return None

        if message[0] == CALL:
            message_id = message[1] if len(message) > 1 and isinstance(message[1], str) else None
            if message_id is None:
                log.warning("%s sent a CALL without a message id; ignored", charger_id)
                return None
            if len(message) != 4 or not isinstance(message[2], str) or not isinstance(message[3], dict):
                return json.dumps([CALLERROR, message_id, "FormationViolation",
                                   "expected [2, id, action, {payload}]", {}])
            action, payload = message[2], message[3]
            try:
                result = self.handle_call(charger_id, action, payload)
            except OcppError as error:
                return json.dumps([CALLERROR, message_id, error.code, error.description, {}])
            except Exception:
                log.exception("%s: error while handling %s", charger_id, action)
                return json.dumps([CALLERROR, message_id, "InternalError", "", {}])
            return json.dumps([CALLRESULT, message_id, result])

        # A reply to one of our CALLs.
        charger = self.chargers.get(charger_id)
        message_id = message[1] if len(message) > 1 else None
        future = charger.pending.pop(message_id, None) if charger and isinstance(message_id, str) else None
        if future is None or future.done():
            log.debug("%s replied to an unknown or expired message %r", charger_id, message_id)
            return None
        if message[0] == CALLRESULT and len(message) == 3:
            future.set_result(message[2])
        elif message[0] == CALLERROR and len(message) >= 4:
            future.set_exception(CallFailed(str(message[2]), str(message[3])))
        else:
            future.set_exception(CallFailed("FormationViolation", "malformed reply"))
        return None

    def handle_call(self, charger_id: str, action: str, payload: dict) -> dict:
        handler = self._handlers.get(action)
        if handler is None:
            raise OcppError("NotImplemented", f"{action} is not supported")
        if charger_id == self.cfg.site_meter_id and action not in (
                "BootNotification", "Heartbeat", "StatusNotification", "MeterValues"):
            raise OcppError("NotSupported", f"the site meter does not send {action}")
        if charger_id not in self.chargers:             # frames fed without attach() (tests)
            self.chargers[charger_id] = Charger(charger_id)
        return handler(charger_id, payload)

    # ---------------------------------------------------------------- OCPP handlers

    def _on_boot(self, charger_id, payload):
        charger = self.chargers[charger_id]
        # A rebooted charger may have lost its profiles: start over with TxProfile and
        # re-send every limit.
        charger.use_max_profile = False
        charger.needs_meter_config = charger_id != self.cfg.site_meter_id
        if charger_id != self.cfg.site_meter_id:
            charger.needs_default_profile, charger.default_accepted_at = True, None
            self.wake_now()
        self._forget_sent_limits(charger)
        log.info("%s booted (%s %s)", charger_id, payload.get("chargePointVendor", "?"),
                 payload.get("chargePointModel", "?"))
        return {"status": "Accepted", "currentTime": gateway.utc_iso(self._clock()),
                "interval": HEARTBEAT_INTERVAL_S}

    def _on_heartbeat(self, charger_id, payload):
        return {"currentTime": gateway.utc_iso(self._clock())}

    def _on_authorize(self, charger_id, payload):
        _require(payload, "idTag", str)
        # Driver authorization is outside this router's scope: every tag is accepted.
        return {"idTagInfo": {"status": "Accepted"}}

    def _on_status(self, charger_id, payload):
        connector_id = _require(payload, "connectorId", int)
        status = _require(payload, "status", str)
        error_code = payload.get("errorCode")
        if connector_id < 0:
            raise OcppError("PropertyConstraintViolation", "connectorId must be >= 0")
        if charger_id == self.cfg.site_meter_id:
            return {}
        if connector_id == 0:                       # the charger as a whole
            if status == "Faulted":
                log.error("%s reports a charger-level fault (%s)", charger_id, error_code)
            return {}

        charger = self.chargers[charger_id]
        connector = charger.connector(connector_id)
        previous, connector.status, connector.error_code = connector.status, status, error_code
        if status == "Faulted":
            log.error("%s connector %d faulted (%s); no power is allocated to it",
                      charger_id, connector_id, error_code)
        elif previous == "Faulted":
            log.info("%s connector %d recovered (%s)", charger_id, connector_id, status)
        if status in PLUGGED_IN_STATUSES and connector.session is None:
            self._open_session(charger, connector_id, f"status {status}")
        elif status in UNPLUGGED_STATUSES and connector.session is not None:
            self._close_session(charger, connector_id, "Unplugged" if status == "Available" else status)
        return {}

    def _on_start_transaction(self, charger_id, payload):
        connector_id = _require(payload, "connectorId", int)
        id_tag = _require(payload, "idTag", str)
        meter_start = _require_number(payload, "meterStart")
        _require(payload, "timestamp", str)
        if connector_id < 1:
            raise OcppError("PropertyConstraintViolation", "connectorId must be >= 1")

        charger = self.chargers[charger_id]
        connector = charger.connector(connector_id)
        session = connector.session
        if session is not None and session.transaction_id is not None:
            # The previous transaction never got a StopTransaction (or this is a second
            # transaction on the same plug-in): close it out and start a fresh session.
            self._close_session(charger, connector_id, "NewTransaction")
            session = None
        if session is None:
            session = self._open_session(charger, connector_id, "StartTransaction")

        transaction_id = next(self._transaction_ids)
        session.transaction_id, session.id_tag = transaction_id, id_tag
        session.transaction_started_at = self._clock()
        session.charging_stopped = False
        if session.energy_start_wh is None:
            session.record_energy(meter_start)
        session.sent_purpose = session.sent_w = session.sent_at = None   # now send a TxProfile
        log.info("%s transaction %d started (idTag %s, meter %.0f Wh)",
                 session.label, transaction_id, id_tag, meter_start)
        return {"idTagInfo": {"status": "Accepted"}, "transactionId": transaction_id}

    def _on_stop_transaction(self, charger_id, payload):
        transaction_id = _require(payload, "transactionId", int)
        meter_stop = _require_number(payload, "meterStop")
        _require(payload, "timestamp", str)
        session = self._find_transaction(self.chargers[charger_id], transaction_id)
        if session is None:
            log.warning("%s stopped unknown transaction %d", charger_id, transaction_id)
            return {"idTagInfo": {"status": "Accepted"}}

        if isinstance(payload.get("transactionData"), list):
            self._apply_reading(session, parse_meter_values({"meterValue": payload["transactionData"]}))
        session.record_energy(meter_stop)
        session.charging_stopped = True
        session.transaction_stopped_at = self._clock()
        session.stop_reason = payload.get("reason", "Local")
        self._record_power(session, 0.0)
        charge_s = (session.transaction_stopped_at - session.transaction_started_at).total_seconds() \
            if session.transaction_started_at else 0.0
        log.info("%s transaction %d stopped (%s): %.3f kWh in %s", session.label, transaction_id,
                 session.stop_reason, session.kwh_delivered, format_duration(charge_s))
        return {"idTagInfo": {"status": "Accepted"}}

    def _on_meter_values(self, charger_id, payload):
        reading = parse_meter_values(payload)
        connector_id = payload.get("connectorId", 1)      # the simple test JSON has none
        if isinstance(connector_id, bool) or not isinstance(connector_id, int) or connector_id < 0:
            raise OcppError("TypeConstraintViolation", "connectorId must be a whole number >= 0")

        if charger_id == self.cfg.site_meter_id:
            self._record_site_load(reading)
            return {}
        if connector_id == 0:
            return {}                     # the charger's own main meter: not a car

        charger = self.chargers[charger_id]
        session = None
        transaction_id = payload.get("transactionId")
        if isinstance(transaction_id, bool) or not isinstance(transaction_id, int):
            transaction_id = None
        if transaction_id is not None:
            session = self._find_transaction(charger, transaction_id)
        if session is None:
            session = charger.connector(connector_id).session
        if session is None:
            if transaction_id is not None and connector_id >= 1:
                # A transaction we never saw start (e.g. this server restarted mid-session):
                # adopt it so the car is controlled again.
                session = self._open_session(charger, connector_id,
                                             f"running transaction {transaction_id} adopted")
                session.transaction_id = transaction_id
                session.under_default = False   # it started before we knew: limit unknown
            elif self._valid_soc(charger_id, reading.soc) is not None:
                session = self._open_session(charger, connector_id, "SoC reported")  # SoC = a car
            else:
                return {}                 # meter values with no car attached: nothing to track
        self._apply_reading(session, reading)
        return {}

    def _on_data_transfer(self, charger_id, payload):
        reading = parse_data_transfer(payload)
        if reading.soc is None:
            return {"status": "UnknownMessageId"}
        self._on_meter_values(charger_id, {"connectorId": payload.get("connectorId", 1),
                                           "soc": reading.soc})
        return {"status": "Accepted"}

    # ---------------------------------------------------------------- readings and sessions

    def _record_site_load(self, reading: MeterReading):
        if reading.power_w is None:
            log.warning("site meter sent MeterValues without a readable Power.Active.Import; ignored")
            return
        self.site_load_kw = reading.power_w / 1000.0
        self.last_meter_timestamp = self._monotonic()
        # Alerts are raised once per episode, not on every reading.
        over_panel = self.site_load_kw > self.cfg.panel_limit_kw
        if over_panel and not self._over_panel:
            log.critical("CRITICAL ALERT: the site draws %.1f kW, above the %.1f kW panel limit; "
                         "throttling EVs now", self.site_load_kw, self.cfg.panel_limit_kw)
        elif self._over_panel and not over_panel:
            log.warning("site back under the panel limit (%.1f kW)", self.site_load_kw)
        self._over_panel = over_panel
        negative = self.site_load_kw < 0.0
        if negative and not self._ct_negative:
            log.critical("CRITICAL ALERT: the main-feeder CT reads %.1f kW (negative). Either the "
                         "clamp is installed backwards or the site is exporting power; neither can "
                         "be trusted for load management, so the EV budget is 0 until it reads "
                         "positive again", self.site_load_kw)
        elif self._ct_negative and not negative:
            log.warning("main-feeder CT reads positive again (%.1f kW)", self.site_load_kw)
        self._ct_negative = negative
        if self.site_load_kw > self.cfg.panel_limit_kw - self.cfg.safety_margin_kw:
            self.wake_now()             # into the safety margin: don't wait for the next cycle

    def wake_now(self):
        """Run the next allocation cycle immediately."""
        if self._wake is not None:
            self._wake.set()

    async def wait_for_next_cycle(self):
        """Sleep one allocation interval, or less if wake_now() is called."""
        if self._wake is None:
            self._wake = asyncio.Event()
        try:
            await asyncio.wait_for(self._wake.wait(), self.cfg.allocation_interval_s)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    def _valid_soc(self, label, soc):
        if soc is None:
            return None
        if not 0.0 <= soc <= 100.0:
            log.warning("%s reported SoC %.1f%%, outside 0-100; reading ignored", label, soc)
            return None
        return soc

    def _apply_reading(self, session: Session, reading: MeterReading):
        soc = self._valid_soc(session.label, reading.soc)
        if soc is not None:
            session.soc = soc
            if session.start_soc is None:
                session.start_soc = soc
        if reading.energy_wh is not None:
            session.record_energy(reading.energy_wh)
        if reading.power_w is not None:
            self._record_power(session, reading.power_w)
        if reading.current_a is not None:
            session.current_a = reading.current_a

    def _record_power(self, session: Session, watts: float):
        session.power_w, session.power_at = watts, self._monotonic()
        session.power_history.append((session.power_at, watts))
        # A charger that accepted a limit but keeps drawing well over it is not obeying.
        # The site stays safe (its excess shows up in the CT reading as other load), but
        # someone needs to know. Settling time: a car may take a few seconds to ramp down.
        if session.accepted_w is not None and session.limit_history and not session.overdraw_reported:
            since = session.power_at - session.limit_history[-1][0]
            if since >= OVERDRAW_SETTLE_S and watts > session.accepted_w * 1.05 + 200:
                session.overdraw_reported = True
                log.error("%s draws %.0f W, over the %d W limit it accepted %.0f s ago; its "
                          "excess is counted as other load", session.label, watts,
                          session.accepted_w, since)

    def _find_transaction(self, charger: Charger, transaction_id: int) -> Session | None:
        for session in charger.sessions():
            if session.transaction_id == transaction_id:
                return session
        return None

    def _open_session(self, charger: Charger, connector_id: int, why: str) -> Session:
        session = Session(session_id=str(uuid.uuid4()), charger_id=charger.charger_id,
                          connector_id=connector_id, plugged_in_at=self._clock(),
                          plugged_in_mono=self._monotonic())
        charger.connector(connector_id).session = session
        session.under_default = charger.default_accepted_at is not None
        log.info("%s car plugged in at %s (%s)", session.label,
                 gateway.utc_iso(session.plugged_in_at), why)
        self.wake_now()                     # give the new car its limit now, not next cycle
        return session

    def _close_session(self, charger: Charger, connector_id: int, reason: str):
        connector = charger.connector(connector_id)
        session, connector.session = connector.session, None
        if session is None:
            return
        ended_at = self._clock()
        duration_s = self._monotonic() - session.plugged_in_mono
        record = {
            "session_id": session.session_id,
            "charger_id": session.charger_id,
            "connector_id": session.connector_id,
            "transaction_id": session.transaction_id,
            "id_tag": session.id_tag,
            "plugged_in_at": gateway.utc_iso(session.plugged_in_at),
            "ended_at": gateway.utc_iso(ended_at),
            "plugged_in_duration_s": round(duration_s, 1),
            "transaction_started_at": _iso_or_none(session.transaction_started_at),
            "transaction_stopped_at": _iso_or_none(session.transaction_stopped_at),
            "stop_reason": session.stop_reason,
            "energy_kwh": round(session.kwh_delivered, 3),
            "start_soc": session.start_soc,
            "end_soc": session.soc,
            "last_current_a": session.current_a,
            "end_reason": reason,
            "used_charge_point_max_profile": session.used_fallback,
            "uncontrollable": session.uncontrollable,
            "peak_shifted_kwh": round(session.peak_shift_kwh, 3),
        }
        self.completed_sessions.append(record)
        self._write_session_log(record)
        log.info("%s session ended (%s): plugged in %s for %s, %.3f kWh delivered",
                 session.label, reason, record["plugged_in_at"], format_duration(duration_s),
                 session.kwh_delivered)

    def _write_session_log(self, record: dict):
        if not self.cfg.session_log_path:
            return
        try:
            path = Path(self.cfg.session_log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record) + "\n")
        except OSError as error:
            log.error("could not write session log %s: %s", self.cfg.session_log_path, error)

    def close_all_sessions(self, reason: str):
        for charger in self.chargers.values():
            for connector_id, connector in list(charger.connectors.items()):
                if connector.session is not None:
                    self._close_session(charger, connector_id, reason)

    def set_grid_signal(self, cap_kw: float | None):
        """Latest live grid cap from the cloud poller; None means no signal (offline)."""
        if cap_kw is None:
            self.grid_signal_cap_kw = self.grid_signal_at = None
            return
        value = to_number(cap_kw)
        if value is None or value < 0.0:
            log.warning("ignoring invalid grid signal cap %r", cap_kw)
            self.grid_signal_cap_kw = self.grid_signal_at = None
            return
        self.grid_signal_cap_kw, self.grid_signal_at = value, self._monotonic()

    def monotonic(self) -> float:
        return self._monotonic()

    def wall_clock(self) -> datetime:
        return self._clock()

    @property
    def telemetry_state(self) -> str | None:
        """'waiting', 'missing', 'stale' or 'ok' as of the last cycle (None before the first)."""
        return self._telemetry_state

    def peak_active(self, now: datetime) -> bool:
        """Is a tariff peak window in effect? The configured windows, unless overridden."""
        return gateway.in_peak_window(self.cfg, now) if self.peak_override is None else self.peak_override

    def set_peak_override(self, active: bool | None):
        """Force the peak window on (True) or off (False), or follow the clock (None)."""
        if active is not None and not isinstance(active, bool):
            raise TypeError("active must be True, False or None")
        if active != self.peak_override:
            log.info("peak window %s", "follows the configured schedule" if active is None
                     else f"forced {'ON' if active else 'OFF'}")
        self.peak_override = active
        self.wake_now()

    # ---------------------------------------------------------------- allocation cycle

    def plan(self) -> Plan:
        """Decide every car's power for this cycle. Sends nothing."""
        cfg, now = self.cfg, self._monotonic()
        self._expire_offline_sessions(now)
        max_w = gateway.watts_floor(cfg.max_charger_kw)

        reserved_w, sessions, ev_low_kw, ev_high_kw = 0, {}, [], []
        for charger in self.chargers.values():
            if charger.charger_id == cfg.site_meter_id:
                continue
            for connector_id, connector in charger.connectors.items():
                session = connector.session
                if session is None or session.charging_stopped:
                    continue
                if self.last_meter_timestamp is not None:
                    low, high = self._ev_draw_bounds_kw(session, connector, self.last_meter_timestamp)
                    ev_low_kw.append(low)
                    ev_high_kw.append(high)
                if session.uncontrollable:
                    reserved_w += max_w                 # it may draw anything up to the cap
                elif not charger.online:
                    reserved_w += self._committed_w(session)   # its last profile still applies
                elif connector.status != "Faulted":
                    sessions[(charger.charger_id, connector_id)] = session

        other_load_kw = None
        telemetry_age = None if self.last_meter_timestamp is None else now - self.last_meter_timestamp
        if self.site_load_kw is not None and self.site_load_kw < 0.0:
            # Backwards clamp or export: a negative load makes available_ev_budget_kw
            # return 0 (see the alert in _record_site_load).
            other_load_kw, self._other_load_prev = self.site_load_kw, None
        elif self.site_load_kw is not None:
            if telemetry_age is not None and telemetry_age > cfg.telemetry_stale_after_s:
                self._other_load_prev = None        # start afresh once readings return
            other_load_kw = gateway.estimate_other_load_kw(self.site_load_kw, ev_low_kw, ev_high_kw,
                                                           self._other_load_prev)
            self._other_load_prev = other_load_kw
        grid_age = None if self.grid_signal_at is None else now - self.grid_signal_at
        in_peak = self.peak_active(self._clock())
        decision = gateway.decide_ev_budget(cfg, other_load_kw, telemetry_age,
                                            self.grid_signal_cap_kw, grid_age, self._clock(), in_peak)
        self._log_budget_transitions(decision)

        weights = {key: gateway.session_weight(s.soc, s.kwh_delivered) for key, s in sessions.items()}
        tapered = {key for key, s in sessions.items() if self._is_tapered(s, now)}
        caps = {key: cfg.min_charger_kw for key in tapered}
        budget_w = gateway.watts_floor(decision.budget_kw)
        arrival = {key: s.plugged_in_mono for key, s in sessions.items()}
        allocations = gateway.allocate_power(weights, max(0, budget_w - reserved_w) / 1000, cfg, caps,
                                             arrival)
        targets_w = {key: gateway.watts_floor(kw) for key, kw in allocations.items()}
        committed_w = {key: self._committed_w(s) for key, s in sessions.items()}
        send_w = gateway.transition_safe_limits(targets_w, committed_w, budget_w, reserved_w, weights,
                                                gateway.watts_ceil(cfg.min_charger_kw))
        accepted_w = {key: s.accepted_w for key, s in sessions.items()}
        send_w = gateway.hold_small_cuts(send_w, committed_w, accepted_w,
                                         gateway.watts_floor(cfg.ct_tolerance_kw))
        return Plan(decision, self.site_load_kw, other_load_kw, reserved_w, targets_w, send_w,
                    sessions, tapered, in_peak)

    def _ev_draw_bounds_kw(self, session: Session, connector: Connector, t_ct: float):
        """(low, high): bounds on this car's draw at the instant the CT reading was taken.

        LOW, a lower bound (the CT and the chargers report at different instants, so
        readings are paired by time, and doubt resolves toward under-counting EV power):
        - the car's last reading at or before t_ct; with none, 0 (a car that only started
          drawing after t_ct is not in the CT reading at all);
        - a lower reading just after t_ct instead (the car may have stopped on its own),
          unless a new limit was accepted in between (then the drop is our own cut, which
          happened after t_ct, and the CT reading still contains the old draw);
        - capped by the limit in force at t_ct (a car still ramping down after a cut only
          counts its new limit), or by max_charger_kw when no limit is known (so a garbage
          reading such as 1e9 W cannot wipe out the other loads).
        HIGH, an upper bound: the same reading, unless the car's limit changed after that
        reading (or it has none) within the last J1772 response time + one meter sample
        interval: then up to the highest limit in force since. That is the longest a
        change can go unseen by a reporting charger. After it, an unconfirmed change no
        longer widens the band, so a charger that never reports power is counted the
        safe way (its draw as other load) instead of hiding a real load increase.
        Both are 0 when the car reports it is not drawing (SuspendedEV / SuspendedEVSE).
        """
        if connector.status in NOT_DRAWING_STATUSES:
            return 0.0, 0.0
        max_w = gateway.watts_floor(self.cfg.max_charger_kw)
        window = self.cfg.telemetry_stale_after_s
        before = after = None
        for t, watts in session.power_history:              # oldest first
            if t <= t_ct:
                if t_ct - t <= window:
                    before = (t, watts)
            elif after is None and t - t_ct <= window:
                after = (t, watts)
        limit = None
        changed_since_reading, highest_since_reading, cut_between = False, 0, False
        unseen_s = J1772_RESPONSE_S + self.cfg.meter_sample_interval_s
        for t, limit_w in session.limit_history:
            if t <= t_ct:
                limit = limit_w                             # the last limit accepted before t_ct
                if (before is None or t > before[0]) and t_ct - t <= unseen_s:
                    changed_since_reading = True
                    highest_since_reading = max(highest_since_reading, limit_w)
            elif after is not None and t <= after[0]:
                cut_between = True
        cap = max_w if limit is None else limit

        if before is None:
            # No usable reading: counted as 0 (its draw stays in "other loads", the safe
            # side), widened only by a limit change recent enough to explain the gap.
            low = 0.0
            high = float(highest_since_reading if changed_since_reading else 0)
        else:
            reading = max(0.0, before[1])
            low = reading
            if after is not None and not cut_between:
                low = min(low, max(0.0, after[1]))
            high = max(reading, highest_since_reading) if changed_since_reading else reading
            low = min(low, cap)
            high = min(max(high, low), max(cap, highest_since_reading))
        return low / 1000.0, max(low, high) / 1000.0

    def _committed_w(self, session: Session) -> int:
        """The most this car may be drawing right now: its accepted limit, or a higher one
        that was sent and may already be in force. No limit of its own yet: 0 if it began
        under the accepted 0 W site default, otherwise max_charger_kw (a charger with no
        profile charges at its hardware default)."""
        if session.accepted_w is not None:
            committed = session.accepted_w
        elif session.under_default:
            committed = 0
        else:
            committed = gateway.watts_floor(self.cfg.max_charger_kw)
        return max(committed, session.inflight_w or 0, session.uncertain_w or 0)

    async def _install_default_profile(self, charger: Charger) -> bool:
        """TxDefaultProfile on connector 0 at 0 W: every NEW session on this charger starts
        at 0 W instead of the charger's hardware default, and waits for its own limit
        (sent at once: plugging in triggers an immediate cycle). Without it, cars that
        plug in together would all draw full power before their first limit arrives.
        Returns False only when the charger did not answer."""
        message = gateway.build_charging_profile(0, 0.0, self.cfg, gateway.TX_DEFAULT_PROFILE)
        try:
            status = await self._send_profile(charger, message)
        except NoResponse as error:
            log.warning("%s did not answer the site default profile (%s); retrying next cycle",
                        charger.charger_id, error)
            return False
        charger.needs_default_profile = False
        if status == "Accepted":
            charger.default_accepted_at = self._monotonic()
        else:
            log.warning("%s answered %s to the 0 W site default profile; new sessions on it are "
                        "counted at %.3f kW until their first limit is accepted",
                        charger.charger_id, status, self.cfg.max_charger_kw)
        return True

    def _is_tapered(self, session: Session, now: float) -> bool:
        """Allowed to charge for at least taper_grace_s, yet drawing (almost) nothing."""
        return (session.power_w is not None and session.power_at is not None
                and now - session.power_at <= self.cfg.telemetry_stale_after_s
                and session.power_w < self.cfg.taper_threshold_w
                and session.limit_active_since is not None
                and now - session.limit_active_since >= self.cfg.taper_grace_s)

    def _expire_offline_sessions(self, now: float):
        for charger in self.chargers.values():
            if charger.online or charger.offline_since is None:
                continue
            if now - charger.offline_since > self.cfg.offline_session_timeout_s:
                for connector_id, connector in list(charger.connectors.items()):
                    if connector.session is not None:
                        self._close_session(charger, connector_id, "ChargerOffline")

    def _log_budget_transitions(self, decision: gateway.BudgetDecision):
        cfg, now = self.cfg, self._monotonic()
        if not decision.telemetry_stale:
            state = "ok"
        elif self.last_meter_timestamp is not None:
            state = "stale"                 # readings arrived before, then stopped
        elif now - self._started <= cfg.telemetry_stale_after_s:
            state = "waiting"               # just started: normal, the meter connects shortly
        else:
            state = "missing"               # never heard from the meter
        if state != self._telemetry_state:
            if state == "waiting":
                log.info("waiting for the first CT reading from %s; EV budget held at the %.1f kW "
                         "safe floor until it arrives", cfg.site_meter_id, cfg.stale_floor_kw)
            elif state == "missing":
                log.warning("no CT reading from %s in the %.0f s since startup (is the site meter "
                            "connected?); EV budget held at the %.1f kW safe floor",
                            cfg.site_meter_id, now - self._started, cfg.stale_floor_kw)
            elif state == "stale":
                log.warning("CT telemetry is stale: the last reading from %s is %.0f s old "
                            "(limit %.0f s); EV budget held at the %.1f kW safe floor",
                            cfg.site_meter_id, now - self.last_meter_timestamp,
                            cfg.telemetry_stale_after_s, cfg.stale_floor_kw)
            elif self._telemetry_state == "stale":
                log.info("CT telemetry restored (%.1f kW)", self.site_load_kw)
            elif self._telemetry_state in ("waiting", "missing"):
                log.info("first CT reading received (%.1f kW)", self.site_load_kw)
            self._telemetry_state = state
        if decision.cap_source != self._last_cap_source:
            if decision.cap_source == "grid":
                log.info("live grid signal caps the EV budget at %.1f kW", decision.cap_kw)
            elif decision.cap_source == "peak":
                log.info("tariff peak window: EV budget capped at %.1f kW (no live grid signal)",
                         decision.cap_kw)
            else:
                log.info("no grid or peak cap in effect")
            self._last_cap_source = decision.cap_source

    async def run_cycle(self) -> list[asyncio.Task]:
        """Plan, log one summary line, and start pushing limits. Returns the started send
        tasks; each charger is handled independently, so a slow or silent charger never
        delays the others or the next cycle."""
        plan = self.plan()
        if plan.sessions or plan.reserved_w:
            log.info(self._summary(plan))

        by_charger: dict[str, dict[int, int]] = {}
        for (charger_id, connector_id), watts in plan.send_w.items():
            by_charger.setdefault(charger_id, {})[connector_id] = watts

        tasks = []
        for charger_id, charger in self.chargers.items():
            if charger_id == self.cfg.site_meter_id or not charger.online or charger.busy:
                continue
            allocations = by_charger.get(charger_id, {})
            if not allocations and not charger.use_max_profile and not charger.needs_meter_config \
                    and not charger.needs_default_profile:
                continue
            charger.busy = True
            task = asyncio.create_task(self._apply(charger, allocations))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            tasks.append(task)
        self.last_plan = plan
        try:
            self._track_savings(plan)
        except Exception:                   # bookkeeping must never stop the control loop
            log.exception("savings tracking failed for this cycle")
        return tasks

    def actual_draw_kw(self, session: Session, now: float) -> float:
        """A car's draw now: its last power reading while fresh, otherwise (unknown) its
        committed limit, which can only make the savings estimate smaller."""
        if session.power_w is not None and session.power_at is not None \
                and now - session.power_at <= self.cfg.telemetry_stale_after_s:
            return max(0.0, session.power_w) / 1000.0
        return self._committed_w(session) / 1000.0

    def _track_savings(self, plan: Plan):
        """Peak demand and energy with vs. without load management (see SavingsTracker)."""
        now = self._monotonic()
        extra_kw = {}
        for (charger_id, connector_id), session in plan.sessions.items():
            actual = self.actual_draw_kw(session, now)
            status = self.chargers[charger_id].connector(connector_id).status
            drawing = ((charger_id, connector_id) not in plan.tapered
                       and status not in NOT_DRAWING_STATUSES
                       and (session.soc is None or session.soc < 100.0))
            extra_kw[(charger_id, connector_id)] = (
                gateway.unmanaged_ev_kw(actual, self.cfg, drawing=drawing) - actual)
        site_kw = None if plan.decision.telemetry_stale else plan.site_load_kw
        hours = self.savings.record(now, self._clock(), plan.in_peak, site_kw, sum(extra_kw.values()))
        for key, kw in extra_kw.items():
            plan.sessions[key].peak_shift_kwh += kw * hours

    def _summary(self, plan: Plan) -> str:
        d = plan.decision
        if d.telemetry_stale:
            head = f"EV budget {d.budget_kw:.3f} kW [STALE CT telemetry: safe floor]"
        else:
            head = (f"EV budget {d.budget_kw:.3f} kW [site {plan.site_load_kw:.1f} kW, "
                    f"other loads {plan.other_load_kw:.1f} kW]")
        if d.cap_source == "grid":
            head += f" [grid cap {d.cap_kw:.1f} kW]"
        elif d.cap_source == "peak":
            head += f" [peak cap {d.cap_kw:.1f} kW]"
        if plan.reserved_w:
            head += f" [{plan.reserved_w / 1000:.3f} kW reserved: offline/uncontrolled]"
        now = self._monotonic()
        parts = []
        for key, session in sorted(plan.sessions.items()):
            level = f"{session.soc:.1f}%" if session.soc is not None else f"{session.kwh_delivered:.2f} kWh in"
            sent, target = plan.send_w[key], plan.targets_w[key]
            if sent == target:
                limit = f"{sent} W"
            elif sent < target:
                limit = f"{sent} W (ramping to {target} W)"
            else:
                limit = f"{sent} W (held: {target} W is within CT accuracy)"
            tag = " tapered" if key in plan.tapered else ""
            parts.append(f"{session.label} {level} -> {limit}{tag} "
                         f"({format_duration(now - session.plugged_in_mono)})")
        return " | ".join([head] + parts)

    async def _apply(self, charger: Charger, allocations: dict):
        try:
            # Safety first: the site default for new sessions, then the cars' limits, then
            # the one-off meter configuration. Everything runs in this one task, so the
            # charger never has two of our CALLs outstanding, as OCPP-J requires.
            if charger.needs_default_profile:
                if not await self._install_default_profile(charger):
                    return                  # no answer: try again next cycle
            if charger.use_max_profile:
                await self._apply_max_profile(charger, allocations)
            else:
                for connector_id, watts in allocations.items():
                    await self._apply_connector(charger, connector_id, watts, allocations)
                    if charger.use_max_profile or not charger.online:
                        break               # switched to ChargePointMax, or the charger left
            if charger.needs_meter_config and charger.online:
                await self._configure_metering(charger)
        except Exception:
            log.exception("%s: error while sending limits", charger.charger_id)
        finally:
            charger.busy = False

    def _needs_send(self, purpose, watts, sent_purpose, sent_w, sent_at) -> bool:
        """Send when it matters, skip the chatter. No hysteresis band:
        - any decrease that reaches this point goes out at once (cuts too small to matter
          were already held back, within the site-wide CT accuracy, by the planner);
        - an increase goes out once it reaches min_increase_w (smaller ones only add network
          traffic; holding the lower value is always safe);
        - an unchanged or skipped value is refreshed every profile_refresh_s."""
        if sent_w is None or sent_purpose != purpose or watts < sent_w:
            return True
        if watts - sent_w >= self.cfg.min_increase_w:
            return True
        return self._monotonic() - sent_at >= self.cfg.profile_refresh_s

    async def _apply_connector(self, charger, connector_id, watts, allocations):
        session = charger.connector(connector_id).session
        if session is None or session.charging_stopped or session.uncontrollable:
            return                          # changed while earlier commands were in flight
        purpose = gateway.TX_PROFILE if session.transaction_id is not None else gateway.TX_DEFAULT_PROFILE
        message = gateway.build_charging_profile(connector_id, watts / 1000, self.cfg, purpose,
                                                 session.transaction_id)
        watts = int(gateway.profile_limit_w(message))       # 0 if below the minimum
        if not self._needs_send(purpose, watts, session.sent_purpose, session.sent_w, session.sent_at):
            return
        status = await self._send_tracked(charger, message, {connector_id: watts}, "SetChargingProfile")
        if status is None:
            return                          # no answer: retried next cycle
        if status == "Accepted":
            self._record_limit(session, watts)
            session.sent_purpose, session.sent_w, session.sent_at = purpose, watts, self._monotonic()
            return

        log.warning("%s answered %s to %s; retrying once with ChargePointMaxProfile",
                    session.label, status, purpose)
        accepted = await self._send_max_profile(charger, allocations)
        if accepted is None:
            return                          # no answer: try the whole sequence again next cycle
        if accepted:
            charger.use_max_profile = True
            session.used_fallback = True
            log.warning("%s is now limited through ChargePointMaxProfile (stackLevel 0)",
                        charger.charger_id)
        else:
            await self._escalate(charger, session, f"{purpose} {status}, ChargePointMaxProfile rejected")

    async def _apply_max_profile(self, charger, allocations):
        watts = sum(allocations.values())
        if not self._needs_send(gateway.CHARGE_POINT_MAX_PROFILE, watts, gateway.CHARGE_POINT_MAX_PROFILE,
                                charger.max_profile_w, charger.max_profile_at):
            return
        accepted = await self._send_max_profile(charger, allocations)
        if accepted is False:
            # It no longer accepts the fallback either. Stop using it; the next session
            # on this charger starts again from TxProfile.
            charger.use_max_profile = False
            for connector_id in allocations:
                session = charger.connector(connector_id).session
                if session is not None and not session.charging_stopped:
                    await self._escalate(charger, session, "ChargePointMaxProfile rejected")

    async def _send_max_profile(self, charger, allocations) -> bool | None:
        """ChargePointMaxProfile for the whole charger = the sum of its cars' limits (whole
        watts, so the sum is exact). True accepted, False refused, None no answer."""
        message = gateway.build_charging_profile(0, sum(allocations.values()) / 1000, self.cfg,
                                                 gateway.CHARGE_POINT_MAX_PROFILE,
                                                 start_schedule=self._clock())
        status = await self._send_tracked(charger, message, allocations, "ChargePointMaxProfile")
        if status is None:
            return None
        if status != "Accepted":
            log.warning("%s answered %s to ChargePointMaxProfile", charger.charger_id, status)
            return False
        charger.max_profile_w = int(gateway.profile_limit_w(message))
        charger.max_profile_at = self._monotonic()
        for connector_id, watts in allocations.items():
            session = charger.connector(connector_id).session
            if session is not None:
                self._record_limit(session, watts)
        return True

    async def _send_tracked(self, charger, message, limits_w: dict, what: str) -> str | None:
        """Send a profile while counting its limits as possibly in force (in flight), so no
        other car is given that headroom meanwhile. No answer -> None, and the limits stay
        counted as possibly in force until a later answer settles them."""
        sessions = {cid: charger.connector(cid).session for cid in limits_w}
        for connector_id, session in sessions.items():
            if session is not None:
                session.inflight_w = limits_w[connector_id]
        try:
            status = await self._send_profile(charger, message)
        except NoResponse as error:
            log.warning("%s did not answer %s (%s); retrying next cycle", charger.charger_id, what, error)
            for connector_id, session in sessions.items():
                if session is not None:
                    session.uncertain_w = max(session.uncertain_w or 0, limits_w[connector_id])
            return None
        finally:
            for session in sessions.values():
                if session is not None:
                    session.inflight_w = None
        if status == "Accepted":
            for session in sessions.values():
                if session is not None:
                    session.uncertain_w = None
        return status

    async def _send_profile(self, charger, message) -> str:
        """'Accepted', 'Rejected', 'NotSupported', or 'CALLERROR <code>'. Raises NoResponse."""
        try:
            response = await self.call(charger, message)
        except CallFailed as error:
            return f"CALLERROR {error.code}"
        status = response.get("status") if isinstance(response, dict) else None
        return status if isinstance(status, str) else "an invalid reply"

    def _record_limit(self, session: Session, watts: int):
        min_w = gateway.watts_ceil(self.cfg.min_charger_kw)
        if watts >= min_w:
            if session.accepted_w is None or session.accepted_w < min_w:
                session.limit_active_since = self._monotonic()
        else:
            session.limit_active_since = None
        session.accepted_w = watts
        session.limit_history.append((self._monotonic(), watts))

    async def _configure_metering(self, charger: Charger):
        """Ask the charger for frequent power readings: the EV share of the CT reading can
        only be subtracted when a charger's reading is fresh (OCPP 1.6 standard keys)."""
        settings = [("MeterValueSampleInterval", str(self.cfg.meter_sample_interval_s)),
                    ("MeterValuesSampledData", ",".join(METER_MEASURANDS))]
        for key, value in settings:
            status = await self._change_configuration(charger, key, value)
            if key == "MeterValuesSampledData" and status not in (None, "Accepted", "RebootRequired"):
                # Most AC chargers cannot read the car's SoC; ask again without it.
                status = await self._change_configuration(charger, key, ",".join(METER_MEASURANDS[:-1]))
            if status is None:
                return                      # no answer: try again next cycle
            if status == "RebootRequired":
                log.warning("%s needs a reboot to apply %s=%s", charger.charger_id, key, value)
            elif status != "Accepted":
                log.warning("%s answered %s to %s=%s; its power readings may be too infrequent "
                            "for the EV share of the CT reading to be subtracted",
                            charger.charger_id, status, key, value)
        charger.needs_meter_config = False

    async def _change_configuration(self, charger, key, value) -> str | None:
        message = [CALL, str(uuid.uuid4()), "ChangeConfiguration", {"key": key, "value": value}]
        try:
            response = await self.call(charger, message)
        except NoResponse:
            return None
        except CallFailed as error:
            return f"CALLERROR {error.code}"
        status = response.get("status") if isinstance(response, dict) else None
        return status if isinstance(status, str) else "an invalid reply"

    async def _escalate(self, charger: Charger, session: Session, why: str):
        """Last resort: the charger refuses every limit, so its car is stopped."""
        session.uncontrollable = True       # from now on its full cap is held in reserve
        if session.transaction_id is None:
            log.critical("CRITICAL ALERT: %s refuses all charging limits (%s) and has no "
                         "transaction to stop; manual intervention required", session.label, why)
            return
        log.critical("CRITICAL ALERT: %s refuses all charging limits (%s); sending "
                     "RemoteStopTransaction for transaction %d", session.label, why,
                     session.transaction_id)
        message = [CALL, str(uuid.uuid4()), "RemoteStopTransaction",
                   {"transactionId": session.transaction_id}]
        try:
            response = await self.call(charger, message)
        except (NoResponse, CallFailed) as error:
            log.critical("CRITICAL ALERT: %s RemoteStopTransaction failed (%s); manual "
                         "intervention required", session.label, error)
            return
        status = response.get("status") if isinstance(response, dict) else None
        if status == "Accepted":
            log.critical("%s accepted RemoteStopTransaction; waiting for its StopTransaction",
                         session.label)
        else:
            log.critical("CRITICAL ALERT: %s answered %s to RemoteStopTransaction; manual "
                         "intervention required", session.label, status)

    async def call(self, charger: Charger, message: list):
        """Send one CALL to a charger and wait for its CALLRESULT payload."""
        if not charger.online or charger.send is None:
            raise NoResponse("charger is offline")
        future = asyncio.get_running_loop().create_future()
        charger.pending[message[1]] = future
        try:
            try:
                await charger.send(json.dumps(message))
            except Exception as error:      # e.g. websockets ConnectionClosed mid-send
                raise NoResponse(f"send failed: {error}") from error
            return await asyncio.wait_for(future, self.cfg.call_timeout_s)
        except asyncio.TimeoutError:
            raise NoResponse(f"no reply within {self.cfg.call_timeout_s:g} s")
        finally:
            charger.pending.pop(message[1], None)


def _require(payload, key, kind):
    if key not in payload:
        raise OcppError("OccurenceConstraintViolation", f"{key} is required")   # sic: OCPP 1.6 spelling
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, kind):
        raise OcppError("TypeConstraintViolation", f"{key} must be {kind.__name__}")
    return value


def _require_number(payload, key):
    if key not in payload:
        raise OcppError("OccurenceConstraintViolation", f"{key} is required")
    value = to_number(payload[key])
    if value is None or isinstance(payload[key], str):
        raise OcppError("TypeConstraintViolation", f"{key} must be a number")
    return value


def _iso_or_none(moment):
    return gateway.utc_iso(moment) if moment is not None else None


# ==================================================================== websocket glue

def charger_id_from_path(path: str) -> str | None:
    """'/CP_1' -> 'CP_1'; '/ocpp/CP_1?x=1' -> 'CP_1' (OCPP: the last path segment)."""
    segment = urlsplit(path).path.rstrip("/").rsplit("/", 1)[-1]
    charger_id = unquote(segment).strip()
    if not charger_id or not charger_id.isprintable():
        return None                         # control characters could forge log lines
    return charger_id


async def handle_connection(cs: CentralSystem, connections: dict, ws):
    from websockets.exceptions import ConnectionClosed

    charger_id = charger_id_from_path(ws.request.path)
    if charger_id is None:
        await ws.close(1008, "the URL must end with the charge point id")
        return
    if ws.subprotocol != "ocpp1.6":
        log.warning("%s did not negotiate the ocpp1.6 subprotocol", charger_id)

    async def send(text):
        await ws.send(text)

    previous = connections.get(charger_id)
    connections[charger_id] = ws
    cs.attach(charger_id, send)
    if previous is not None:
        await previous.close(1000, "replaced by a new connection")
    try:
        async for raw in ws:
            reply = cs.on_message(charger_id, raw)
            if reply is not None:
                await ws.send(reply)
    except ConnectionClosed:
        pass                                # dropped without a close frame: normal for chargers
    finally:
        if connections.get(charger_id) is ws:
            del connections[charger_id]
        cs.detach(charger_id, send)


async def allocation_loop(cs: CentralSystem):
    """Runs forever. A failure in one cycle is logged and the next cycle runs as usual."""
    while True:
        await cs.wait_for_next_cycle()
        try:
            await cs.run_cycle()
        except Exception:
            log.exception("allocation cycle failed; continuing with the next cycle")


async def fetch_grid_signal_stub() -> float | None:
    """Placeholder for a live CAISO / GridStatus integration: makes no network call and
    always reports "no signal", so the tariff peak windows apply."""
    return None


async def grid_signal_poller(cs: CentralSystem, fetch=fetch_grid_signal_stub):
    """Polls `fetch` (an async callable returning a cap in kW, or None) forever."""
    while True:
        try:
            cs.set_grid_signal(await fetch())
        except Exception:
            log.exception("grid signal poll failed; using the tariff peak windows")
            cs.set_grid_signal(None)
        await asyncio.sleep(cs.cfg.grid_signal_poll_interval_s)


async def start_server(cs: CentralSystem, host: str, port: int):
    """Start listening. Returns the websockets server (use .sockets for the bound port)."""
    from websockets.asyncio.server import serve

    connections: dict = {}
    return await serve(lambda ws: handle_connection(cs, connections, ws), host, port,
                       subprotocols=["ocpp1.6"])


async def main(cfg: SiteConfig):
    import dashboard

    cs = CentralSystem(cfg)
    server = await start_server(cs, cfg.host, cfg.port)
    log.info("OCPP 1.6J central system listening on ws://%s:%d", cfg.host, cfg.port)
    web = await dashboard.start_dashboard(cs, cfg.host, cfg.dashboard_port)
    log.info("dashboard on http://%s:%d (demo controls are off on a live site)", cfg.host, web.port)
    background = [asyncio.create_task(allocation_loop(cs))]
    if cfg.grid_signal_enabled:
        log.info("grid signal poller enabled (stub: no live API is called)")
        background.append(asyncio.create_task(grid_signal_poller(cs)))
    try:
        await asyncio.gather(*background)
    finally:
        for task in background:
            task.cancel()
        server.close()
        await server.wait_closed()
        await web.close()
        cs.close_all_sessions("ServerShutdown")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OCPP 1.6J EV load-management server")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="path to site_config.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    logging.getLogger("websockets").setLevel(logging.WARNING)   # we log connections ourselves
    try:
        site_config = load_site_config(args.config)
    except ConfigError as error:
        raise SystemExit(f"config error: {error}")
    try:
        asyncio.run(main(site_config))
    except KeyboardInterrupt:
        pass
