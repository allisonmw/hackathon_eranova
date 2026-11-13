"""Simulated GPS/ELD feed for open loads. Owner: agent & evals lead.

    SIMULATED DATA. The TMS has no truck positions, so this module makes them up, deterministically,
    from the real open shipments: same input + same seed = the same trucks, stops and incidents.

Each truck follows a straight line from its pickup to its delivery city, at a cruise speed drawn per
truck, obeying hours of service (11 h driving, then a 10 h break). It waits for the pickup window,
loads, drives, and at the receiver waits for the delivery window and receiving hours, then unloads.
A short scripted scenario adds incidents (breakdown, traffic, weather, a slow shipper, a driver low on
hours) so the demo shows the same story every time.

meridian.tracking reads only the pings this produces (what a real telematics feed would give), never
the speeds or incident plans below, so its ETAs are a genuine estimate rather than a replay.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from . import geo
from .geo import UTC
from .models import FeedEvent, Ping

TRACKED_STATUSES = {"DISPATCHED", "AT_PICKUP", "PICKED_UP", "IN_TRANSIT", "AT_DELIVERY"}
STEP = timedelta(minutes=5)
HOS_DRIVE_H, HOS_BREAK_H, HOS_DUTY_H = 11.0, 10.0, 14.0
CRUISE_MPH = {"TL": (52, 60), "LTL": (44, 50), "EXPEDITE": (57, 63)}

IncidentKind = Literal["BREAKDOWN", "TRAFFIC", "WEATHER", "SHIPPER_DELAY", "HOS_SHORT"]


@dataclass(frozen=True)
class Incident:
    shipment_id: str
    kind: IncidentKind
    after_hours: float = 0.0       # hours after the sim start when it begins (ignored for HOS_SHORT)
    duration_hours: float = 1.0    # how long it lasts; for HOS_SHORT, the driving hours the driver has left


# Scripted for the 2026-11-13 snapshot. Incidents for loads that aren't open are dropped.
DEFAULT_SCENARIO: list[Incident] = [
    Incident("7300159", "BREAKDOWN", after_hours=29.5, duration_hours=4.5),    # Des Moines → Aurora CO, MABD 11/15
    Incident("7300138", "SHIPPER_DELAY", after_hours=0.5, duration_hours=6.0),  # still loading in Kansas City
    Incident("7300137", "HOS_SHORT", duration_hours=0.75),                       # Phoenix → El Paso, driver nearly out of hours
    Incident("7300139", "TRAFFIC", after_hours=1.0, duration_hours=3.0),        # Pittsburgh → Newark
    Incident("7300147", "WEATHER", after_hours=32.0, duration_hours=6.0),       # Chicago → Dallas, MABD 11/16
]

INCIDENT_TEXT = {
    "BREAKDOWN": ("driver", "AI", "Driver reports mechanical breakdown; truck stopped"),
    "TRAFFIC": ("telematics", None, "Heavy traffic: speed down"),
    "WEATHER": ("driver", "AO", "Driver reports bad weather; driving slowly"),
    "SHIPPER_DELAY": ("driver", None, "Driver reports shipper is behind; still waiting to load"),
}


@dataclass
class SimResult:
    start: datetime
    end: datetime
    pings: dict[str, list[Ping]]                       # per shipment, every 5 min, oldest first
    events: list[FeedEvent]                            # oldest first
    routes: dict[str, tuple[tuple[float, float], tuple[float, float]]]
    untracked: dict[str, str] = field(default_factory=dict)   # shipment_id -> why it has no truck


def data_now(shipments: list[dict]) -> datetime:
    """The 'now' of the data: the latest status update, rounded up to 5 minutes."""
    times = [t for s in shipments if (t := geo.parse_utc(s.get("status_updated_at")))]
    now = max(times) if times else datetime.now(UTC)
    now = now.replace(second=0, microsecond=0)
    return now + timedelta(minutes=(-now.minute) % 5)


def active_scenario(shipments: list[dict], scenario: list[Incident] | None = None) -> list[Incident]:
    ids = {str(s["shipment_id"]) for s in shipments if s.get("status") in TRACKED_STATUSES}
    return [i for i in (DEFAULT_SCENARIO if scenario is None else scenario) if i.shipment_id in ids]


def run(shipments: list[dict], facilities: dict[str, dict], start: datetime | None = None,
        hours: float = 96, scenario: list[Incident] | None = None, seed: int = 7) -> SimResult:
    start = start or data_now(shipments)
    end = start + timedelta(hours=hours)
    incidents = active_scenario(shipments, scenario)
    result = SimResult(start=start, end=end, pings={}, events=[], routes={})
    for s in shipments:
        sid = str(s["shipment_id"])
        if s.get("status") not in TRACKED_STATUSES:
            continue
        stops = s.get("stops") or []
        if len(stops) < 2:
            result.untracked[sid] = "fewer than two stops"
            continue
        origin, dest = geo.stop_coords(stops[0], facilities), geo.stop_coords(stops[-1], facilities)
        if not origin or not dest:
            result.untracked[sid] = "no coordinates for the pickup or delivery city"
            continue
        result.routes[sid] = (origin, dest)
        truck = _Truck(s, facilities, origin, dest, start, [i for i in incidents if i.shipment_id == sid],
                       random.Random(f"{seed}:{sid}"))
        result.pings[sid], events = truck.drive(start, end)
        result.events += events
    result.events.sort(key=lambda e: e.at)
    return result


# --------------------------------------------------------------------------- one truck

class _Truck:
    def __init__(self, s: dict, facilities: dict[str, dict], origin, dest, start: datetime,
                 incidents: list[Incident], rng: random.Random):
        self.sid, self.s, self.rng = str(s["shipment_id"]), s, rng
        self.origin, self.dest = origin, dest
        p_stop, d_stop = s["stops"][0], s["stops"][-1]
        self.pickup_fac = facilities.get(p_stop.get("facility_id") or "", {})
        self.dest_fac = facilities.get(d_stop.get("facility_id") or "", {})
        pw, dw = p_stop.get("scheduled_window") or {}, d_stop.get("scheduled_window") or {}
        self.pickup_open = geo.local_to_utc(pw.get("earliest_local"), pw.get("timezone")) or start
        self.delivery_open = geo.local_to_utc(dw.get("earliest_local"), dw.get("timezone"))
        self.delivery_close = geo.local_to_utc(dw.get("latest_local"), dw.get("timezone"))
        self.since_break = 0.0   # driving hours since the last 30-min break
        self.total = float(s.get("miles") or geo.haversine_mi(origin, dest) * 1.2)
        lo, hi = CRUISE_MPH.get(s.get("mode"), CRUISE_MPH["TL"])
        self.cruise = rng.uniform(lo, hi)
        self.incidents = {i.kind: i for i in incidents}
        self.start = start
        self.events: list[FeedEvent] = []
        self.code: str | None = None
        self.until: datetime | None = None
        self.hos = HOS_DRIVE_H
        self.duty_start: datetime | None = None   # start of the 14 h on-duty window
        self.done = 0.0
        self.broke_down = False
        status = s.get("status")

        if status == "DISPATCHED":
            self.phase = "WAIT_PICKUP" if start < self.pickup_open else "LOADING"
            if self.phase == "LOADING":
                self.until, self.duty_start = start + self._dwell(self.pickup_fac), start
        elif status == "AT_PICKUP":
            self.phase, self.until = "LOADING", start + timedelta(minutes=rng.uniform(30, 100))
            self.duty_start = start - timedelta(hours=rng.uniform(0.5, 1.5))
        elif status == "AT_DELIVERY":
            self.phase, self.done = "UNLOADING", self.total
            self.until = start + timedelta(minutes=rng.uniform(30, 110))
        else:  # PICKED_UP / IN_TRANSIT: place the truck where its schedule says, inside the lane
            self.phase = "DRIVING"
            dep = self.pickup_open + timedelta(hours=2)
            arr = self.delivery_open or dep + timedelta(hours=self.total / 50)
            frac = 0.02 if status == "PICKED_UP" else (start - dep) / max(arr - dep, timedelta(hours=1))
            self.done = self.total * min(max(frac + rng.uniform(-0.05, 0.05), 0.08), 0.9)
            self.hos = rng.uniform(3, 10)
            self.duty_start = start - timedelta(hours=HOS_DRIVE_H - self.hos + rng.uniform(0.5, 1.5))
            self.since_break = rng.uniform(0, 6)
        if "HOS_SHORT" in self.incidents and self.phase == "DRIVING":
            self.hos = self.incidents["HOS_SHORT"].duration_hours

        if "SHIPPER_DELAY" in self.incidents and self.phase in ("WAIT_PICKUP", "LOADING"):
            inc = self.incidents["SHIPPER_DELAY"]
            self.until = max(self.until or self.pickup_open, self.pickup_open) + timedelta(hours=inc.duration_hours)
            if self.phase == "WAIT_PICKUP":
                self.phase = "LOADING_LATER"

    # ------------------------------------------------------------------ helpers

    def _dwell(self, fac: dict) -> timedelta:
        mins = (fac or {}).get("avg_unload_minutes") or 90
        return timedelta(minutes=mins * self.rng.uniform(0.8, 1.2))

    def _drive_left(self, t: datetime) -> float:
        """ELD 'available driving': the tighter of the 11 h driving and 14 h on-duty limits."""
        duty_left = HOS_DUTY_H - (t - self.duty_start).total_seconds() / 3600 if self.duty_start else HOS_DUTY_H
        return max(min(self.hos, duty_left), 0.0)

    def _pos(self) -> tuple[float, float]:
        return geo.interpolate(self.origin, self.dest, self.done / self.total if self.total else 1)

    def _event(self, at: datetime, source: str, text: str, level: str = "info") -> None:
        self.events.append(FeedEvent(at=at, shipment_id=self.sid, source=source, level=level,
                                     text=f"{text} ({geo.near(self._pos())})"))

    def _active(self, kind: str, t: datetime) -> bool:
        inc = self.incidents.get(kind)
        if not inc:
            return False
        begin = self.start + timedelta(hours=inc.after_hours)
        return begin <= t < begin + timedelta(hours=inc.duration_hours)

    def _arrive(self, t: datetime) -> None:
        """Inside or before the booked window: unload in the window. Missed it: wait for the next
        time the receiving dock is open (which can be Monday)."""
        self._event(t, "telematics", "Arrived at receiver")
        if self.delivery_close is None or t <= self.delivery_close:
            opens = max(t, self.delivery_open or t)
        else:
            opens = geo.next_open(self.dest_fac, t)
            self._event(t, "driver", "Missed the delivery window", "alert")
        if opens > t + STEP:
            self.phase, self.until = "WAIT_DELIVERY", opens
            self._event(t, "driver", f"Waiting to unload: receiver takes the truck at "
                                     f"{geo.fmt_local(opens, self.dest_fac.get('timezone'))}", "warning")
        else:
            self.phase, self.until = "UNLOADING", t + self._dwell(self.dest_fac)

    # ------------------------------------------------------------------ main loop

    def drive(self, start: datetime, end: datetime) -> tuple[list[Ping], list[FeedEvent]]:
        pings: list[Ping] = []
        slow_reported = set()
        t = start
        while t <= end:
            # 1. phase changes that happen at t
            if self.phase == "WAIT_PICKUP" and t >= self.pickup_open:
                self.phase, self.until, self.duty_start = "LOADING", t + self._dwell(self.pickup_fac), t
                self._event(t, "telematics", "Checked in at shipper")
            elif self.phase == "LOADING_LATER" and t >= self.pickup_open:
                self.phase, self.duty_start = "LOADING", t
                self._event(t, "telematics", "Checked in at shipper")
            if self.phase == "LOADING" and "SHIPPER_DELAY" in self.incidents and "SHIPPER_DELAY" not in slow_reported \
                    and t >= self.start + timedelta(hours=self.incidents["SHIPPER_DELAY"].after_hours):
                slow_reported.add("SHIPPER_DELAY")
                self._event(t, "driver", INCIDENT_TEXT["SHIPPER_DELAY"][2], "warning")
            if self.phase == "LOADING" and t >= self.until:
                self.phase, self.hos = "DRIVING", self.hos if "HOS_SHORT" in self.incidents else HOS_DRIVE_H
                self.since_break = 0.0   # loading counts as the non-driving break
                self._event(t, "telematics", "Departed shipper, loaded")
            elif self.phase == "STOPPED" and t >= self.until:
                if self.code == "HOS":
                    self.hos, self.since_break, self.duty_start = HOS_DRIVE_H, 0.0, t
                    self._event(t, "telematics", "Driver back on duty after 10 h break")
                elif self.code == "BREAK":
                    self.since_break = 0.0
                else:
                    self._event(t, "driver", "Driver reports repair done; moving again")
                self.phase, self.code, self.until = "DRIVING", None, None
            elif self.phase == "WAIT_DELIVERY" and t >= self.until:
                self.phase, self.until = "UNLOADING", t + self._dwell(self.dest_fac)
                self._event(t, "telematics", "Checked in at receiver, unloading")
            elif self.phase == "UNLOADING" and t >= self.until:
                self.phase = "DELIVERED"
                self._event(t, "telematics", "Unloaded; delivery complete")

            if self.phase == "DRIVING":
                bd = self.incidents.get("BREAKDOWN")
                if bd and not self.broke_down and t >= self.start + timedelta(hours=bd.after_hours):
                    self.broke_down = True
                    self.phase, self.code, self.until = "STOPPED", "AI", t + timedelta(hours=bd.duration_hours)
                    self._event(t, "driver", INCIDENT_TEXT["BREAKDOWN"][2], "alert")
                elif self._drive_left(t) <= 0:
                    self.phase, self.code, self.until = "STOPPED", "HOS", t + timedelta(hours=HOS_BREAK_H)
                    self._event(t, "telematics", "Driver out of hours: 10 h break")
                elif self.since_break >= 8:   # FMCSA: 30-min break after 8 h driving
                    self.phase, self.code, self.until = "STOPPED", "BREAK", t + timedelta(minutes=30)

            # 2. the ping for t
            speed = 0.0
            if self.phase == "DRIVING":
                mult = 1.0
                for kind, factor in (("TRAFFIC", 0.45), ("WEATHER", 0.6)):
                    if self._active(kind, t):
                        mult *= factor
                        if kind not in slow_reported:
                            slow_reported.add(kind)
                            src, _, text = INCIDENT_TEXT[kind]
                            self._event(t, src, text, "warning")
                speed = self.cruise * mult * self.rng.uniform(0.85, 1.08)
            state = {"WAIT_PICKUP": "AT_PICKUP", "LOADING_LATER": "AT_PICKUP", "LOADING": "AT_PICKUP",
                     "DRIVING": "MOVING", "STOPPED": "STOPPED", "WAIT_DELIVERY": "AT_DELIVERY",
                     "UNLOADING": "AT_DELIVERY", "DELIVERED": "DELIVERED"}[self.phase]
            lat, lon = self._pos()
            pings.append(Ping(
                shipment_id=self.sid, at=t, lat=round(lat, 5), lon=round(lon, 5), speed_mph=round(speed, 1),
                state=state, hos_drive_hours_left=round(self._drive_left(t), 2) if self.duty_start or state in ("MOVING", "STOPPED") else None,
                hos_available_at=self.until if self.code == "HOS" else None,
                event_code=self.code if state == "STOPPED" else None,
            ))
            if self.phase == "DELIVERED":
                break

            # 3. move for the next 5 minutes
            if self.phase == "DRIVING":
                hours = STEP.total_seconds() / 3600
                self.done = min(self.total, self.done + speed * hours)
                self.hos -= hours
                self.since_break += hours
                if self.done >= self.total:
                    self._arrive(t + STEP)
            t += STEP
        return pings, self.events
