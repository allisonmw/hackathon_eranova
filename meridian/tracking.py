"""Tracking from TMS data: where each truck is, our ETA at legal driving hours, and what needs attention.
Owner: agent & evals lead.

Inputs, per load (all from the TMS, nothing simulated):
  - the shipment: status, stops, scheduled windows, MABD, lane miles
  - its facilities: city/state (placed on the map via meridian.geo), time zone
  - its activity log: check calls (city, state, the driver's reported ETA, reason code, note) and
    status changes (when it reached the shipper, was loaded, arrived at the receiver)
  - an "as of" time

What it calculates:
  - the latest known position and the trail of check calls
  - our ETA from the latest position: distance left at ASSUMED_MPH, plus the federal hours-of-service
    breaks (30 min after 8 h driving; 10 h off after 11 h driving or 14 h on duty)
  - lateness vs the delivery window and the MABD, and a status: on time / at risk / late / arrived
  - exceptions: driver's ETA not possible at legal hours, no location update for hours, reported
    breakdown or weather, ETA passed with no arrival recorded

Every planning assumption is listed on the status so the UI can show it next to the ETA.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from . import geo
from .geo import UTC
from .models import FeedEvent, PositionReport, TrackingStatus

ASSUMED_MPH = 50.0            # planning average for a moving truck
HOS_DRIVE_H, HOS_BREAK_H, HOS_DUTY_H = 11.0, 10.0, 14.0
HOS_CYCLE_H = HOS_DRIVE_H + HOS_BREAK_H
SHORT_BREAK_AFTER_H = 8.0     # 30-min break after 8 h driving
HOS_GRACE_H = 0.5             # overshooting the driver's hours by less than this (at a rough 50 mph): no 10 h break
REST_INFERENCE_H = 8.0        # an idle gap this long between reports counts as rest (sleeper-berth split allows 8 + 2)
LOADING_H = 1.5               # loading once the pickup window opens
LOADING_LEFT_H = 1.0          # truck at the shipper, loading under way
REPAIR_H = 3.0                # breakdown reported with no repair ETA
AT_RISK_SLACK_MIN = 120       # less slack than this = at risk
LATE_TOLERANCE_MIN = 30       # projections hours out aren't that precise
STALE_REPORT_H = 6.0          # in transit with no location update for this long = at risk
DRIVER_ETA_GAP_MIN = 60       # driver's ETA this much earlier than legal-hours ETA = flag it

TRACKED_STAGES = {"DISPATCHED", "AT_PICKUP", "PICKED_UP", "IN_TRANSIT", "AT_DELIVERY"}
REASON_TEXT = {"AI": "mechanical breakdown", "MECHANICAL": "mechanical breakdown", "AO": "weather",
               "WEATHER": "weather", "B1": "consignee closed", "T3": "tractor or trailer not available",
               "D1": "carrier dispatch error"}
BREAKDOWN_CODES = {"AI", "MECHANICAL"}
HOURS_LEFT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hours?)\b[^.\n]{0,25}?\bleft", re.I)
STATUS_TO_RE = re.compile(r"→\s*([A-Z_]+)")
MILESTONE_FOR = {"AT_PICKUP": "AT_SHIPPER", "PICKED_UP": "LOADED", "IN_TRANSIT": "IN_TRANSIT",
                 "AT_DELIVERY": "AT_CONSIGNEE"}


# --------------------------------------------------------------------------- inputs

def lane(shipment: dict, facilities: dict[str, dict]) -> dict:
    stops = shipment.get("stops") or []
    p, d = (stops[0], stops[-1]) if len(stops) >= 2 else ({}, {})
    pw, dw = p.get("scheduled_window") or {}, d.get("scheduled_window") or {}
    p_fac = facilities.get(p.get("facility_id") or "", {})
    d_fac = facilities.get(d.get("facility_id") or "", {})
    tz_name = dw.get("timezone") or d_fac.get("timezone")
    origin = geo.stop_coords(p, facilities) if p else None
    dest = geo.stop_coords(d, facilities) if d else None
    crow = geo.haversine_mi(origin, dest) if origin and dest else 0
    miles = float(shipment.get("miles") or 0)
    return {
        "origin": origin, "dest": dest, "miles": miles or crow * 1.2,
        "road_factor": (miles / crow) if miles and crow > 1 else 1.2,
        "pickup_open": geo.local_to_utc(pw.get("earliest_local"), pw.get("timezone")),
        "pickup_tz": pw.get("timezone") or p_fac.get("timezone"),
        "window_start": geo.local_to_utc(dw.get("earliest_local"), tz_name),
        "window_end": geo.local_to_utc(dw.get("latest_local"), tz_name),
        "mabd_end": geo.end_of_day_utc(d.get("mabd_date"), tz_name),
        "tz": tz_name,
        "pickup_city": (p_fac.get("city"), p_fac.get("state")), "dest_city": (d_fac.get("city"), d_fac.get("state")),
    }


def status_times(shipment: dict, events: list[dict]) -> dict[str, datetime]:
    """When the load reached each status, from status-change events (milestones as a fallback)."""
    times: dict[str, datetime] = {}
    for e in events:
        if e.get("event_type") == "STATUS_CHANGE" and (m := STATUS_TO_RE.search(e.get("note") or "")):
            at = geo.parse_utc(e.get("event_at"))
            if at and (m.group(1) not in times or at > times[m.group(1)]):
                times[m.group(1)] = at
    by_code = {m.get("milestone_code"): m for m in shipment.get("milestones") or []}
    for status, code in MILESTONE_FOR.items():
        m = by_code.get(code) or {}
        if status not in times and m.get("complete") and (at := geo.parse_utc(m.get("event_at"))):
            times[status] = at
    return times


def check_calls(events: list[dict], dest: tuple[float, float] | None) -> list[PositionReport]:
    """Check calls as position reports, oldest first. Calls logged at the same moment are ordered
    by distance to the destination, so the latest is the one furthest along."""
    out = []
    for e in events:
        if e.get("event_type") != "CHECK_CALL" or not (at := geo.parse_utc(e.get("event_at"))):
            continue
        coords, approx = geo.place(e.get("city"), e.get("state"))
        note = e.get("note") or ""
        m = HOURS_LEFT_RE.search(note)
        out.append(PositionReport(
            at=at, city=e.get("city"), state=e.get("state"), lat=coords[0] if coords else None,
            lon=coords[1] if coords else None, approximate=approx and coords is not None, source="check_call",
            reported_eta=geo.parse_utc(e.get("eta")), reason_code=e.get("reason_code"),
            hours_left=float(m.group(1)) if m else None, note=note))
    far = lambda r: -geo.haversine_mi((r.lat, r.lon), dest) if dest and r.lat is not None else 0.0  # noqa: E731
    return sorted(out, key=lambda r: (r.at, far(r)))


def latest_activity(shipments: list[dict], events_by_id: dict[str, list[dict]]) -> datetime:
    """The most recent moment the TMS data describes (latest status update or event)."""
    times = [geo.parse_utc(s.get("status_updated_at")) for s in shipments]
    times += [geo.parse_utc(e.get("event_at")) for evs in events_by_id.values() for e in evs]
    times = [t for t in times if t]
    return max(times) if times else datetime.now(UTC)


# --------------------------------------------------------------------------- the estimate

def drive_time(miles: float, hos_left: float | None) -> tuple[timedelta, int]:
    """Time to cover `miles` at ASSUMED_MPH with hours-of-service breaks: a 10 h break each time the
    driver's hours run out, and 30 min per 8 h driving stretch. Returns (duration, number of 10 h breaks)."""
    hours = miles / ASSUMED_MPH
    cap = HOS_DRIVE_H if hos_left is None else max(hos_left, 0.0)
    remaining, breaks, short = hours, 0, 0
    while remaining > 1e-9:
        if remaining - cap <= HOS_GRACE_H:     # close enough: finishes on this shift
            cap = max(cap, remaining)
        if cap <= 0:
            breaks, cap = breaks + 1, HOS_DRIVE_H
            continue
        drive = min(remaining, cap)
        short += drive > SHORT_BREAK_AFTER_H
        remaining -= drive
        if remaining > 1e-9:
            breaks, cap = breaks + 1, HOS_DRIVE_H
    return timedelta(hours=hours + breaks * HOS_BREAK_H + short * 0.5), breaks


def _hours(td: timedelta) -> float:
    return td.total_seconds() / 3600


def hours_driven_since_rest(points: list[tuple[datetime, tuple[float, float]]], road_factor: float) -> float:
    """Driving hours on the driver's current shift, inferred from timed positions (departure, check calls).
    For each stretch: driving needed = road miles / ASSUMED_MPH; if the rest of the gap is long enough to
    rest (REST_INFERENCE_H), assume the driver rested and the clock restarted. Only the first point: nothing driven yet."""
    driven = 0.0
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        seg = geo.haversine_mi(p0, p1) * road_factor / ASSUMED_MPH
        idle = _hours(t1 - t0) - seg
        driven = seg if idle >= REST_INFERENCE_H else driven + seg
        if driven > HOS_DRIVE_H:          # the reports imply more driving than one shift allows:
            driven -= HOS_DRIVE_H         # a break must have happened somewhere in between
    return min(driven, HOS_DRIVE_H)


def _pos(source: str, at: datetime, city_state: tuple, coords) -> PositionReport:
    return PositionReport(at=at, city=city_state[0], state=city_state[1], source=source,
                          lat=coords[0] if coords else None, lon=coords[1] if coords else None)


def _dur(minutes: float) -> str:
    m = abs(minutes)
    return f"{m / 60:.1f} h" if m >= 90 else f"{m:.0f} min"


def estimate(shipment: dict, facilities: dict[str, dict], events: list[dict], as_of: datetime) -> TrackingStatus:
    sid, stage = str(shipment["shipment_id"]), shipment.get("status") or ""
    ln = lane(shipment, facilities)
    tzn = ln["tz"]
    base = dict(shipment_id=sid, as_of=as_of, stage=stage, window_end=ln["window_end"],
                mabd_end=ln["mabd_end"], delivery_tz=tzn)
    if stage not in TRACKED_STAGES:
        return TrackingStatus(status="NOT_TRACKED", **base)

    times = status_times(shipment, events)
    calls = [c for c in check_calls(events, ln["dest"]) if c.at <= as_of]
    departed = times.get("PICKED_UP") or times.get("IN_TRANSIT")
    arrived = times.get("AT_DELIVERY") if stage == "AT_DELIVERY" else None
    driver_eta = calls[-1].reported_eta if calls else None   # only the latest call's ETA counts
    pickup = _pos("pickup", times.get("AT_PICKUP") or departed or ln["pickup_open"] or as_of, ln["pickup_city"], ln["origin"])
    trail = ([pickup] if stage != "DISPATCHED" else []) + [c for c in calls if not departed or c.at >= departed]
    assumptions: list[str] = []
    exceptions: list[str] = []
    position, eta, miles_left = None, None, None

    if stage == "AT_DELIVERY":
        arrived = arrived or geo.parse_utc(shipment.get("status_updated_at")) or as_of
        position = _pos("delivery", arrived, ln["dest_city"], ln["dest"])
        trail.append(position)
        eta, miles_left = arrived, 0.0

    elif stage in ("DISPATCHED", "AT_PICKUP"):
        position, miles_left = pickup, ln["miles"]
        at_shipper = times.get("AT_PICKUP")
        if stage == "AT_PICKUP" and at_shipper and (not ln["pickup_open"] or as_of >= ln["pickup_open"]):
            start = as_of + timedelta(hours=LOADING_LEFT_H)
            on_duty = _hours(as_of - at_shipper)
            hos = max(min(HOS_DRIVE_H, HOS_DUTY_H - on_duty - LOADING_LEFT_H), 0.0)
            assumptions.append(f"loading finishes in ~{LOADING_LEFT_H:g} h; driver on duty since arriving at the "
                               f"shipper {geo.fmt_local(at_shipper, ln['pickup_tz'])}, so ~{hos:.1f} h of driving "
                               f"left today (14 h rule)")
            if on_duty > 2:
                exceptions.append(f"At the shipper for {_dur(on_duty * 60)} — still not loaded")
        else:
            opens = max(as_of, ln["pickup_open"] or as_of)
            start, hos = opens + timedelta(hours=LOADING_H), HOS_DRIVE_H
            assumptions.append(f"loads for {LOADING_H:g} h once the pickup window opens "
                               f"({geo.fmt_local(opens, ln['pickup_tz'])}), driver starts with a full 11 h")
        drive, breaks = drive_time(miles_left, hos)
        eta = start + drive
        assumptions.append(f"{ASSUMED_MPH:g} mph over {miles_left:.0f} mi" + (f", {breaks} × 10 h break" if breaks else ""))

    else:  # PICKED_UP / IN_TRANSIT
        last = next((c for c in reversed(calls) if c.lat is not None and (not departed or c.at >= departed)), None)
        if last:
            position = last
            miles_left = geo.haversine_mi((last.lat, last.lon), ln["dest"]) * ln["road_factor"] if ln["dest"] else ln["miles"]
            start = last.at
            if last.hours_left is not None:
                hos = last.hours_left
                assumptions.append(f"{hos:g} h of driving left, as reported on the {geo.fmt_local(last.at, tzn)} call")
            else:
                start_pt = (departed, ln["origin"]) if departed and ln["origin"] else None
                pts = ([start_pt] if start_pt else []) + [(c.at, (c.lat, c.lon)) for c in calls
                                                          if c.lat is not None and (not departed or c.at >= departed)]
                driven = hours_driven_since_rest(pts, ln["road_factor"])
                hos = max(HOS_DRIVE_H - driven, 0.0)
                assumptions.append(f"driver's hours not reported: ~{driven:.1f} h driven since the last rest "
                                   f"(inferred from the check-call trail{' and departure time' if start_pt else ''}), "
                                   f"so ~{hos:.1f} h left")
            if (last.reason_code or "").upper() in BREAKDOWN_CODES:
                start = last.at + timedelta(hours=REPAIR_H)
                assumptions.append(f"breakdown repaired ~{REPAIR_H:g} h after the call (no repair time given)")
        else:
            position = _pos("pickup", departed or as_of, ln["pickup_city"], ln["origin"])
            miles_left, start, hos = ln["miles"], departed or as_of, HOS_DRIVE_H
            assumptions.append("no check call since departure: projected from the shipper with a full 11 h")
        drive, breaks = drive_time(miles_left, hos)
        eta = start + drive
        assumptions.append(f"{ASSUMED_MPH:g} mph over {miles_left:.0f} mi from {position.city}, {position.state}"
                           + (f", {breaks} × 10 h break" if breaks else ""))

        since = _hours(as_of - position.at)
        if since >= STALE_REPORT_H:
            exceptions.append(f"No location update for {since:.0f} h (last: {position.city}, {position.state}, "
                              f"{geo.fmt_local(position.at, tzn)})")
        if eta < as_of:
            exceptions.append(f"No arrival recorded, but our ETA ({geo.fmt_local(eta, tzn)}) passed "
                              f"{_dur(_hours(as_of - eta) * 60)} ago")
        if last and last.reason_code:
            code = last.reason_code.upper()
            exceptions.append(f"Driver reported {REASON_TEXT.get(code, 'a delay')} ({code}) at "
                              f"{last.city}, {last.state}")

    # the driver's ETA vs ours
    gap = None
    if driver_eta and eta and stage != "AT_DELIVERY":
        gap = round(_hours(eta - driver_eta) * 60)
        if gap >= DRIVER_ETA_GAP_MIN:
            exceptions.append(f"Driver's ETA ({geo.fmt_local(driver_eta, tzn)}) is {_dur(gap)} earlier than "
                              f"possible at legal driving hours ({geo.fmt_local(eta, tzn)})")
        if driver_eta < as_of and stage in ("PICKED_UP", "IN_TRANSIT"):
            exceptions.append(f"Driver's ETA passed {_dur(_hours(as_of - driver_eta) * 60)} ago with no arrival recorded")

    # lateness and status (a truck that hasn't arrived can't arrive before now)
    effective = eta if stage == "AT_DELIVERY" else max(eta, as_of) if eta else None
    late = round(_hours(effective - ln["window_end"]) * 60) if effective and ln["window_end"] else None
    late_mabd = round(_hours(effective - ln["mabd_end"]) * 60) if effective and ln["mabd_end"] else None
    if stage == "AT_DELIVERY":
        status = "LATE" if late is not None and late > 0 else "ARRIVED"
        if status == "LATE":
            exceptions.append(f"Arrived {_dur(late)} after the delivery window closed")
    elif (late is not None and late > LATE_TOLERANCE_MIN) or (late_mabd is not None and late_mabd > 0):
        status = "LATE"
    elif (late is not None and late > -AT_RISK_SLACK_MIN) or exceptions:
        status = "AT_RISK"
    else:
        status = "ON_TIME"

    return TrackingStatus(
        **base, status=status, position=position, trail=trail,
        hours_since_report=round(_hours(as_of - position.at), 1) if position else None,
        miles_remaining=round(miles_left, 1) if miles_left is not None else None,
        eta=eta, driver_eta=driver_eta, eta_gap_minutes=gap, departed_at=departed, arrived_at=arrived,
        minutes_late=late, minutes_late_mabd=late_mabd, exceptions=exceptions, assumptions=assumptions)


def describe_lateness(st: TrackingStatus) -> str:
    """'3.2 h past the window end, misses the MABD' / '45 min to spare'."""
    if st.minutes_late is None:
        return ""
    text = (f"{_dur(st.minutes_late)} past the window end" if st.minutes_late > 0
            else f"{_dur(-st.minutes_late)} to spare")
    if st.minutes_late_mabd is not None and st.minutes_late_mabd > 0:
        text += ", misses the MABD"
    return text


# --------------------------------------------------------------------------- all loads + the event stream

class Tracker:
    """Statuses and the event stream for every open load, as of one time."""

    def __init__(self, shipments: list[dict], facilities: dict[str, dict],
                 events_by_id: dict[str, list[dict]], as_of: datetime | None = None):
        self.shipments = {str(s["shipment_id"]): s for s in shipments}
        self.facilities = facilities
        self.events_by_id = {str(k): v for k, v in events_by_id.items()}
        self.as_of = as_of or latest_activity(shipments, self.events_by_id)
        self.statuses: dict[str, TrackingStatus] = {}
        for sid, s in self.shipments.items():
            if s.get("status") in TRACKED_STAGES:
                self.statuses[sid] = estimate(s, facilities, self.events_by_id.get(sid, []), self.as_of)

    def events(self, shipment_id: str | None = None, limit: int = 200) -> list[FeedEvent]:
        """Check calls, status changes and appointments from the TMS, plus copilot exceptions, newest first."""
        out: list[FeedEvent] = []
        ids = [shipment_id] if shipment_id else list(self.statuses)
        for sid in ids:
            st = self.statuses.get(sid)
            tzn = st.delivery_tz if st else None
            for e in self.events_by_id.get(sid, []):
                at = geo.parse_utc(e.get("event_at"))
                if not at or at > self.as_of:
                    continue
                kind, note = e.get("event_type"), e.get("note") or ""
                dry = " (dry-run, not saved)" if e.get("_dry_run") else ""
                if kind == "CHECK_CALL":
                    eta = geo.parse_utc(e.get("eta"))
                    code = e.get("reason_code")
                    text = (f"Check call: {e.get('city')}, {e.get('state')}"
                            + (f" · driver ETA {geo.fmt_local(eta, tzn)}" if eta else "")
                            + (f" · reason {code}" if code else "") + (f" — {note}" if note else "") + dry)
                    out.append(FeedEvent(at=at, shipment_id=sid, source="check_call",
                                         level="warning" if code else "info", text=text))
                elif kind == "STATUS_CHANGE":
                    out.append(FeedEvent(at=at, shipment_id=sid, source="status", text=note))
                elif kind == "APPOINTMENT":
                    out.append(FeedEvent(at=at, shipment_id=sid, source="appointment", text=note))
            if st:
                for x in st.exceptions:
                    out.append(FeedEvent(at=self.as_of, shipment_id=sid, source="copilot",
                                         level="alert" if st.status == "LATE" else "warning", text=x))
        out.sort(key=lambda e: (e.at, e.source == "copilot"), reverse=True)
        return out[:limit]
