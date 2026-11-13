"""Tracking: where each truck is, its projected ETA, and whether it makes the deadline. Owner: agent & evals lead.

`estimate()` works from one ping plus the shipment's schedule, the way it would with a real
telematics/ELD feed. It deliberately uses planning assumptions (ASSUMED_MPH, repair time, …), not the
simulator's internals, and lists every assumption on the status so the UI can show it.

`TrackingFeed` wraps a simulation (meridian.sim) and answers "as of time t" questions for the UI:
statuses, positions, the event stream (telematics + driver reports + copilot alerts) and history.
"""

from __future__ import annotations

import bisect
import math
from datetime import datetime, timedelta

from . import geo, sim
from .models import FeedEvent, Ping, TrackingStatus

ASSUMED_MPH = 50.0            # planning average for a moving truck
HOS_DRIVE_H, HOS_BREAK_H = 11.0, 10.0
SHORT_BREAK_AFTER_H = 8.0     # 30-min break after 8 h driving
LOADING_LEFT_H = 1.0          # loading in progress: assume ~1 h more
REPAIR_H = 3.0                # mechanical breakdown with no repair ETA: assume 3 h from the stop
AT_RISK_SLACK_MIN = 120       # less slack than this before the window closes = at risk
LATE_TOLERANCE_MIN = 30       # projections hours out aren't that precise: within 30 min late = at risk
LONG_STOP_MIN = 60            # unplanned stop this long = at risk
ALERT_GRID = timedelta(minutes=15)
ETA_SLIP_ALERT_MIN = 60
BACK_ON_TIME_SLACK_MIN = 60
RANK = {"ON_TIME": 0, "AT_RISK": 1, "LATE": 2}


# --------------------------------------------------------------------------- lane facts

def lane(shipment: dict, facilities: dict[str, dict]) -> dict:
    stops = shipment.get("stops") or []
    p, d = (stops[0], stops[-1]) if len(stops) >= 2 else ({}, {})
    pw, dw = p.get("scheduled_window") or {}, d.get("scheduled_window") or {}
    dest_fac = facilities.get(d.get("facility_id") or "", {})
    tz_name = dw.get("timezone") or dest_fac.get("timezone")
    origin, dest = geo.stop_coords(p, facilities) if p else None, geo.stop_coords(d, facilities) if d else None
    crow = geo.haversine_mi(origin, dest) if origin and dest else 0
    miles = float(shipment.get("miles") or 0)
    return {
        "origin": origin, "dest": dest, "miles": miles or crow * 1.2,
        "road_factor": (miles / crow) if miles and crow > 1 else 1.2,
        "pickup_open": geo.local_to_utc(pw.get("earliest_local"), pw.get("timezone")),
        "window_end": geo.local_to_utc(dw.get("latest_local"), tz_name),
        "mabd_end": geo.end_of_day_utc(d.get("mabd_date"), tz_name),
        "tz": tz_name, "dest_name": dest_fac.get("name") or d.get("facility_id"),
    }


# --------------------------------------------------------------------------- the estimate

def drive_time(miles: float, hos_left: float | None) -> tuple[timedelta, int]:
    """Hours to cover `miles` at ASSUMED_MPH, with 10 h breaks whenever the driver's hours run out,
    and a 30-min break per 8 h stretch. Returns (duration, number of 10 h breaks)."""
    hours = miles / ASSUMED_MPH
    left = HOS_DRIVE_H if hos_left is None else max(hos_left, 0.0)
    breaks = 0 if hours <= left else math.ceil((hours - left) / HOS_DRIVE_H)
    short = sum(int(min(hours, left if i == 0 else HOS_DRIVE_H) > SHORT_BREAK_AFTER_H) for i in range(breaks + 1))
    return timedelta(hours=hours + breaks * HOS_BREAK_H + short * 0.5), breaks


def estimate(shipment: dict, facilities: dict[str, dict], ping: Ping | None, now: datetime,
             stopped_since: datetime | None = None, arrived_at: datetime | None = None) -> TrackingStatus:
    sid = str(shipment["shipment_id"])
    ln = lane(shipment, facilities)
    base = dict(shipment_id=sid, at=now, window_end=ln["window_end"], mabd_end=ln["mabd_end"], delivery_tz=ln["tz"])
    if ping is None:
        return TrackingStatus(state="NOT_TRACKED", status="NOT_TRACKED", **base)

    pos = (ping.lat, ping.lon)
    assumptions: list[str] = []
    stopped_min = int((now - stopped_since).total_seconds() // 60) if ping.state == "STOPPED" and stopped_since else 0
    miles_left = 0.0
    if ping.state in ("AT_DELIVERY", "DELIVERED"):
        eta = arrived_at or ping.at
    else:
        if ping.state == "AT_PICKUP":
            miles_left = ln["miles"]
            if ln["pickup_open"] and now < ln["pickup_open"]:
                start = ln["pickup_open"] + timedelta(hours=LOADING_LEFT_H * 1.5)
                assumptions.append(f"loads in {LOADING_LEFT_H * 1.5:g} h once the pickup window opens")
            else:
                start = now + timedelta(hours=LOADING_LEFT_H)
                assumptions.append(f"loading finishes in ~{LOADING_LEFT_H:g} h")
            hos = HOS_DRIVE_H
            if ping.hos_drive_hours_left is not None and not (ln["pickup_open"] and now < ln["pickup_open"]):
                hos = max(ping.hos_drive_hours_left - LOADING_LEFT_H, 0.0)
                assumptions.append(f"driver has {hos:.1f} h of driving left after loading (ELD, 14 h duty clock)")
        else:
            miles_left = geo.haversine_mi(pos, ln["dest"]) * ln["road_factor"] if ln["dest"] else 0.0
            hos, start = ping.hos_drive_hours_left, now
            if ping.state == "STOPPED":
                if ping.event_code == "HOS" and ping.hos_available_at:
                    start, hos = max(now, ping.hos_available_at), HOS_DRIVE_H
                    assumptions.append("driver back on duty when the ELD says")
                elif ping.event_code == "AI":
                    start = max(now, (stopped_since or now) + timedelta(hours=REPAIR_H))
                    assumptions.append(f"breakdown repaired ~{REPAIR_H:g} h after the stop (no repair ETA reported)")
                elif ping.event_code != "BREAK":
                    assumptions.append("truck moves again now")
        drive, breaks = drive_time(miles_left, hos)
        eta = start + drive
        assumptions.append(f"{ASSUMED_MPH:g} mph average over {miles_left:.0f} mi"
                           + (f", {breaks} × 10 h HOS break" if breaks else ""))

    late = round((eta - ln["window_end"]).total_seconds() / 60) if ln["window_end"] else None
    late_mabd = round((eta - ln["mabd_end"]).total_seconds() / 60) if ln["mabd_end"] else None
    unplanned_stop = ping.state == "STOPPED" and ping.event_code not in ("HOS", "BREAK") and stopped_min >= LONG_STOP_MIN

    if ping.state == "DELIVERED":
        status = "DELIVERED"
    elif (late is not None and late > LATE_TOLERANCE_MIN) or (late_mabd is not None and late_mabd > 0):
        status = "LATE"
    elif (late is not None and late > -AT_RISK_SLACK_MIN) or unplanned_stop:
        status = "AT_RISK"
    else:
        status = "ON_TIME"

    return TrackingStatus(
        **base, state=ping.state, status=status, lat=ping.lat, lon=ping.lon, near=geo.near(pos),
        speed_mph=ping.speed_mph, miles_remaining=round(miles_left, 1), stopped_minutes=stopped_min,
        event_code=ping.event_code, hos_drive_hours_left=ping.hos_drive_hours_left, eta=eta,
        minutes_late=late, minutes_late_mabd=late_mabd, assumptions=assumptions, simulated=ping.simulated,
    )


def describe_lateness(st: TrackingStatus) -> str:
    """'3.2 h past the window end, misses the MABD' / '45 min to spare'."""
    if st.minutes_late is None:
        return ""
    def dur(m: int) -> str:
        return f"{m / 60:.1f} h" if abs(m) >= 90 else f"{abs(m)} min"
    text = f"{dur(st.minutes_late)} past the window end" if st.minutes_late > 0 else f"{dur(-st.minutes_late)} to spare"
    if st.minutes_late_mabd is not None and st.minutes_late_mabd > 0:
        text += ", misses the MABD"
    return text


# --------------------------------------------------------------------------- the feed

class TrackingFeed:
    """A simulation plus everything the UI asks about it at a given time."""

    def __init__(self, shipments: list[dict], facilities: dict[str, dict], start: datetime | None = None,
                 hours: float = 96, scenario: list[sim.Incident] | None = None, seed: int = 7):
        self.shipments = {str(s["shipment_id"]): s for s in shipments}
        self.facilities = facilities
        self.sim = sim.run(shipments, facilities, start=start, hours=hours, scenario=scenario, seed=seed)
        self.start, self.end = self.sim.start, self.sim.end
        self.incidents = sim.active_scenario(shipments, scenario)
        self._times: dict[str, list[datetime]] = {}
        self._stopped_since: dict[str, list[datetime | None]] = {}
        self._arrived_at: dict[str, list[datetime | None]] = {}
        for sid, pings in self.sim.pings.items():
            self._times[sid] = [p.at for p in pings]
            since, arrived, ss, aa = None, None, [], []
            for i, p in enumerate(pings):
                prev = pings[i - 1].state if i else None
                since = (since if prev == "STOPPED" else p.at) if p.state == "STOPPED" else None
                if p.state in ("AT_DELIVERY", "DELIVERED"):
                    arrived = arrived if prev in ("AT_DELIVERY", "DELIVERED") else p.at
                else:
                    arrived = None
                ss.append(since)
                aa.append(arrived)
            self._stopped_since[sid], self._arrived_at[sid] = ss, aa
        self._alerts = self._build_alerts()

    @property
    def tracked_ids(self) -> list[str]:
        return list(self.sim.pings)

    def _index(self, sid: str, t: datetime) -> int | None:
        i = bisect.bisect_right(self._times.get(sid, []), t) - 1
        return i if i >= 0 else None

    def ping(self, sid: str, t: datetime) -> Ping | None:
        i = self._index(sid, t)
        return None if i is None else self.sim.pings[sid][i]

    def status(self, sid: str, t: datetime) -> TrackingStatus:
        i = self._index(sid, t)
        ping = None if i is None else self.sim.pings[sid][i]
        return estimate(self.shipments[sid], self.facilities, ping, t,
                        stopped_since=None if i is None else self._stopped_since[sid][i],
                        arrived_at=None if i is None else self._arrived_at[sid][i])

    def statuses(self, t: datetime) -> dict[str, TrackingStatus]:
        return {sid: self.status(sid, t) for sid in self.tracked_ids}

    def history(self, sid: str, t: datetime) -> list[Ping]:
        i = self._index(sid, t)
        return [] if i is None else self.sim.pings[sid][: i + 1]

    def events(self, t: datetime, shipment_id: str | None = None, limit: int = 60) -> list[FeedEvent]:
        """Telematics, driver reports and copilot alerts up to t, newest first."""
        evs = [e for e in self.sim.events + self._alerts
               if e.at <= t and (shipment_id is None or e.shipment_id == shipment_id)]
        evs.sort(key=lambda e: (e.at, e.source == "copilot"), reverse=True)
        return evs[:limit]

    # ------------------------------------------------------------------ copilot alerts

    def _build_alerts(self) -> list[FeedEvent]:
        """Walk the timeline every 15 min and raise alerts on status changes, ETA slips and long stops."""
        alerts: list[FeedEvent] = []
        alerted_eta: dict[str, datetime] = {}
        alerted_rank: dict[str, int] = {}
        stop_alerted: set[tuple[str, datetime]] = set()
        t = self.start
        while t <= self.end:
            for sid in self.tracked_ids:
                i = self._index(sid, t)
                if i is None:
                    continue
                st = self.status(sid, t)
                tzn = st.delivery_tz
                rank = RANK.get(st.status, 0)
                if rank > alerted_rank.get(sid, 0):
                    level = "alert" if st.status == "LATE" else "warning"
                    word = "Running late" if st.status == "LATE" else "At risk"
                    alerts.append(FeedEvent(at=t, shipment_id=sid, source="copilot", level=level,
                                            text=f"{word}: ETA {geo.fmt_local(st.eta, tzn)}, {describe_lateness(st)}"))
                    alerted_eta[sid], alerted_rank[sid] = st.eta, rank
                elif alerted_rank.get(sid) and st.status == "ON_TIME" and (st.minutes_late or 0) <= -BACK_ON_TIME_SLACK_MIN:
                    alerted_rank[sid] = 0
                    alerts.append(FeedEvent(at=t, shipment_id=sid, source="copilot", level="info",
                                            text=f"Back on schedule: ETA {geo.fmt_local(st.eta, tzn)}, {describe_lateness(st)}"))
                elif st.status in ("LATE", "AT_RISK") and st.eta and sid in alerted_eta \
                        and (st.eta - alerted_eta[sid]) >= timedelta(minutes=ETA_SLIP_ALERT_MIN):
                    slip = (st.eta - alerted_eta[sid]).total_seconds() / 3600
                    alerts.append(FeedEvent(at=t, shipment_id=sid, source="copilot", level="alert",
                                            text=f"ETA slipped {slip:.1f} h to {geo.fmt_local(st.eta, tzn)}, {describe_lateness(st)}"))
                    alerted_eta[sid] = st.eta
                since = self._stopped_since[sid][i]
                if st.state == "STOPPED" and st.event_code not in ("HOS", "BREAK") and since \
                        and st.stopped_minutes >= 45 and (sid, since) not in stop_alerted:
                    stop_alerted.add((sid, since))
                    alerts.append(FeedEvent(at=t, shipment_id=sid, source="copilot", level="warning",
                                            text=f"Stopped {st.stopped_minutes} min {st.near}"))
            t += ALERT_GRID
        return alerts
