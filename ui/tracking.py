"""Tab: Live tracking on a SIMULATED GPS/ELD feed. Owner: UI lead.

A sim clock (play / pause / speed / scrub) drives everything: truck positions on the map, the event
stream, the per-load timeline, and the tracking statuses the load watch's LATE_ETA rule uses.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st

from meridian import client, geo, tracking
from meridian.models import TrackingStatus

from . import state

CLOCK_TZ = "America/Chicago"
SPEEDS = {"1 min/s": 1, "5 min/s": 5, "15 min/s": 15, "1 h/s": 60}
STATUS_COLOR = {"ON_TIME": [46, 160, 67], "AT_RISK": [235, 140, 0], "LATE": [214, 39, 40],
                "DELIVERED": [140, 140, 140], "NOT_TRACKED": [140, 140, 140]}
STATUS_LABEL = {"ON_TIME": ":green[On time]", "AT_RISK": ":orange[At risk]", "LATE": ":red[Late]",
                "DELIVERED": ":gray[Delivered]", "NOT_TRACKED": ":gray[Not tracked]"}
STATUS_ORDER = {"LATE": 0, "AT_RISK": 1, "ON_TIME": 2, "DELIVERED": 3, "NOT_TRACKED": 4}
LEVEL_ICON = {"alert": "🔴", "warning": "🟠", "info": "⚪"}
SOURCE_LABEL = {"telematics": "GPS", "driver": "Driver", "copilot": "Copilot"}


# --------------------------------------------------------------------------- data and clock

def _load(source: str) -> tuple[list[dict], dict[str, dict]]:
    if source == "Fake data":   # fake mode has no loads to track: use the committed snapshot
        snap = state.ROOT / "snapshots"
        ships = json.loads((snap / "open_shipments.json").read_text(encoding="utf-8"))
        facs = json.loads((snap / "facilities.json").read_text(encoding="utf-8"))
        return ships, {f["facility_id"]: f for f in facs}
    ships, facs = client.list_shipments(), client.list_facilities()
    if not ships["ok"] or not facs["ok"]:
        raise RuntimeError(ships.get("detail") or facs.get("detail") or "could not read shipments/facilities")
    return ships["data"], {f["facility_id"]: f for f in facs["data"]}


@st.cache_resource(show_spinner="Simulating trucks…")
def _build(source: str, demo_mode: bool, refresh: int) -> tracking.TrackingFeed:
    ships, facs = _load(source)
    return tracking.TrackingFeed(ships, facs)


def get_feed() -> tracking.TrackingFeed | None:
    ss = st.session_state
    ss.setdefault("sim_refresh", 0)
    try:
        return _build(ss.data_source, client.demo_mode(), ss.sim_refresh)
    except Exception as e:  # keep the app alive if the API is down
        st.error(f"Tracking unavailable: {e}")
        return None


def _init(feed: tracking.TrackingFeed) -> None:
    ss = st.session_state
    if ss.get("sim_feed_start") != feed.start:
        ss.sim_feed_start, ss.sim_offset_min, ss.sim_playing = feed.start, 0.0, False
        ss.sim_last_real = time.time()
    ss.setdefault("sim_speed", "15 min/s")
    ss.setdefault("sim_alerts_only", False)


def now(feed: tracking.TrackingFeed) -> datetime:
    return feed.start + timedelta(minutes=st.session_state.get("sim_offset_min", 0.0))


def current_statuses() -> tuple[dict[str, TrackingStatus], datetime, datetime] | None:
    """For the load watch: (tracking statuses at the sim time, sim time, time of the loaded TMS data)."""
    feed = get_feed()
    if feed is None:
        return None
    _init(feed)
    t = now(feed)
    return feed.statuses(t), t, feed.start


def _set_offset(minutes: float, feed: tracking.TrackingFeed) -> None:
    total = (feed.end - feed.start).total_seconds() / 60
    st.session_state.sim_offset_min = min(max(minutes, 0.0), total)


def _advance(feed: tracking.TrackingFeed) -> None:
    ss = st.session_state
    real = time.time()
    if ss.sim_playing:
        _set_offset(ss.sim_offset_min + (real - ss.sim_last_real) * SPEEDS[ss.sim_speed], feed)
        ss.sim_last_real = real
        if now(feed) >= feed.end:
            ss.sim_playing = False
            st.rerun(scope="app")
    ss.sim_last_real = real


# --------------------------------------------------------------------------- pieces

def _controls(feed: tracking.TrackingFeed) -> None:
    ss = st.session_state
    total_h = (feed.end - feed.start).total_seconds() / 3600

    def on_jump(h: float):
        _set_offset(ss.sim_offset_min + h * 60, feed)

    def on_reset():
        ss.sim_offset_min, ss.sim_playing = 0.0, False

    def on_scrub():
        _set_offset(ss.sim_scrub * 60, feed)

    c = st.columns([1.1, 1.3, 0.8, 0.8, 0.9, 4])
    if c[0].button("⏸ Pause" if ss.sim_playing else "▶ Play", type="primary", use_container_width=True):
        ss.sim_playing, ss.sim_last_real = not ss.sim_playing, time.time()
        st.rerun(scope="app")   # the fragment's auto-refresh is set by the full app run
    c[1].selectbox("Speed", list(SPEEDS), key="sim_speed", label_visibility="collapsed")
    c[2].button("+1 h", on_click=on_jump, args=(1,), use_container_width=True)
    c[3].button("+6 h", on_click=on_jump, args=(6,), use_container_width=True)
    c[4].button("↺ Reset", on_click=on_reset, use_container_width=True)
    t = now(feed)
    c[5].markdown(f"**Sim clock:** {geo.fmt_local(t, CLOCK_TZ)} &nbsp;·&nbsp; "
                  f"+{(t - feed.start).total_seconds() / 3600:.1f} h after the data snapshot")
    ss.sim_scrub = round(ss.sim_offset_min / 60, 2)
    st.slider("Scrub sim time (hours after snapshot)", 0.0, total_h, key="sim_scrub", step=0.25,
              on_change=on_scrub, label_visibility="collapsed")


def _metrics(statuses: dict[str, TrackingStatus]) -> None:
    counts = {k: sum(1 for s in statuses.values() if s.status == k) for k in ("ON_TIME", "AT_RISK", "LATE", "DELIVERED")}
    c = st.columns(5)
    c[0].metric("Trucks tracked", len(statuses))
    c[1].metric("On time", counts["ON_TIME"])
    c[2].metric("At risk", counts["AT_RISK"])
    c[3].metric("Late", counts["LATE"])
    c[4].metric("Delivered", counts["DELIVERED"])


def _map(feed: tracking.TrackingFeed, statuses: dict[str, TrackingStatus], selected: str | None) -> None:
    trucks, routes, trails = [], [], []
    for sid, st_ in statuses.items():
        if st_.lat is None:
            continue
        s = feed.shipments[sid]
        origin, dest = feed.sim.routes[sid]
        trucks.append({
            "id": sid, "lon": st_.lon, "lat": st_.lat, "color": STATUS_COLOR[st_.status],
            "radius": 30 if sid == selected else 12,
            "status": st_.status.replace("_", " ").title(), "near": st_.near,
            "eta": geo.fmt_local(st_.eta, st_.delivery_tz), "lane": f"{s.get('shipper_city')} → {s.get('consignee_city')}",
            "late": tracking.describe_lateness(st_),
        })
        if st_.status != "DELIVERED":
            routes.append({"from": [st_.lon, st_.lat], "to": [dest[1], dest[0]],
                           "color": [*STATUS_COLOR[st_.status], 200 if sid == selected else 90],
                           "width": 3 if sid == selected else 1.5})
            trails.append({"from": [origin[1], origin[0]], "to": [st_.lon, st_.lat], "color": [90, 90, 90, 110]})
    layers = [
        pdk.Layer("LineLayer", trails, get_source_position="from", get_target_position="to", get_color="color", get_width=1.5),
        pdk.Layer("LineLayer", routes, get_source_position="to", get_target_position="from", get_color="color", get_width="width"),
        pdk.Layer("ScatterplotLayer", trucks, id="trucks", get_position=["lon", "lat"], get_fill_color="color",
                  get_radius="radius", radius_scale=2500, radius_min_pixels=5, radius_max_pixels=11,
                  stroked=True, get_line_color=[255, 255, 255], line_width_min_pixels=1.5, pickable=True),
    ]
    tooltip = {"html": "<b>{id}</b> · {status}<br/>{lane}<br/>{near}<br/>ETA {eta}<br/>{late}",
               "style": {"fontSize": "12px"}}
    deck = pdk.Deck(layers=layers, initial_view_state=pdk.ViewState(latitude=38.6, longitude=-97.5, zoom=3.3),
                    map_provider="carto", map_style="light", tooltip=tooltip)
    st.pydeck_chart(deck, use_container_width=True, height=430)
    st.caption("Lines: grey = driven so far, coloured = still to go (straight line between cities). "
               "Hover a truck for its ETA.")


def _feed_panel(feed: tracking.TrackingFeed, t: datetime) -> None:
    ss = st.session_state
    st.toggle("Alerts only", key="sim_alerts_only")
    events = feed.events(t, limit=200)
    if ss.sim_alerts_only:
        events = [e for e in events if e.level != "info"]
    with st.container(height=405):
        if not events:
            st.caption("No events yet.")
        for e in events[:80]:
            st.markdown(f"{LEVEL_ICON[e.level]} `{geo.fmt_local(e.at, CLOCK_TZ, with_day=False)}` **{e.shipment_id}** "
                        f"· _{SOURCE_LABEL[e.source]}_ · {e.text}")


def _timeline(feed: tracking.TrackingFeed, statuses: dict[str, TrackingStatus], t: datetime, sid: str) -> None:
    s, st_ = feed.shipments[sid], statuses[sid]
    tz_name = st_.delivery_tz
    p_stop, d_stop = s["stops"][0], s["stops"][-1]
    pw, dw = p_stop.get("scheduled_window") or {}, d_stop.get("scheduled_window") or {}

    def loc(dt: datetime | None) -> str | None:   # naive delivery-local time, so the chart shows local clock times
        return dt.astimezone(geo.tz(tz_name)).replace(tzinfo=None).isoformat() if dt else None

    bars = [
        {"row": "Pickup window", "start": loc(geo.local_to_utc(pw.get("earliest_local"), pw.get("timezone"))),
         "end": loc(geo.local_to_utc(pw.get("latest_local"), pw.get("timezone"))), "kind": "Planned"},
        {"row": "Delivery window", "start": loc(geo.local_to_utc(dw.get("earliest_local"), dw.get("timezone"))),
         "end": loc(st_.window_end), "kind": "Planned"},
    ]
    if d_stop.get("mabd_date"):
        bars.append({"row": "Must arrive by", "start": loc(geo.local_to_utc(f"{d_stop['mabd_date']}T00:00:00", tz_name)),
                     "end": loc(st_.mabd_end), "kind": "MABD"})
    evs = feed.events(feed.end, sid, limit=500)
    departed = next((e.at for e in reversed(evs) if "Departed shipper" in e.text and e.at <= t), None)
    arrived = next((e.at for e in reversed(evs) if "Arrived at receiver" in e.text and e.at <= t), None)
    points = [{"row": "Truck", "at": loc(t), "label": "Now"}]
    if departed:
        points.append({"row": "Truck", "at": loc(departed), "label": "Departed"})
    if arrived:
        points.append({"row": "Truck", "at": loc(arrived), "label": "Arrived"})
    elif st_.eta:
        points.append({"row": "Truck", "at": loc(st_.eta), "label": "Projected arrival"})

    rows = [r for r in ("Pickup window", "Truck", "Delivery window", "Must arrive by")
            if r == "Truck" or any(b["row"] == r for b in bars)]
    y = alt.Y("row:N", scale=alt.Scale(domain=rows), sort=None, title=None, axis=alt.Axis(labelLimit=200))
    bar = alt.Chart(pd.DataFrame(bars)).mark_bar(height=16, opacity=0.75).encode(
        x=alt.X("start:T", title=f"Local time at the receiver ({tz_name})"), x2="end:T", y=y,
        color=alt.Color("kind:N", scale=alt.Scale(domain=["Planned", "MABD"], range=["#4C78A8", "#9D755D"]), legend=None),
        tooltip=["row", "start:T", "end:T"])
    pts_df = pd.DataFrame(points)
    pt = alt.Chart(pts_df).mark_point(filled=True, size=140).encode(
        x="at:T", y=y, shape=alt.Shape("label:N", legend=alt.Legend(title=None, orient="bottom")),
        color=alt.Color("label:N", scale=alt.Scale(domain=["Now", "Departed", "Arrived", "Projected arrival"],
                                                   range=["#9E9E9E", "#4C78A8", "#2E9F43", "#D62728"]), legend=None),
        tooltip=["label", "at:T"])
    now_rule = alt.Chart(pd.DataFrame([{"at": loc(t)}])).mark_rule(strokeDash=[4, 3], color="#9E9E9E").encode(x="at:T")
    chart = (bar + now_rule + pt).resolve_scale(color="independent", shape="independent")
    st.altair_chart(chart.properties(height=60 + 40 * len(rows)), use_container_width=True)

    left, right = st.columns([3, 2])
    with left:
        st.markdown(f"**{STATUS_LABEL[st_.status]}** · {tracking.describe_lateness(st_) or '—'}")
        st.table(pd.DataFrame({"": [
            "Truck", "Position", "Speed", "Miles to go", "Driving hours left (ELD)",
            "Projected arrival" if st_.state not in ("AT_DELIVERY", "DELIVERED") else "Arrived",
            "Window ends", "Must arrive by", "Carrier"],
            "value": [st_.state.replace("_", " ").lower() + (f" ({st_.event_code})" if st_.event_code else "")
                      + (f", {st_.stopped_minutes} min" if st_.stopped_minutes else ""),
                      st_.near, f"{st_.speed_mph:.0f} mph", "—" if st_.miles_remaining is None else f"{st_.miles_remaining:.0f}",
                      "—" if st_.hos_drive_hours_left is None else f"{st_.hos_drive_hours_left:.1f} h",
                      geo.fmt_local(st_.eta, tz_name), geo.fmt_local(st_.window_end, tz_name),
                      geo.fmt_local(st_.mabd_end, tz_name) if st_.mabd_end else "—", s.get("carrier_id") or "—"]}
        ).set_index(""))
        if st_.assumptions:
            st.caption("ETA assumes: " + "; ".join(st_.assumptions) + ".")
    with right:
        st.markdown("**This load's events**")
        with st.container(height=300):
            for e in feed.events(t, sid, limit=30):
                st.markdown(f"{LEVEL_ICON[e.level]} `{geo.fmt_local(e.at, tz_name)}` · _{SOURCE_LABEL[e.source]}_ · {e.text}")


# --------------------------------------------------------------------------- tab

def _live(feed: tracking.TrackingFeed) -> None:
    _advance(feed)
    _controls(feed)
    t = now(feed)
    statuses = feed.statuses(t)
    _metrics(statuses)

    ordered = sorted(statuses, key=lambda k: (STATUS_ORDER[statuses[k].status], -(statuses[k].minutes_late or -10**6)))
    ss = st.session_state
    if ss.get("sim_selected") not in statuses:
        ss.sim_selected = ordered[0] if ordered else None

    left, right = st.columns([3, 2])
    with left:
        _map(feed, statuses, ss.sim_selected)
    with right:
        _feed_panel(feed, t)

    st.subheader("Load timeline")
    st.selectbox("Load", ordered, key="sim_selected",
                 format_func=lambda k: f"{k} · {statuses[k].status.replace('_', ' ').title()} · "
                                       f"{feed.shipments[k].get('shipper_city')} → {feed.shipments[k].get('consignee_city')}"
                                       f" · {tracking.describe_lateness(statuses[k]) or 'delivered'}")
    if ss.sim_selected:
        _timeline(feed, statuses, t, ss.sim_selected)


def render() -> None:
    st.markdown(":orange-background[**SIMULATED**] &nbsp; Truck positions, driver reports and hours of service "
                "are generated from the open loads (the TMS has no GPS). ETAs and alerts are computed from the "
                "pings only, as they would be on a real telematics feed.")
    feed = get_feed()
    if feed is None:
        return
    _init(feed)
    if state.use_fake():
        st.caption("Data source is *Fake data*, so tracking runs on the committed snapshot.")
    if st.button("Rebuild simulation from current data", help="Re-reads the loads (live API or snapshot) and re-runs the sim."):
        st.session_state.sim_refresh += 1
        st.rerun()
    st.fragment(_live, run_every=1.0 if st.session_state.sim_playing else None)(feed)
