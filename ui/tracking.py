"""Tab: Live tracking from TMS check calls and status changes. Owner: UI lead.

Everything is read from the TMS (live API or snapshot) as of one time: where each truck last
reported, our ETA at legal driving hours next to the driver's ETA, and what needs attention.
Dispatchers can log a check call here; in dry-run it is shown but not saved.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time as dtime, timedelta

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st

from meridian import client, geo, tracking
from meridian.models import TrackingStatus

from . import state

CLOCK_TZ = "America/Chicago"
STATUS_COLOR = {"ON_TIME": [46, 160, 67], "AT_RISK": [235, 140, 0], "LATE": [214, 39, 40],
                "ARRIVED": [120, 120, 120], "NOT_TRACKED": [140, 140, 140]}
STATUS_LABEL = {"ON_TIME": ":green[On time]", "AT_RISK": ":orange[At risk]", "LATE": ":red[Late]",
                "ARRIVED": ":gray[Arrived]", "NOT_TRACKED": ":gray[Not tracked]"}
STATUS_ORDER = {"LATE": 0, "AT_RISK": 1, "ON_TIME": 2, "ARRIVED": 3, "NOT_TRACKED": 4}
SOURCE_ICON = {"check_call": "📞", "status": "🚚", "appointment": "📅", "copilot": "⚠️"}
LEVEL_DOT = {"alert": "🔴", "warning": "🟠", "info": "⚪"}
REASON_CODES = {"": "none", "AI": "AI · mechanical breakdown", "AO": "AO · weather", "B1": "B1 · consignee closed",
                "T3": "T3 · tractor/trailer not available", "D1": "D1 · carrier dispatch error"}


# --------------------------------------------------------------------------- data

def _load(source: str) -> tuple[list[dict], dict[str, dict], dict[str, list[dict]]]:
    if source == "Fake data":   # fake mode has no loads to track: use the committed snapshot
        snap = state.ROOT / "snapshots"
        read = lambda n: json.loads((snap / f"{n}.json").read_text(encoding="utf-8"))  # noqa: E731
        return read("open_shipments"), {f["facility_id"]: f for f in read("facilities")}, read("shipment_events")
    ships, facs = client.list_shipments(), client.list_facilities()
    if not ships["ok"] or not facs["ok"]:
        raise RuntimeError(ships.get("detail") or facs.get("detail") or "could not read shipments/facilities")
    tracked = [s["shipment_id"] for s in ships["data"] if s.get("status") in tracking.TRACKED_STAGES]
    events = client.events_for(tracked)
    return ships["data"], {f["facility_id"]: f for f in facs["data"]}, events["data"]


@st.cache_data(show_spinner="Reading loads and check calls…", ttl=300)
def _data(source: str, demo_mode: bool, refresh: int):
    return _load(source)


def get_tracker() -> tracking.Tracker | None:
    ss = st.session_state
    ss.setdefault("track_refresh", 0)
    ss.setdefault("track_dry_calls", {})          # shipment_id -> [event dicts] logged in dry-run
    ss.setdefault("track_as_of_mode", "Latest TMS activity")
    try:
        ships, facs, events = _data(ss.data_source, client.demo_mode(), ss.track_refresh)
    except Exception as e:  # keep the app alive if the API is down
        st.error(f"Tracking unavailable: {e}")
        return None
    merged = {sid: list(evs) + ss.track_dry_calls.get(sid, []) for sid, evs in events.items()}
    for sid, extra in ss.track_dry_calls.items():
        merged.setdefault(sid, list(extra))
    as_of = None if ss.track_as_of_mode == "Latest TMS activity" else datetime.now(geo.UTC)
    return tracking.Tracker(ships, facs, merged, as_of=as_of)


def current_statuses() -> tuple[dict[str, TrackingStatus], datetime] | None:
    """For the load watch: tracking statuses and the time they describe."""
    tr = get_tracker()
    return (tr.statuses, tr.as_of) if tr else None


# --------------------------------------------------------------------------- pieces

def _header(tr: tracking.Tracker) -> None:
    c1, c2, c3 = st.columns([2.2, 1.6, 1])
    c1.markdown(f"**As of {geo.fmt_local(tr.as_of, CLOCK_TZ)}**")
    c1.caption("Positions come from check calls and status changes in the TMS. "
               "ETAs are ours, at legal driving hours, shown next to what the driver reported.")
    modes = ["Latest TMS activity"] + (["Now"] if st.session_state.data_source == "Live API" else [])
    c2.radio("As of", modes, key="track_as_of_mode", horizontal=True,
             help="Latest TMS activity: the moment the data describes. Now: the current clock (live API only).")
    if c3.button("↻ Refresh", help="Re-read loads and check calls from the TMS."):
        st.session_state.track_refresh += 1
        st.rerun()


def _metrics(statuses: dict[str, TrackingStatus]) -> None:
    n = lambda k: sum(1 for s in statuses.values() if s.status == k)  # noqa: E731
    c = st.columns(5)
    c[0].metric("Trucks tracked", len(statuses))
    c[1].metric("On time", n("ON_TIME"))
    c[2].metric("At risk", n("AT_RISK"))
    c[3].metric("Late", n("LATE"))
    c[4].metric("Arrived", n("ARRIVED"))


def _map(tr: tracking.Tracker, selected: str | None) -> None:
    trucks, trails, ahead = [], [], []
    for sid, s in tr.statuses.items():
        pos = s.position
        if not pos or pos.lat is None:
            continue
        ship = tr.shipments[sid]
        dest = tracking.lane(ship, tr.facilities)["dest"]
        color = STATUS_COLOR[s.status]
        trucks.append({
            "id": sid, "lon": pos.lon, "lat": pos.lat, "color": color, "radius": 30 if sid == selected else 12,
            "status": s.status.replace("_", " ").title(), "stage": s.stage.replace("_", " ").lower(),
            "where": f"{pos.city}, {pos.state}" + (" (approx.)" if pos.approximate else ""),
            "when": f"{geo.fmt_local(pos.at, s.delivery_tz)} ({s.hours_since_report:.0f} h ago)",
            "eta": geo.fmt_local(s.eta, s.delivery_tz), "late": tracking.describe_lateness(s),
            "lane": f"{ship.get('shipper_city')} → {ship.get('consignee_city')}",
        })
        pts = [p for p in s.trail if p.lat is not None]
        for a, b in zip(pts, pts[1:]):
            trails.append({"from": [a.lon, a.lat], "to": [b.lon, b.lat],
                           "color": [*color, 230] if sid == selected else [110, 110, 110, 150]})
        if dest and s.status != "ARRIVED" and s.stage != "AT_DELIVERY":
            ahead.append({"from": [pos.lon, pos.lat], "to": [dest[1], dest[0]],
                          "color": [*color, 200 if sid == selected else 70], "width": 2.5 if sid == selected else 1})
    layers = [
        pdk.Layer("LineLayer", ahead, get_source_position="from", get_target_position="to", get_color="color", get_width="width"),
        pdk.Layer("LineLayer", trails, get_source_position="from", get_target_position="to", get_color="color", get_width=2),
        pdk.Layer("ScatterplotLayer", trucks, get_position=["lon", "lat"], get_fill_color="color", get_radius="radius",
                  radius_scale=2500, radius_min_pixels=5, radius_max_pixels=11, stroked=True,
                  get_line_color=[255, 255, 255], line_width_min_pixels=1.5, pickable=True),
    ]
    tooltip = {"html": "<b>{id}</b> · {status} · {stage}<br/>{lane}<br/>Last report: {where}<br/>{when}"
                       "<br/>Our ETA {eta}<br/>{late}", "style": {"fontSize": "12px"}}
    deck = pdk.Deck(layers=layers, initial_view_state=pdk.ViewState(latitude=38.6, longitude=-97.5, zoom=3.3),
                    map_provider="carto", map_style="light", tooltip=tooltip)
    st.pydeck_chart(deck, width="stretch", height=430)
    st.caption("Dot = last reported position. Solid line = check-call trail, thin line = still to go "
               "(straight line, not a road route). Hover for details.")


def _feed(tr: tracking.Tracker) -> None:
    st.toggle("Exceptions only", key="track_exceptions_only")
    events = tr.events(limit=300)
    if st.session_state.track_exceptions_only:
        events = [e for e in events if e.source == "copilot"]
    with st.container(height=405):
        if not events:
            st.caption("Nothing to show.")
        for e in events[:120]:
            st.markdown(f"{LEVEL_DOT[e.level]} {SOURCE_ICON[e.source]} `{geo.fmt_local(e.at, CLOCK_TZ)}` "
                        f"**{e.shipment_id}** · {e.text}")


def _timeline(tr: tracking.Tracker, sid: str) -> None:
    s, ship = tr.statuses[sid], tr.shipments[sid]
    tz_name = s.delivery_tz
    p_stop, d_stop = ship["stops"][0], ship["stops"][-1]
    pw, dw = p_stop.get("scheduled_window") or {}, d_stop.get("scheduled_window") or {}

    def loc(dt: datetime | None) -> str | None:   # receiver-local wall time, so the axis reads in local hours
        return dt.astimezone(geo.tz(tz_name)).replace(tzinfo=None).isoformat() if dt else None

    bars = [
        {"row": "Pickup window", "start": loc(geo.local_to_utc(pw.get("earliest_local"), pw.get("timezone"))),
         "end": loc(geo.local_to_utc(pw.get("latest_local"), pw.get("timezone"))), "kind": "Window"},
        {"row": "Delivery window", "start": loc(geo.local_to_utc(dw.get("earliest_local"), tz_name)),
         "end": loc(s.window_end), "kind": "Window"},
    ]
    if d_stop.get("mabd_date"):
        bars.append({"row": "Must arrive by", "start": loc(geo.local_to_utc(f"{d_stop['mabd_date']}T00:00:00", tz_name)),
                     "end": loc(s.mabd_end), "kind": "MABD"})
    pts = []
    if s.departed_at:
        pts.append({"row": "Truck", "at": loc(s.departed_at), "label": "Departed", "detail": "loaded, left the shipper"})
    for c in s.trail:
        if c.source == "check_call":
            pts.append({"row": "Truck", "at": loc(c.at), "label": "Check call", "detail": f"{c.city}, {c.state}"})
    if s.arrived_at:
        pts.append({"row": "Truck", "at": loc(s.arrived_at), "label": "Arrived", "detail": "at the receiver"})
    else:
        if s.eta:
            pts.append({"row": "Truck", "at": loc(s.eta), "label": "Our ETA (legal hours)", "detail": "; ".join(s.assumptions)})
        if s.driver_eta:
            pts.append({"row": "Truck", "at": loc(s.driver_eta), "label": "Driver's ETA", "detail": "as reported on the last call"})

    rows = [r for r in ("Pickup window", "Truck", "Delivery window", "Must arrive by")
            if r == "Truck" or any(b["row"] == r for b in bars)]
    y = alt.Y("row:N", scale=alt.Scale(domain=rows), sort=None, title=None, axis=alt.Axis(labelLimit=200))
    labels = ["Departed", "Check call", "Arrived", "Our ETA (legal hours)", "Driver's ETA"]
    bar = alt.Chart(pd.DataFrame(bars)).mark_bar(height=16, opacity=0.75).encode(
        x=alt.X("start:T", title=f"Local time at the receiver ({tz_name})"), x2="end:T", y=y,
        color=alt.Color("kind:N", scale=alt.Scale(domain=["Window", "MABD"], range=["#4C78A8", "#9D755D"]), legend=None),
        tooltip=["row", alt.Tooltip("start:T", format="%a %m/%d %H:%M"), alt.Tooltip("end:T", format="%a %m/%d %H:%M")])
    pt = alt.Chart(pd.DataFrame(pts)).mark_point(filled=True, size=150).encode(
        x="at:T", y=y,
        shape=alt.Shape("label:N", scale=alt.Scale(domain=labels, range=["circle", "diamond", "square", "triangle-up", "triangle-down"]),
                        legend=alt.Legend(title=None, orient="bottom")),
        color=alt.Color("label:N", scale=alt.Scale(domain=labels, range=["#4C78A8", "#9E9E9E", "#2E9F43", "#D62728", "#F2A93B"]),
                        legend=alt.Legend(title=None, orient="bottom")),
        tooltip=["label", alt.Tooltip("at:T", format="%a %m/%d %H:%M"), "detail"]) if pts else None
    rule = alt.Chart(pd.DataFrame([{"at": loc(tr.as_of)}])).mark_rule(strokeDash=[4, 3], color="#9E9E9E").encode(
        x="at:T", tooltip=[alt.Tooltip("at:T", title="as of", format="%a %m/%d %H:%M")])
    chart = bar + rule + pt if pt is not None else bar + rule
    st.altair_chart(chart.resolve_scale(color="independent", shape="independent").properties(height=60 + 42 * len(rows)),
                    width="stretch")
    st.caption("Dashed line = as-of time. ▲ red = our ETA at legal driving hours, ▼ yellow = the driver's reported ETA.")

    left, right = st.columns([3, 2])
    with left:
        st.markdown(f"**{STATUS_LABEL[s.status]}** · {tracking.describe_lateness(s) or '—'}")
        pos = s.position
        gap = s.eta_gap_minutes
        rows_tbl = {
            "Stage": s.stage.replace("_", " ").lower(),
            "Last position": (f"{pos.city}, {pos.state}" + (" (approx.)" if pos.approximate else "")
                              + f" · {geo.fmt_local(pos.at, tz_name)} · {s.hours_since_report:.0f} h ago") if pos else "—",
            "Miles to go": "—" if s.miles_remaining is None else f"{s.miles_remaining:.0f}",
            "Arrived" if s.arrived_at else "Our ETA (legal hours)": geo.fmt_local(s.arrived_at or s.eta, tz_name),
            "Driver's ETA": geo.fmt_local(s.driver_eta, tz_name) if s.driver_eta else "—",
            "Driver vs ours": "—" if gap is None else (f"driver {abs(gap) / 60:.1f} h earlier" if gap > 0 else
                                                      f"driver {abs(gap) / 60:.1f} h later"),
            "Window ends": geo.fmt_local(s.window_end, tz_name),
            "Must arrive by": geo.fmt_local(s.mabd_end, tz_name) if s.mabd_end else "—",
            "Carrier": ship.get("carrier_id") or "—",
        }
        st.table(pd.DataFrame({"": list(rows_tbl), "value": list(rows_tbl.values())}).set_index(""))
        for x in s.exceptions:
            (st.error if s.status == "LATE" else st.warning)(x, icon="⚠️")
        if s.assumptions:
            st.caption("Our ETA assumes: " + "; ".join(s.assumptions) + ".")
    with right:
        st.markdown("**This load's activity**")
        with st.container(height=330):
            for e in tr.events(sid, limit=40):
                st.markdown(f"{LEVEL_DOT[e.level]} {SOURCE_ICON[e.source]} `{geo.fmt_local(e.at, tz_name)}` · {e.text}")
    _check_call_form(tr, sid)


def _check_call_form(tr: tracking.Tracker, sid: str) -> None:
    s = tr.statuses[sid]
    tz_name = s.delivery_tz
    ss = st.session_state
    with st.expander(f"📞 Log a check call for {sid}"):
        with st.form(f"cc-{sid}", clear_on_submit=True):
            c1, c2, c3 = st.columns([2, 1, 2])
            city = c1.text_input("City")
            st_code = c2.text_input("State", max_chars=2)
            reason = c3.selectbox("Reason code", list(REASON_CODES), format_func=REASON_CODES.get)
            local_now = tr.as_of.astimezone(geo.tz(tz_name))
            c4, c5, c6 = st.columns(3)
            eta_day = c4.date_input("Driver's ETA (date)", value=None, help=f"Receiver local time ({tz_name})")
            eta_time = c5.time_input("Driver's ETA (time)", value=None, step=900)
            hours = c6.number_input("Driving hours left (if the driver says)", min_value=0.0, max_value=11.0,
                                    value=None, step=0.5)
            note = st.text_input("Note", placeholder="e.g. Running behind after a tire change")
            c7, c8 = st.columns(2)
            call_day = c7.date_input("Call date", value=local_now.date())
            call_time = c8.time_input("Call time", value=local_now.time().replace(second=0, microsecond=0), step=300)
            submitted = st.form_submit_button("Log check call", type="primary")
        if submitted:
            if not city or not st_code:
                st.error("City and state are required.")
                return
            at = datetime.combine(call_day, call_time, tzinfo=geo.tz(tz_name)).astimezone(geo.UTC)
            eta = (datetime.combine(eta_day, eta_time, tzinfo=geo.tz(tz_name)).astimezone(geo.UTC)
                   if eta_day and eta_time else None)
            text = (note or "Check call") + (f" ({hours:g} h left)" if hours is not None else "")
            iso = lambda d: d.isoformat().replace("+00:00", "Z")  # noqa: E731
            fields = {"city": city.strip().title(), "state": st_code.strip().upper(), "event_at": iso(at)}
            if eta:
                fields["eta"] = iso(eta)
            if reason:
                fields["reason_code"] = reason
            res = client.record_check_call(sid, text, **fields)
            if not res["ok"]:
                st.error(f"Check call not recorded: {res.get('detail') or res.get('error')}")
                return
            if res.get("dry_run"):
                ss.track_dry_calls.setdefault(sid, []).append({"event_type": "CHECK_CALL", "note": text, "_dry_run": True, **fields})
                st.toast(f"Check call for {sid} shown in dry-run (not saved to the TMS)")
            else:
                ss.track_refresh += 1
                st.toast(f"Check call saved on {sid}")
            st.rerun()
    if ss.track_dry_calls.get(sid):
        if st.button(f"Clear {len(ss.track_dry_calls[sid])} dry-run call(s) on {sid}"):
            ss.track_dry_calls.pop(sid)
            st.rerun()


# --------------------------------------------------------------------------- tab

def render() -> None:
    tr = get_tracker()
    if tr is None:
        return
    if state.use_fake():
        st.caption("Data source is *Fake data*, so tracking reads the committed snapshot.")
    _header(tr)
    _metrics(tr.statuses)

    ss = st.session_state
    ss.setdefault("track_exceptions_only", False)
    ordered = sorted(tr.statuses, key=lambda k: (STATUS_ORDER[tr.statuses[k].status],
                                                 -(tr.statuses[k].minutes_late or -10**6)))
    if ss.get("track_selected") not in tr.statuses:
        ss.track_selected = ordered[0] if ordered else None

    left, right = st.columns([3, 2])
    with left:
        _map(tr, ss.track_selected)
    with right:
        _feed(tr)

    st.subheader("Load timeline")
    st.selectbox("Load", ordered, key="track_selected",
                 format_func=lambda k: f"{k} · {tr.statuses[k].status.replace('_', ' ').title()} · "
                                       f"{tr.shipments[k].get('shipper_city')} → {tr.shipments[k].get('consignee_city')}"
                                       f" · {tracking.describe_lateness(tr.statuses[k]) or tr.statuses[k].stage.lower()}")
    if ss.track_selected:
        _timeline(tr, ss.track_selected)
