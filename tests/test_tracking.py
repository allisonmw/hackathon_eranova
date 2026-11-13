import json
import pathlib
from datetime import date, datetime, timedelta

import pytest

from meridian import geo, risk_rules, sim, tracking
from meridian.geo import UTC
from meridian.models import Ping, TrackingStatus
from meridian.risk_rules import RiskContext

SNAP = pathlib.Path(__file__).resolve().parent.parent / "snapshots"
CHI, DAL = geo.CITIES[("Chicago", "IL")], geo.CITIES[("Dallas", "TX")]

FACILITIES = {
    "CHI": {"facility_id": "CHI", "city": "Chicago", "state": "IL", "timezone": "America/Chicago", "avg_unload_minutes": 60},
    "DAL": {"facility_id": "DAL", "city": "Dallas", "state": "TX", "timezone": "America/Chicago", "avg_unload_minutes": 60,
            "receiving_hours": {d: {"open": "06:00", "close": "14:00"} for d in ("mon", "tue", "wed", "thu", "fri")}},
}


def load(status="IN_TRANSIT", mabd=None, deliver="2026-11-16", **kw):
    return {"shipment_id": "7300001", "status": status, "mode": "TL", "miles": 925, "sell_total": 2400,
            "status_updated_at": "2026-11-13T12:50:00Z", "shipper_city": "Chicago", "consignee_city": "Dallas",
            "stops": [
                {"stop_type": "PICKUP", "facility_id": "CHI",
                 "scheduled_window": {"earliest_local": "2026-11-14T08:00:00", "latest_local": "2026-11-14T14:00:00",
                                      "timezone": "America/Chicago"}},
                {"stop_type": "DELIVERY", "facility_id": "DAL", "mabd_date": mabd,
                 "scheduled_window": {"earliest_local": f"{deliver}T06:00:00", "latest_local": f"{deliver}T12:00:00",
                                      "timezone": "America/Chicago"}},
            ], **kw}


def ping(state="MOVING", at=datetime(2026, 11, 15, 12, 0, tzinfo=UTC), pos=None, **kw):
    lat, lon = pos or geo.interpolate(CHI, DAL, 0.5)
    return Ping(shipment_id="7300001", at=at, lat=lat, lon=lon, state=state, speed_mph=55, **kw)


# --------------------------------------------------------------------------- geo

def test_haversine_chicago_dallas():
    assert 790 < geo.haversine_mi(CHI, DAL) < 810


def test_near_names_the_closest_city():
    assert geo.near(CHI) == "at Chicago, IL"
    assert geo.near((CHI[0] - 0.3, CHI[1])).startswith("near ")


def test_next_open_skips_the_weekend():
    sat_noon = datetime(2026, 11, 14, 18, 0, tzinfo=UTC)        # Saturday 12:00 CST
    assert geo.next_open(FACILITIES["DAL"], sat_noon) == datetime(2026, 11, 16, 12, 0, tzinfo=UTC)  # Mon 06:00 CST
    assert geo.next_open({}, sat_noon) == sat_noon               # no hours on file = always open


def test_local_to_utc_uses_the_facility_zone():
    assert geo.local_to_utc("2026-11-14T08:00:00", "America/Denver") == datetime(2026, 11, 14, 15, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- estimate

def test_drive_time_adds_hos_breaks():
    assert tracking.drive_time(300, 11) == (timedelta(hours=6), 0)
    dur, breaks = tracking.drive_time(600, 2)                    # 12 h of driving, 2 h left
    assert breaks == 1 and dur >= timedelta(hours=22)


def test_moving_truck_with_time_to_spare_is_on_time():
    st = tracking.estimate(load(), FACILITIES, ping(hos_drive_hours_left=11), now=datetime(2026, 11, 15, 12, 0, tzinfo=UTC))
    assert st.status == "ON_TIME" and st.minutes_late < -120
    assert st.miles_remaining == pytest.approx(462, abs=15)       # half of 925 lane miles


def test_breakdown_assumes_repair_time_and_turns_late():
    now = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)                # 02:00 CST, window closes 12:00 CST
    p = ping("STOPPED", at=now, pos=geo.interpolate(CHI, DAL, 0.8), event_code="AI", hos_drive_hours_left=8)
    moving = tracking.estimate(load(), FACILITIES, ping(at=now, pos=geo.interpolate(CHI, DAL, 0.8), hos_drive_hours_left=8), now)
    stopped = tracking.estimate(load(), FACILITIES, p, now, stopped_since=now - timedelta(minutes=30))
    assert stopped.eta - moving.eta == timedelta(hours=tracking.REPAIR_H, minutes=-30)
    assert any("breakdown" in a for a in stopped.assumptions)


def test_late_needs_more_than_the_tolerance_and_mabd_miss_is_late():
    now = datetime(2026, 11, 16, 15, 30, tzinfo=UTC)              # 09:30 CST; window closes 12:00 CST
    near_dal = geo.interpolate(CHI, DAL, 0.9)                     # ~92 mi out ≈ 1.9 h: 11:20 CST, 40 min to spare
    assert tracking.estimate(load(), FACILITIES, ping(at=now, pos=near_dal), now).status == "AT_RISK"
    st = tracking.estimate(load(mabd="2026-11-15"), FACILITIES, ping(at=now, pos=near_dal), now)
    assert st.status == "LATE" and st.minutes_late_mabd > 0
    assert "misses the MABD" in tracking.describe_lateness(st)


def test_no_ping_is_not_tracked():
    st = tracking.estimate(load(), FACILITIES, None, datetime(2026, 11, 15, tzinfo=UTC))
    assert st.status == "NOT_TRACKED"


# --------------------------------------------------------------------------- simulator

def test_sim_is_deterministic_and_seeded():
    a = sim.run([load()], FACILITIES, hours=24)
    b = sim.run([load()], FACILITIES, hours=24)
    c = sim.run([load()], FACILITIES, hours=24, seed=99)
    assert a.pings["7300001"] == b.pings["7300001"]
    assert a.pings["7300001"] != c.pings["7300001"]


def test_sim_skips_loads_without_a_truck():
    r = sim.run([load(status="BOOKED")], FACILITIES, hours=4)
    assert r.pings == {}


def test_breakdown_incident_stops_the_truck_with_code_ai():
    r = sim.run([load()], FACILITIES, hours=10, scenario=[sim.Incident("7300001", "BREAKDOWN", after_hours=1, duration_hours=2)])
    stopped = [p for p in r.pings["7300001"] if p.event_code == "AI"]
    assert stopped and all(p.state == "STOPPED" and p.speed_mph == 0 for p in stopped)
    assert len(stopped) == 2 * 12                                 # 2 h of 5-min pings
    assert any("breakdown" in e.text for e in r.events)


def test_driver_never_drives_without_hours_left():
    r = sim.run([load()], FACILITIES, hours=48, scenario=[sim.Incident("7300001", "HOS_SHORT", duration_hours=0.5)])
    pings = r.pings["7300001"]
    hos = [i for i, p in enumerate(pings) if p.event_code == "HOS"]
    assert hos[:120] == list(range(hos[0], hos[0] + 120))              # the first break is an unbroken 10 h
    assert pings[hos[0] + 120].event_code != "HOS"
    assert pings[hos[0] - 1].hos_drive_hours_left < 0.1               # it stopped because the hours ran out


def test_truck_that_misses_the_window_waits_for_receiving_hours():
    # Window closes Sun 11/15 12:00 CST; a breakdown makes the truck miss it; the dock is closed until Monday.
    r = sim.run([load(status="DISPATCHED", deliver="2026-11-15")], FACILITIES, hours=96,
                scenario=[sim.Incident("7300001", "BREAKDOWN", after_hours=26, duration_hours=3)])
    texts = [e.text for e in r.events]
    assert any("Missed the delivery window" in t for t in texts)
    unload = next(e for e in r.events if "unloading" in e.text)
    assert unload.at == datetime(2026, 11, 16, 12, 0, tzinfo=UTC)  # Mon 06:00 CST


# --------------------------------------------------------------------------- feed + rule on the committed snapshot

@pytest.fixture(scope="module")
def feed():
    ships = json.loads((SNAP / "open_shipments.json").read_text(encoding="utf-8"))
    facs = {f["facility_id"]: f for f in json.loads((SNAP / "facilities.json").read_text(encoding="utf-8"))}
    return tracking.TrackingFeed(ships, facs)


def test_feed_tracks_every_truck_and_scripted_breakdown_turns_late(feed):
    assert len(feed.tracked_ids) == 27 and not feed.sim.untracked
    t = feed.start + timedelta(hours=34)                          # after the 7300159 breakdown
    st = feed.status("7300159", t)
    assert st.status == "LATE" and st.minutes_late > 120         # MABD is end of day: arrival still makes it
    texts = [e.text for e in feed.events(t, "7300159")]
    assert any(x.startswith("Running late") for x in texts) and any("breakdown" in x for x in texts)


def test_everything_delivered_by_the_end(feed):
    assert {s.status for s in feed.statuses(feed.end).values()} == {"DELIVERED"}


def test_late_eta_rule_from_feed_statuses(feed):
    t = feed.start + timedelta(hours=34)
    items = risk_rules.late_eta(RiskContext(today=date(2026, 11, 14), tracking=feed.statuses(t)))
    by_id = {it.record_id: it for it in items}
    assert by_id["7300159"].severity == "HIGH" and "past the delivery window" in by_id["7300159"].title
    assert by_id["7300159"].evidence["data_source"] == "simulated GPS/ELD feed"
    json.dumps([it.evidence for it in items])


def test_late_eta_rule_ignores_on_time_and_delivered():
    base = dict(shipment_id="1", at=datetime(2026, 11, 14, tzinfo=UTC), state="MOVING", eta=datetime(2026, 11, 14, tzinfo=UTC))
    tr = {"1": TrackingStatus(status="ON_TIME", minutes_late=-300, **base),
          "2": TrackingStatus(**{**base, "shipment_id": "2", "state": "DELIVERED"}, status="DELIVERED")}
    assert risk_rules.late_eta(RiskContext(today=date(2026, 11, 14), tracking=tr)) == []


def test_truck_that_arrived_after_the_window_is_high():
    at = datetime(2026, 11, 14, 17, 0, tzinfo=UTC)
    st = TrackingStatus(shipment_id="1", at=at, state="AT_DELIVERY", status="LATE", eta=at, minutes_late=55,
                        window_end=at - timedelta(minutes=55), delivery_tz="America/Denver")
    item = risk_rules.late_eta(RiskContext(today=date(2026, 11, 14), tracking={"1": st}))[0]
    assert item.severity == "HIGH" and item.title.startswith("Arrived 0.9 h after")
    assert "Rebook" in item.suggested_action
