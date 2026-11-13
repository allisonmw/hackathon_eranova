import json
import pathlib
from datetime import date, datetime, timedelta

import pytest

from meridian import geo, risk_rules, tracking
from meridian.geo import UTC
from meridian.models import TrackingStatus
from meridian.risk_rules import RiskContext

SNAP = pathlib.Path(__file__).resolve().parent.parent / "snapshots"
CHI, DAL = geo.CITIES[("Chicago", "IL")], geo.CITIES[("Dallas", "TX")]

FACILITIES = {
    "CHI": {"facility_id": "CHI", "city": "Chicago", "state": "IL", "timezone": "America/Chicago"},
    "DAL": {"facility_id": "DAL", "city": "Dallas", "state": "TX", "timezone": "America/Chicago",
            "receiving_hours": {d: {"open": "06:00", "close": "14:00"} for d in ("mon", "tue", "wed", "thu", "fri")}},
}


def load(status="IN_TRANSIT", mabd=None, deliver="2026-11-16", **kw):
    """Chicago → Dallas, 925 lane miles, pickup Sat 11/14 08:00 CST, delivery window 06:00–12:00 CST."""
    return {"shipment_id": "7300001", "status": status, "mode": "TL", "miles": 925, "sell_total": 2400,
            "status_updated_at": "2026-11-14T15:00:00Z", "shipper_city": "Chicago", "consignee_city": "Dallas",
            "stops": [
                {"stop_type": "PICKUP", "facility_id": "CHI",
                 "scheduled_window": {"earliest_local": "2026-11-14T08:00:00", "latest_local": "2026-11-14T14:00:00",
                                      "timezone": "America/Chicago"}},
                {"stop_type": "DELIVERY", "facility_id": "DAL", "mabd_date": mabd,
                 "scheduled_window": {"earliest_local": f"{deliver}T06:00:00", "latest_local": f"{deliver}T12:00:00",
                                      "timezone": "America/Chicago"}},
            ], **kw}


def status_change(to: str, at: str, frm: str = "X") -> dict:
    return {"event_type": "STATUS_CHANGE", "event_at": at, "note": f"Status {frm} → {to}"}


def call(at: str, city: str, state: str, eta: str | None = None, note: str = "", reason: str | None = None) -> dict:
    return {"event_type": "CHECK_CALL", "event_at": at, "city": city, "state": state, "eta": eta,
            "note": note, "reason_code": reason}


DEPARTED = status_change("PICKED_UP", "2026-11-14T15:00:00Z")           # Sat 09:00 CST


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# --------------------------------------------------------------------------- geo

def test_haversine_chicago_dallas():
    assert 790 < geo.haversine_mi(CHI, DAL) < 810


def test_near_names_the_closest_city():
    assert geo.near(CHI) == "at Chicago, IL"
    assert geo.near((CHI[0] - 0.3, CHI[1])).startswith("near ")


def test_place_falls_back_to_the_state_centre():
    assert geo.place("Joplin", "MO") == (geo.CITIES[("Joplin", "MO")], False)
    coords, approx = geo.place("Nowhereville", "KS")
    assert approx and coords == geo.STATE_CENTERS["KS"]


def test_next_open_skips_the_weekend():
    sat_noon = datetime(2026, 11, 14, 18, 0, tzinfo=UTC)        # Saturday 12:00 CST
    assert geo.next_open(FACILITIES["DAL"], sat_noon) == datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    assert geo.next_open({}, sat_noon) == sat_noon


def test_local_to_utc_uses_the_facility_zone():
    assert geo.local_to_utc("2026-11-14T08:00:00", "America/Denver") == datetime(2026, 11, 14, 15, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- hours-of-service maths

def test_drive_time_adds_hos_breaks():
    assert tracking.drive_time(300, 11) == (timedelta(hours=6), 0)
    dur, breaks = tracking.drive_time(600, 2)                    # 12 h of driving, 2 h left
    assert breaks == 1 and dur >= timedelta(hours=22)


def test_drive_time_grace_avoids_a_break_for_a_few_minutes_over():
    assert tracking.drive_time(560, 11)[1] == 0                  # 11.2 h ≈ one shift
    assert tracking.drive_time(600, 11)[1] == 1                  # 12 h needs a break


def test_hours_driven_resets_after_a_long_idle_gap():
    t = datetime(2026, 11, 14, 12, tzinfo=UTC)
    a, b, c = (41.0, -90.0), (40.0, -90.0), (39.0, -90.0)        # ~69 mi apart
    steady = [(t, a), (t + timedelta(hours=1.5), b), (t + timedelta(hours=3), c)]
    rested = [(t, a), (t + timedelta(hours=1.5), b), (t + timedelta(hours=13), c)]   # 10 h idle before c
    assert tracking.hours_driven_since_rest(steady, 1.0) == pytest.approx(2.76, abs=0.1)
    assert tracking.hours_driven_since_rest(rested, 1.0) == pytest.approx(1.38, abs=0.1)


# --------------------------------------------------------------------------- estimate

def test_close_to_the_receiver_with_hours_is_on_time():
    evs = [DEPARTED, call("2026-11-15T20:00:00Z", "Waco", "TX", note="7 h left")]
    st = tracking.estimate(load(), FACILITIES, evs, utc("2026-11-15T21:00:00Z"))
    assert st.status == "ON_TIME" and st.position.city == "Waco"
    assert any("7 h of driving left" in a for a in st.assumptions)


def test_driver_eta_that_needs_more_hours_than_legal_is_flagged():
    # Springfield, MO at Sun 06:00 CST, ~420 road miles left, driver says 2 h left but claims 15:00 CST arrival
    evs = [DEPARTED, call("2026-11-15T12:00:00Z", "Springfield", "MO", eta="2026-11-15T21:00:00Z", note="2 h left")]
    st = tracking.estimate(load(), FACILITIES, evs, utc("2026-11-15T12:30:00Z"))
    assert st.driver_eta == utc("2026-11-15T21:00:00Z")
    assert st.eta_gap_minutes > 8 * 60                             # needs a 10 h break the driver didn't plan
    assert any("earlier than possible at legal driving hours" in x for x in st.exceptions)


def test_reported_breakdown_adds_repair_time_and_an_exception():
    evs = [DEPARTED, call("2026-11-15T12:00:00Z", "Joplin", "MO", note="9 h left")]
    broke = [DEPARTED, call("2026-11-15T12:00:00Z", "Joplin", "MO", note="9 h left", reason="AI")]
    a = tracking.estimate(load(), FACILITIES, evs, utc("2026-11-15T12:30:00Z"))
    b = tracking.estimate(load(), FACILITIES, broke, utc("2026-11-15T12:30:00Z"))
    assert b.eta - a.eta == timedelta(hours=tracking.REPAIR_H)
    assert any("mechanical breakdown (AI)" in x for x in b.exceptions)


def test_stale_report_and_eta_passed_are_exceptions():
    evs = [DEPARTED, call("2026-11-15T20:00:00Z", "Waco", "TX", eta="2026-11-15T23:00:00Z")]
    st = tracking.estimate(load(), FACILITIES, evs, utc("2026-11-16T09:00:00Z"))   # 13 h later, no arrival
    texts = " | ".join(st.exceptions)
    assert "No location update for 13 h" in texts and "No arrival recorded" in texts and "Driver's ETA passed" in texts
    assert st.status == "AT_RISK"                                  # window (12:00 CST Mon) still open


def test_projected_past_the_mabd_is_late():
    evs = [DEPARTED, call("2026-11-15T12:00:00Z", "Springfield", "MO", note="1 h left")]
    st = tracking.estimate(load(mabd="2026-11-15", deliver="2026-11-15"), FACILITIES, evs, utc("2026-11-15T12:30:00Z"))
    assert st.status == "LATE" and st.minutes_late_mabd > 0
    assert "misses the MABD" in tracking.describe_lateness(st)


def test_arrival_after_the_window_is_late_and_on_time_is_arrived():
    late = [status_change("AT_DELIVERY", "2026-11-16T19:00:00Z")]  # 13:00 CST, window closed 12:00
    ok = [status_change("AT_DELIVERY", "2026-11-16T14:00:00Z")]    # 08:00 CST
    a = tracking.estimate(load(status="AT_DELIVERY"), FACILITIES, late, utc("2026-11-16T20:00:00Z"))
    b = tracking.estimate(load(status="AT_DELIVERY"), FACILITIES, ok, utc("2026-11-16T20:00:00Z"))
    assert a.status == "LATE" and a.exceptions[-1].startswith("Arrived 60 min after")
    assert b.status == "ARRIVED" and b.position.city == "Dallas"


def test_dispatched_load_projects_from_the_pickup_window():
    st = tracking.estimate(load(status="DISPATCHED"), FACILITIES, [], utc("2026-11-13T18:00:00Z"))
    # 08:00 CST + 1.5 h loading + 18.5 h driving (11 + 7.5) + 1 × 10 h break + one 30-min break (first shift > 8 h)
    assert st.eta == utc("2026-11-14T14:00:00Z") + timedelta(hours=1.5 + 18.5 + 10 + 0.5)
    assert st.status == "ON_TIME"


def test_long_wait_at_the_shipper_uses_up_the_driver_day():
    evs = [status_change("AT_PICKUP", "2026-11-14T14:00:00Z")]     # at the shipper since 08:00 CST
    st = tracking.estimate(load(status="AT_PICKUP"), FACILITIES, evs, utc("2026-11-14T20:00:00Z"))  # 6 h later
    assert any("At the shipper for 6.0 h" in x for x in st.exceptions)
    assert any("~7.0 h of driving left today (14 h rule)" in a for a in st.assumptions)


def test_same_time_calls_take_the_one_furthest_along():
    evs = [DEPARTED, call("2026-11-15T12:00:00Z", "St. Louis", "MO"), call("2026-11-15T12:00:00Z", "Joplin", "MO")]
    st = tracking.estimate(load(), FACILITIES, list(reversed(evs)), utc("2026-11-15T12:30:00Z"))
    assert st.position.city == "Joplin"


def test_unknown_city_is_placed_approximately():
    evs = [DEPARTED, call("2026-11-15T12:00:00Z", "Smallville", "OK")]
    st = tracking.estimate(load(), FACILITIES, evs, utc("2026-11-15T12:30:00Z"))
    assert st.position.approximate and (st.position.lat, st.position.lon) == geo.STATE_CENTERS["OK"]


def test_loads_without_a_truck_are_not_tracked():
    assert tracking.estimate(load(status="BOOKED"), FACILITIES, [], utc("2026-11-14T00:00:00Z")).status == "NOT_TRACKED"


# --------------------------------------------------------------------------- on the committed snapshot

@pytest.fixture(scope="module")
def tracker():
    read = lambda n: json.loads((SNAP / f"{n}.json").read_text(encoding="utf-8"))  # noqa: E731
    facs = {f["facility_id"]: f for f in read("facilities")}
    return tracking.Tracker(read("open_shipments"), facs, read("shipment_events"))


def test_snapshot_tracks_every_truck_as_of_the_latest_activity(tracker):
    assert tracker.as_of == datetime(2026, 11, 13, 13, 18, 36, tzinfo=UTC)
    assert len(tracker.statuses) == 27
    assert all(s.position is not None for s in tracker.statuses.values())


def test_snapshot_flags_the_driver_eta_that_needs_a_break(tracker):
    st = tracker.statuses["7300145"]                               # last call Hays, KS; driver says 06:55 MST
    assert st.status == "LATE" and st.position.city == "Hays"
    assert any("earlier than possible at legal driving hours" in x for x in st.exceptions)


def test_snapshot_event_stream_mixes_tms_events_and_exceptions(tracker):
    evs = tracker.events("7300117")
    assert {e.source for e in evs} >= {"check_call", "status", "copilot"}
    assert evs == sorted(evs, key=lambda e: (e.at, e.source == "copilot"), reverse=True)


def test_late_eta_rule_on_snapshot(tracker):
    items = {it.record_id: it for it in risk_rules.late_eta(RiskContext(today=date(2026, 11, 13), tracking=tracker.statuses))}
    assert items["7300117"].severity == "HIGH"                     # no arrival a day after its ETA, misses MABD
    assert items["7300117"].evidence["data_source"] == "TMS check calls and status changes"
    assert "7300166" not in items                                  # dispatched, plenty of slack
    json.dumps([it.evidence for it in items.values()])


def test_late_eta_rule_arrived_late_is_high_with_rebook():
    at = datetime(2026, 11, 14, 17, 0, tzinfo=UTC)
    st = TrackingStatus(shipment_id="1", as_of=at, stage="AT_DELIVERY", status="LATE", eta=at, arrived_at=at,
                        minutes_late=55, window_end=at - timedelta(minutes=55), delivery_tz="America/Denver")
    item = risk_rules.late_eta(RiskContext(today=date(2026, 11, 14), tracking={"1": st}))[0]
    assert item.severity == "HIGH" and item.title.startswith("Arrived 0.9 h after")
    assert "Rebook" in item.suggested_action


def test_late_eta_rule_ignores_on_time_and_arrived():
    at = datetime(2026, 11, 14, tzinfo=UTC)
    tr = {"1": TrackingStatus(shipment_id="1", as_of=at, stage="IN_TRANSIT", status="ON_TIME", minutes_late=-300),
          "2": TrackingStatus(shipment_id="2", as_of=at, stage="AT_DELIVERY", status="ARRIVED")}
    assert risk_rules.late_eta(RiskContext(today=date(2026, 11, 14), tracking=tr)) == []


def test_driver_eta_comes_from_the_latest_call_only():
    evs = [DEPARTED, call("2026-11-15T06:00:00Z", "St. Louis", "MO", eta="2026-11-15T20:00:00Z"),
           call("2026-11-15T12:00:00Z", "Joplin", "MO")]                 # newer call gave no ETA
    st = tracking.estimate(load(), FACILITIES, evs, utc("2026-11-15T12:30:00Z"))
    assert st.driver_eta is None and st.eta_gap_minutes is None
