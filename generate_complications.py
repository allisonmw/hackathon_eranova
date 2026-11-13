"""Generate a synthetic-but-realistic shipment delay / complication dataset on top of the
Meridian TMS snapshot in data/raw/, cost each event, and attach solutions + alternatives.

Everything is keyed to real TMS ids (shipment_id, carrier vendor_id, facility_id, customer_id)
and uses the API's own vocabularies (late reason codes, X12 status codes, charge codes, hold
reasons, POD conditions, appointment statuses, cost-line statuses), so rows can be replayed
through the API (see api_payloads.jsonl) without translation.

Deterministic: same snapshot + same SEED -> same output.
"""
import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

SEED = 20261113
rng = np.random.default_rng(SEED)

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "complications"
OUT.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------------------------
# Assumptions (all surfaced in assumptions.csv and the workbook's Assumptions sheet)
# --------------------------------------------------------------------------------------------
A = {
    "DET_RATE_HR": (75.0, "Detention $/hr after 2 hr free — TMS charge code DET default rate"),
    "DET_FREE_HR": (2.0, "Free time before detention — TMS charge code DET name"),
    "LAY_RATE": (250.0, "Layover $/night — TMS charge code LAY default rate"),
    "STOR_RATE": (25.0, "Storage $/day — TMS charge code STOR default rate"),
    "TONU_RATE": (250.0, "Truck ordered not used — TMS charge code TONU default rate"),
    "REWEIGH_RATE": (35.0, "LTL reweigh fee — TMS charge code REWEIGH default rate"),
    "APPT_RATE": (50.0, "Re-appointment / scheduled redelivery fee — TMS charge code APPT"),
    "OOR_RATE_MI": (2.75, "Out-of-route miles paid to carrier, $/mi (assumed; ~TL linehaul+FSC)"),
    "OTIF_PCT": (0.03, "OTIF/MABD chargeback = 3% of PO value — TMS facility mabd_program_note (BBR)"),
    "OPS_RATE_HR": (42.0, "Loaded cost of CSR/dispatch labor $/hr (assumed)"),
    "COST_OF_CAPITAL": (0.09, "Annual cost of capital for delayed invoicing (assumed)"),
    "DISPOSAL_FEE": (450.0, "Rejected-load disposal / salvage handling fee (assumed)"),
    "CLAIM_DEDUCTIBLE": (1000.0, "Typical carrier cargo-policy deductible not recovered (assumed)"),
}
def a(k):
    return A[k][0]

# Cargo value per pound by customer (commodity-based assumption) -> PO value for OTIF & claims.
VALUE_PER_LB = {
    "NORTHSTAR": 3.50, "FROSTLINE": 2.20, "VALIANTFD": 1.80, "KEYSTONEHD": 2.50,
    "KEYSTONE-TX": 2.50, "IRONPEAK": 4.00, "PIONEERBLD": 1.20, "LONESTARCH": 1.50,
    "SUNBELTAG": 0.90, "CASCADEPKG": 1.10, "MIDWAYDST": 1.60, "REDROCKEL": 12.00,
    "BLUEHARBOR": 6.00, "TRUENORTH": 5.00,
}
NORTHERN = {"IL", "MI", "PA", "OH", "MA", "WA", "CO", "UT", "IA", "MO", "NJ", "OR", "WI", "MN", "NY", "IN"}
WINTER = {11, 12, 1, 2, 3}

LIFECYCLE = ["BOOKED", "COVERED", "DISPATCHED", "AT_PICKUP", "PICKED_UP", "IN_TRANSIT",
             "AT_DELIVERY", "DELIVERED", "POD_RECEIVED", "CLOSED"]
STAGE_MIN_STATUS = {  # status a shipment must have reached for an event at this stage
    "PRE_PICKUP": "COVERED", "AT_PICKUP": "AT_PICKUP", "IN_TRANSIT": "IN_TRANSIT",
    "AT_DELIVERY": "AT_DELIVERY", "POST_DELIVERY": "DELIVERED",
}

# Complication catalog: code -> (label, stage, late_reason_code, x12_status_code, root_cause_owner)
TYPES = {
    "CARRIER_FALLOFF":     ("Carrier fell off / truck not available", "PRE_PICKUP", "T3", "SD", "CARRIER"),
    "DETENTION_PICKUP":    ("Detention at shipper (slow loading)", "AT_PICKUP", "NS", "SD", "SHIPPER"),
    "MECHANICAL":          ("Tractor/trailer mechanical breakdown", "IN_TRANSIT", "AI", "SD", "CARRIER"),
    "WEATHER":             ("Weather / road closure", "IN_TRANSIT", "WEATHER", "SD", "EXTERNAL"),
    "DRIVER_HOS":          ("Driver out of hours (HOS) / poor trip plan", "IN_TRANSIT", "D1", "SD", "CARRIER"),
    "DISPATCH_ERROR":      ("Wrong address / out-of-route dispatch", "IN_TRANSIT", "D1", "SD", "BROKER"),
    "MISSED_APPOINTMENT":  ("Missed delivery appointment", "AT_DELIVERY", "LATE", "SD", "CARRIER"),
    "CONSIGNEE_CLOSED":    ("Consignee closed / refused to receive in window", "AT_DELIVERY", "B1", "SD", "CONSIGNEE"),
    "DETENTION_DELIVERY":  ("Detention at consignee (dock congestion)", "AT_DELIVERY", "NS", "SD", "CONSIGNEE"),
    "LUMPER_OVERAGE":      ("Lumper fee above quoted amount", "AT_DELIVERY", "NS", "OA", "CONSIGNEE"),
    "OTIF_CHARGEBACK":     ("Retailer OTIF / MABD chargeback", "POST_DELIVERY", "LATE", "OA", "CARRIER"),
    "REEFER_TEMP_EXCURSION": ("Reefer temperature excursion", "AT_DELIVERY", "MECHANICAL", "SD", "CARRIER"),
    "CARGO_DAMAGE":        ("Freight damaged in transit", "AT_DELIVERY", "NS", "OA", "CARRIER"),
    "SHORTAGE":            ("Short delivery (pieces missing)", "AT_DELIVERY", "NS", "OA", "CARRIER"),
    "LTL_REWEIGH_RECLASS": ("LTL reweigh / reclass by carrier", "POST_DELIVERY", "NS", "OA", "SHIPPER"),
    "PAPERWORK_HOLD":      ("Missing paperwork (POD/lumper receipt) blocks billing", "POST_DELIVERY", "NS", "P1", "BROKER"),
    "CUSTOMER_HOLD":       ("Customer-requested hold", "PRE_PICKUP", "NS", "P1", "CUSTOMER"),
    "TONU":                ("Truck ordered not used", "PRE_PICKUP", "NS", "P1", "CUSTOMER"),
}

# --------------------------------------------------------------------------------------------
# Load snapshot
# --------------------------------------------------------------------------------------------
def load(name):
    return json.loads((RAW / f"{name}.json").read_text(encoding="utf-8"))

shipments = load("shipments")
vendors = {v["vendor_id"]: v for v in load("vendors")}
facilities = {f["facility_id"]: f for f in load("facilities")}
customers = {c["customer_id"]: c for c in load("customers")}

def reached(s, stage):
    st = s["status"]
    if st == "ON_HOLD":
        st = s.get("held_from") or "BOOKED"
    if st in ("CANCELLED", "TONU"):
        return stage == "PRE_PICKUP"
    return LIFECYCLE.index(st) >= LIFECYCLE.index(STAGE_MIN_STATUS[stage])

def stage_is_current(s, stage):
    """True when the event's stage is the shipment's live stage (cost still accruing)."""
    st = s["status"] if s["status"] != "ON_HOLD" else (s.get("held_from") or "BOOKED")
    return st not in ("POD_RECEIVED", "CLOSED", "CANCELLED", "TONU")

def carrier_risk(s):
    v = vendors.get(s.get("carrier_id") or "")
    if not v or v.get("on_time_delivery_pct") is None:
        return 1.0
    r = (100 - v["on_time_delivery_pct"]) / 8.0  # 92% OTD -> 1.0
    if v.get("compliance_light") == "RED" or v.get("status") == "DO_NOT_USE":
        r *= 1.4
    return max(0.5, min(r, 4.5))

def stop(s, i):
    return s["stops"][i]

def fac(st):
    return facilities.get(st.get("facility_id") or "", {})

def cargo_value(s):
    if s.get("declared_value"):
        return float(s["declared_value"])
    lb = (s.get("freight_totals") or {}).get("chargeable_lb") or 10000
    return round(lb * VALUE_PER_LB.get(s["customer_id"], 2.0), 2)

def frt_buy(s):
    v = sum(c["amount"] for c in s.get("vendor_costs", []) if c["charge_code"] == "FRT" and c["status"] != "CANCELLED")
    if not v:  # uncovered: estimate from sell FRT at the shipment's typical margin
        v = sum(c["amount"] for c in s.get("customer_charges", []) if c["charge_code"] == "FRT") * 0.85
    return round(v, 2)

def local_to_utc(local_str, tz):
    dt = datetime.fromisoformat(local_str).replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(timezone.utc)

def window(st):
    w = st.get("scheduled_window") or {}
    tz = w.get("timezone") or st["address_snapshot"]["timezone"]
    return datetime.fromisoformat(w["earliest_local"]), datetime.fromisoformat(w["latest_local"]), tz

# --------------------------------------------------------------------------------------------
# Event + cost-line builders
# --------------------------------------------------------------------------------------------
events, lines, payloads = [], [], []
eid_counter = [0]

def new_event(s, etype, stop_seq, at_local, tz, delay_h, detail, parent=None, source="SYNTHETIC", extra=None):
    eid_counter[0] += 1
    label, stage, lrc, x12, owner = TYPES[etype]
    st = s["stops"][stop_seq - 1]
    f = fac(st)
    ev = {
        "event_id": f"DLY-{eid_counter[0]:05d}",
        "parent_event_id": parent["event_id"] if parent else "",
        "root_event_id": (parent["root_event_id"] if parent else f"DLY-{eid_counter[0]:05d}"),
        "source": source,
        "shipment_id": s["shipment_id"],
        "shipment_status": s["status"],
        "mode": s["mode"],
        "service_level": s.get("service_level") or "",
        "equipment_type": s.get("equipment_type") or "",
        "customer_id": s["customer_id"],
        "carrier_id": s.get("carrier_id") or "",
        "lane": f'{s["shipper_city"]},{s["shipper_state"]} > {s["consignee_city"]},{s["consignee_state"]}',
        "miles": s.get("miles") or 0,
        "pickup_date": s.get("pickup_date"),
        "month": s.get("pickup_date", "")[:7],
        "stop_seq": stop_seq,
        "stop_type": st.get("stop_type"),
        "facility_id": st.get("facility_id") or "",
        "facility_type": f.get("facility_type", ""),
        "complication_type": etype,
        "complication_label": label,
        "stage": stage,
        "late_reason_code": lrc,
        "x12_status_code": x12,
        "root_cause_owner": owner,
        "event_at_local": at_local.strftime("%Y-%m-%dT%H:%M:%S"),
        "event_timezone": tz,
        "event_at_utc": local_to_utc(at_local.isoformat(), tz).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "delay_hours": round(delay_h, 1),
        "cost_status": "ESTIMATED" if stage_is_current(s, stage) else "CONFIRMED",
        "detail": detail,
        "appointment_status_after": "",
        "pod_condition": "",
        "hold_reason": "",
        "service_failure": False,
    }
    if extra:
        ev.update(extra)
    events.append(ev)
    return ev

def add_line(ev, category, ledger, code, qty, rate, desc, vendor_id="", recoverable="", sign=1):
    """ledger: BUY (vendor_costs), SELL (customer_charges) or OFF_LEDGER (hidden cost not in TMS)."""
    amt = round(round(qty, 2) * round(rate, 4), 2)
    if amt == 0:
        return
    lines.append({
        "line_id": f"{ev['event_id']}-{sum(1 for l in lines if l['event_id'] == ev['event_id']) + 1}",
        "event_id": ev["event_id"],
        "root_event_id": ev["root_event_id"],
        "shipment_id": ev["shipment_id"],
        "complication_type": ev["complication_type"],
        "cost_category": category,
        "ledger": ledger,
        "charge_code": code,
        "rate_basis": "PER_HOUR" if code == "DET" else ("PER_MILE" if code == "OOR" else "FLAT"),
        "quantity": round(qty, 2),
        "rate": round(rate, 4),
        "amount": amt,
        "sign": sign,  # +1 = cost to Meridian, -1 = recovery that offsets cost
        "net_cost": round(sign * amt, 2),
        "vendor_id": vendor_id,
        "status": ev["cost_status"] if ledger == "BUY" else ("APPROVED" if ev["cost_status"] == "CONFIRMED" else "ESTIMATED") if ledger == "SELL" else ev["cost_status"],
        "recoverable_from": recoverable,
        "in_tms": False,
        "description": desc,
    })
    # API-compatible payload for TMS-ledger lines (not posted — this script never calls the API)
    if ledger in ("BUY", "SELL") and code in {"DET", "LAY", "STOR", "TONU", "REWEIGH", "APPT", "LUM", "FRT", "STOP"}:
        body = {"charge_code": code, "description": desc, "quantity": round(qty, 2), "rate": round(rate, 4),
                "rate_basis": "PER_HOUR" if code == "DET" else "FLAT"}
        if ledger == "BUY":
            body["vendor_id"] = vendor_id
            body["status"] = "CONFIRMED" if ev["cost_status"] == "CONFIRMED" else "ESTIMATED"
            route = f"/shipments/{ev['shipment_id']}/costs"
        else:
            body["status"] = "APPROVED" if ev["cost_status"] == "CONFIRMED" else "ESTIMATED"
            route = f"/shipments/{ev['shipment_id']}/charges"
        payloads.append({"event_id": ev["event_id"], "method": "POST", "route": route, "body": body})

def ops(ev, hours, why="CSR/dispatch exception handling"):
    add_line(ev, "OPS_LABOR", "OFF_LEDGER", "OPS", hours, a("OPS_RATE_HR"), f"{hours:.2f} h {why}")

def check_call_payload(ev, s):
    st = s["stops"][ev["stop_seq"] - 1]["address_snapshot"]
    payloads.append({"event_id": ev["event_id"], "method": "POST",
                     "route": f"/shipments/{ev['shipment_id']}/check-calls",
                     "body": {"status_code": ev["x12_status_code"], "reason_code": ev["late_reason_code"],
                              "city": st["city"], "state": st["state"],
                              "event_at": ev["event_at_local"], "event_timezone": ev["event_timezone"],
                              "note": f"[{ev['event_id']}] {ev['complication_label']}: {ev['detail']}"}})

# --------------------------------------------------------------------------------------------
# Cascade: an upstream delay that eats the delivery slack -> missed appt -> layover -> OTIF
# --------------------------------------------------------------------------------------------
def cascade_delivery(s, parent, delay_h):
    last = len(s["stops"])
    dst = stop(s, last - 1)
    f = fac(dst)
    e0, l0, tz = window(dst)
    slack = rng.uniform(1.5, 8.0)  # planned buffer between ETA and appointment / window close
    if delay_h <= slack:
        return
    # a delivery can only be missed once: later upstream delays fold into the first cascade
    if any(e["shipment_id"] == s["shipment_id"] and e["complication_type"] in ("MISSED_APPOINTMENT", "CONSIGNEE_CLOSED") for e in events):
        return
    carrier = s.get("carrier_id") or ""
    appt_needed = f.get("appointment_required") or dst.get("appointment", {}).get("status") not in (None, "NOT_REQUIRED")
    at = e0 + timedelta(hours=delay_h - slack)
    if appt_needed:
        late_h = delay_h - slack
        ev = new_event(s, "MISSED_APPOINTMENT", last, at, tz, late_h,
                       f"Arrived {late_h:.1f} h after appointment; facility reworked to next open slot",
                       parent=parent, extra={"late_reason_code": parent["late_reason_code"],
                                             "appointment_status_after": "RESCHEDULED", "service_failure": True})
        nights = 1 if late_h < 20 else 2
        add_line(ev, "CARRIER_ACCESSORIAL", "BUY", "LAY", nights, a("LAY_RATE"), f"Layover {nights} night(s) awaiting reschedule", carrier)
        add_line(ev, "CARRIER_ACCESSORIAL", "BUY", "APPT", 1, a("APPT_RATE"), "Re-appointment / redelivery", carrier)
        ops(ev, rng.uniform(0.75, 1.75), "rescheduling with receiver + customer updates")
        # who eats it? carrier-caused -> charge back to carrier; weather -> pass to customer partially
        if parent["root_cause_owner"] == "CARRIER" and rng.random() < 0.55:
            add_line(ev, "CARRIER_CHARGEBACK", "BUY", "LAY", nights, a("LAY_RATE"), "Layover disputed back to carrier (carrier-caused)", carrier, "CARRIER", sign=-1)
        elif parent["root_cause_owner"] in ("EXTERNAL", "SHIPPER") and rng.random() < 0.45:
            add_line(ev, "CUSTOMER_RECOVERY", "SELL", "LAY", nights, a("LAY_RATE"), "Layover billed to customer", "", "CUSTOMER", sign=-1)
        # MABD/OTIF program: a late arrival past the MABD date triggers a chargeback
        if f.get("mabd_program") and late_h > 6 and rng.random() < 0.8:
            po = cargo_value(s)
            ch = new_event(s, "OTIF_CHARGEBACK", last, l0 + timedelta(days=int(rng.integers(12, 35))), tz, 0,
                           f"{f.get('parent_company') or f.get('name')} OTIF fine {a('OTIF_PCT'):.0%} of PO value ${po:,.0f}",
                           parent=ev, extra={"late_reason_code": parent["late_reason_code"]})
            add_line(ch, "CUSTOMER_PENALTY", "OFF_LEDGER", "OTIF", 1, round(po * a("OTIF_PCT"), 2),
                     f"OTIF chargeback passed through by {s['customer_id']}")
            ops(ch, rng.uniform(1.0, 2.5), "chargeback dispute package")
            if parent["root_cause_owner"] == "CARRIER" and rng.random() < 0.35:
                add_line(ch, "CARRIER_CHARGEBACK", "OFF_LEDGER", "OTIF", 1, round(po * a("OTIF_PCT") * 0.5, 2),
                         "50% of OTIF fine recovered from carrier", carrier, "CARRIER", sign=-1)
    elif rng.random() < 0.5:
        # FCFS receiver: late = arrives after receiving close -> overnight
        recv = (f.get("receiving_hours") or {})
        ev = new_event(s, "CONSIGNEE_CLOSED", last, at, tz, delay_h - slack,
                       "Arrived after receiving hours; held overnight", parent=parent,
                       extra={"late_reason_code": "B1", "service_failure": True})
        add_line(ev, "CARRIER_ACCESSORIAL", "BUY", "LAY", 1, a("LAY_RATE"), "Layover after receiving closed", carrier)
        ops(ev, rng.uniform(0.5, 1.0))

# --------------------------------------------------------------------------------------------
# Per-type generators
# --------------------------------------------------------------------------------------------
def g_falloff(s, forced=False):
    p0 = stop(s, 0); e0, _, tz = window(p0)
    at = e0 - timedelta(hours=float(rng.uniform(2, 30)))
    late = float(rng.uniform(3, 20)) if (forced or rng.random() < 0.7) else 0.0
    prem = float(rng.uniform(0.12, 0.40))
    ev = new_event(s, "CARRIER_FALLOFF", 1, at, tz, late,
                   f"Original truck unavailable; recovered on spot market at +{prem:.0%} — pickup {late:.1f} h late",
                   extra={"service_failure": late > 0})
    add_line(ev, "RECOVERY_PREMIUM", "BUY", "FRT", 1, round(frt_buy(s) * prem, 2), f"Spot-market recovery premium (+{prem:.0%} linehaul)", s.get("carrier_id") or "")
    ops(ev, rng.uniform(1.0, 2.5), "re-covering the load")
    check_call_payload(ev, s)
    if late:
        cascade_delivery(s, ev, late)

def g_det_pickup(s, existing_hours=None):
    p0 = stop(s, 0); e0, _, tz = window(p0)
    f = fac(p0)
    dwell = existing_hours + a("DET_FREE_HR") if existing_hours else float(rng.lognormal(math.log(3.6 + f.get("avg_unload_minutes", 60) / 120), 0.3))
    det = max(0.0, round(dwell - a("DET_FREE_HR")))
    if det < 1:
        return
    ev = new_event(s, "DETENTION_PICKUP", 1, e0 + timedelta(hours=dwell), tz, det,
                   f"Driver on dock {dwell:.1f} h at {p0['address_snapshot']['name']}")
    add_line(ev, "CARRIER_ACCESSORIAL", "BUY", "DET", det, a("DET_RATE_HR"), f"Detention {det:.0f} hrs at shipper", s.get("carrier_id") or "")
    if rng.random() < 0.7:  # shipper is usually the customer -> billable
        add_line(ev, "CUSTOMER_RECOVERY", "SELL", "DET", det, a("DET_RATE_HR"), f"Detention {det:.0f} hrs at shipper", "", "CUSTOMER", sign=-1)
    ops(ev, 0.5)
    check_call_payload(ev, s)
    if det >= 3:
        cascade_delivery(s, ev, det)

def g_mechanical(s):
    p0 = stop(s, 0); e0, _, tz = window(p0)
    delay = float(rng.uniform(3, 16))
    at = e0 + timedelta(hours=float(rng.uniform(2, 10)))
    repower = rng.random() < 0.3
    ev = new_event(s, "MECHANICAL", 1, at, tz, delay,
                   ("Breakdown en route; load re-powered with recovery truck" if repower else "Breakdown en route; roadside repair") + f", {delay:.1f} h lost")
    if repower:
        prem = float(rng.uniform(0.30, 0.60))
        add_line(ev, "RECOVERY_PREMIUM", "BUY", "FRT", 1, round(frt_buy(s) * prem, 2), f"Re-power truck (+{prem:.0%} of linehaul)", s.get("carrier_id") or "")
        if rng.random() < 0.4:
            add_line(ev, "CARRIER_CHARGEBACK", "BUY", "FRT", 1, round(frt_buy(s) * prem * 0.5, 2), "Half of re-power short-paid to original carrier", s.get("carrier_id") or "", "CARRIER", sign=-1)
    ops(ev, rng.uniform(1.0, 3.0), "breakdown recovery + customer updates")
    check_call_payload(ev, s)
    cascade_delivery(s, ev, delay)

def g_weather(s, forced=False):
    p0 = stop(s, 0); e0, _, tz = window(p0)
    delay = float(rng.uniform(5, 32))
    ev = new_event(s, "WEATHER", 1, e0 + timedelta(hours=float(rng.uniform(3, 14))), tz, delay,
                   f"Winter storm / road closure; {delay:.0f} h shutdown" if int(s["pickup_date"][5:7]) in WINTER else f"Severe weather / closure; {delay:.0f} h delay",
                   extra={"service_failure": forced})
    if delay > 14:
        add_line(ev, "CARRIER_ACCESSORIAL", "BUY", "LAY", 1, a("LAY_RATE"), "Weather layover", s.get("carrier_id") or "")
        if rng.random() < 0.35:
            add_line(ev, "CUSTOMER_RECOVERY", "SELL", "LAY", 1, a("LAY_RATE"), "Weather layover billed (force majeure clause)", "", "CUSTOMER", sign=-1)
    ops(ev, rng.uniform(0.5, 1.5))
    check_call_payload(ev, s)
    cascade_delivery(s, ev, delay)

def g_hos(s):
    p0 = stop(s, 0); e0, _, tz = window(p0)
    delay = float(rng.uniform(4, 11))
    ev = new_event(s, "DRIVER_HOS", 1, e0 + timedelta(hours=float(rng.uniform(6, 12))), tz, delay,
                   f"Driver out of drive hours, mandatory 10 h break; {delay:.1f} h late vs plan")
    ops(ev, rng.uniform(0.5, 1.0))
    check_call_payload(ev, s)
    cascade_delivery(s, ev, delay)

def g_dispatch_err(s):
    p0 = stop(s, 0); e0, _, tz = window(p0)
    oor = int(rng.integers(20, 120)); delay = oor / 50 + float(rng.uniform(0.5, 2))
    ev = new_event(s, "DISPATCH_ERROR", 1, e0 + timedelta(hours=float(rng.uniform(4, 10))), tz, delay,
                   f"Driver routed to wrong address on rate con; {oor} out-of-route miles")
    add_line(ev, "CARRIER_ACCESSORIAL", "BUY", "OOR", oor, a("OOR_RATE_MI"), f"{oor} out-of-route miles", s.get("carrier_id") or "")
    ops(ev, rng.uniform(0.5, 1.5), "re-issue rate confirmation")
    check_call_payload(ev, s)
    cascade_delivery(s, ev, delay)

def g_det_delivery(s, existing_hours=None):
    last = len(s["stops"]); d = stop(s, last - 1); e0, _, tz = window(d); f = fac(d)
    if existing_hours:
        det = existing_hours
    else:
        dwell = float(rng.lognormal(math.log(2.4 + f.get("avg_unload_minutes", 60) / 90), 0.35))
        det = max(0.0, round(dwell - a("DET_FREE_HR")))
    if det < 1:
        return
    ev = new_event(s, "DETENTION_DELIVERY", last, e0 + timedelta(hours=det + 2), tz, det,
                   f"Dock congestion at {d['address_snapshot']['name']} ({f.get('dock_count','?')} doors, avg unload {f.get('avg_unload_minutes','?')} min)",
                   source="TMS_EXISTING" if existing_hours else "SYNTHETIC")
    if not existing_hours:  # existing ones are already on the TMS ledger; don't duplicate payloads
        add_line(ev, "CARRIER_ACCESSORIAL", "BUY", "DET", det, a("DET_RATE_HR"), f"Detention {det:.0f} hrs at consignee", s.get("carrier_id") or "")
    else:
        lines.append(dict(_existing_line(s, ev, "DET")))
    if existing_hours:
        _existing_charge(s, ev, "DET")
    elif rng.random() < 0.45:
        add_line(ev, "CUSTOMER_RECOVERY", "SELL", "DET", det, a("DET_RATE_HR"), f"Detention {det:.0f} hrs billed to customer", "", "CUSTOMER", sign=-1)
    ops(ev, 0.5)
    if not existing_hours:
        check_call_payload(ev, s)

def _existing_line(s, ev, code):
    c = next(c for c in s["vendor_costs"] if c["charge_code"] == code)
    return {"line_id": f"{ev['event_id']}-TMS-{c['cost_id'][-6:]}", "event_id": ev["event_id"], "root_event_id": ev["root_event_id"],
            "shipment_id": s["shipment_id"], "complication_type": ev["complication_type"], "cost_category": "CARRIER_ACCESSORIAL",
            "ledger": "BUY", "charge_code": code, "rate_basis": c["rate_basis"], "quantity": c["quantity"], "rate": c["rate"],
            "amount": c["amount"], "sign": 1, "net_cost": c["amount"], "vendor_id": c["vendor_id"], "status": c["status"],
            "recoverable_from": "", "in_tms": True, "description": c["description"] + " (already in TMS: " + c["cost_id"] + ")"}

def _existing_charge(s, ev, code):
    """Customer charge already on the TMS sell ledger -> recovery line flagged in_tms."""
    c = next((c for c in s.get("customer_charges", []) if c["charge_code"] == code), None)
    if not c:
        return False
    lines.append({"line_id": f"{ev['event_id']}-TMS-{c['charge_id'][-6:]}", "event_id": ev["event_id"], "root_event_id": ev["root_event_id"],
                  "shipment_id": s["shipment_id"], "complication_type": ev["complication_type"], "cost_category": "CUSTOMER_RECOVERY",
                  "ledger": "SELL", "charge_code": code, "rate_basis": c["rate_basis"], "quantity": c["quantity"], "rate": c["rate"],
                  "amount": c["amount"], "sign": -1, "net_cost": -c["amount"], "vendor_id": "", "status": c["status"],
                  "recoverable_from": "CUSTOMER", "in_tms": True, "description": c["description"] + " (already in TMS: " + c["charge_id"] + ")"})
    return True

def g_lumper_overage(s):
    last = len(s["stops"]); d = stop(s, last - 1); e0, _, tz = window(d); f = fac(d)
    quoted = next((c["amount"] for c in s.get("customer_charges", []) if c["charge_code"] == "LUM"), f.get("lumper_fee_min") or 175)
    over = float(rng.uniform(25, 140))
    ev = new_event(s, "LUMPER_OVERAGE", last, e0 + timedelta(hours=2.5), tz, 0,
                   f"Lumper charged ${quoted + over:,.0f} vs ${quoted:,.0f} quoted (facility range ${f.get('lumper_fee_min')}-{f.get('lumper_fee_max')})")
    add_line(ev, "CARRIER_ACCESSORIAL", "BUY", "LUM", 1, round(over, 2), "Lumper overage reimbursed to driver", s.get("carrier_id") or "")
    if rng.random() < 0.4:
        add_line(ev, "CUSTOMER_RECOVERY", "SELL", "LUM", 1, round(over, 2), "Lumper overage rebilled with receipt", "", "CUSTOMER", sign=-1)
    ops(ev, 0.33)

def _claim(s, ev, loss_frac, salvage=0.0, carrier_liable=True):
    val = cargo_value(s)
    loss = round(val * loss_frac * (1 - salvage), 2)
    add_line(ev, "CLAIM_LOSS", "OFF_LEDGER", "CLAIM", 1, loss, f"Product loss {loss_frac:.0%} of ${val:,.0f} cargo" + (f", {salvage:.0%} salvage" if salvage else ""))
    # Carmack: carrier is liable; broker eats the deductible gap, or a negotiated share if denied
    if carrier_liable and rng.random() < 0.85:
        rec = max(0.0, loss - a("CLAIM_DEDUCTIBLE")) * float(rng.uniform(0.9, 1.0))
        if rec > 0:
            add_line(ev, "CLAIM_RECOVERY", "OFF_LEDGER", "CLAIM", 1, round(rec, 2), "Recovered from carrier cargo insurance (net of deductible / depreciation)", s.get("carrier_id") or "", "CARRIER", sign=-1)
    else:
        settle = float(rng.uniform(0.4, 0.7))
        add_line(ev, "CLAIM_RECOVERY", "OFF_LEDGER", "CLAIM", 1, round(loss * settle, 2), f"Carrier denied; customer settled at {1 - settle:.0%} of claim (goodwill)", "", "CUSTOMER", sign=-1)
    ops(ev, rng.uniform(3, 8), "claim filing, photos, salvage coordination")

def g_reefer(s):
    last = len(s["stops"]); d = stop(s, last - 1); e0, _, tz = window(d)
    r = rng.random()
    if r < 0.15:
        ev = new_event(s, "REEFER_TEMP_EXCURSION", last, e0 + timedelta(hours=1), tz, 2,
                       "Unit alarm; pulp check within spec after download — accepted", extra={"pod_condition": "CLEAR"})
        ops(ev, 1.0)
        return
    full = r > 0.75
    ev = new_event(s, "REEFER_TEMP_EXCURSION", last, e0 + timedelta(hours=1), tz, float(rng.uniform(4, 30)),
                   "Reefer unit failure; pulp out of range — load REFUSED" if full else "Temp drift; pallets rejected at door",
                   extra={"pod_condition": "REFUSED" if full else "DAMAGE", "hold_reason": "CLAIM", "service_failure": full})
    _claim(s, ev, 1.0 if full else float(rng.uniform(0.05, 0.25)), salvage=0.25 if full else 0)
    days = int(rng.integers(1, 4))
    add_line(ev, "CARRIER_ACCESSORIAL", "BUY", "STOR", days, a("STOR_RATE"), f"Cold storage {days} day(s) pending inspection", "")
    if full:
        add_line(ev, "DISPOSAL", "OFF_LEDGER", "DISPOSAL", 1, a("DISPOSAL_FEE"), "Disposal / salvage handling")
    check_call_payload(ev, s)
    payloads.append({"event_id": ev["event_id"], "method": "POST", "route": f"/shipments/{s['shipment_id']}/status",
                     "body": {"status": "ON_HOLD", "hold_reason": "CLAIM", "note": ev["detail"]}})

def g_damage(s, existing=False):
    last = len(s["stops"]); d = stop(s, last - 1); e0, _, tz = window(d)
    ev = new_event(s, "CARGO_DAMAGE", last, e0 + timedelta(hours=1.5), tz, 0,
                   "Load shift / forklift damage noted on POD", source="TMS_EXISTING" if existing else "SYNTHETIC",
                   extra={"pod_condition": "DAMAGE", "hold_reason": "CLAIM"})
    _claim(s, ev, float(rng.uniform(0.02, 0.15)))

def g_shortage(s, existing=False):
    last = len(s["stops"]); d = stop(s, last - 1); e0, _, tz = window(d)
    pcs = (s.get("freight_totals") or {}).get("pieces") or 10
    short = max(1, int(round(pcs * rng.uniform(0.03, 0.10))))
    ev = new_event(s, "SHORTAGE", last, e0 + timedelta(hours=1.5), tz, 0,
                   f"Short {short} of {pcs} pieces at delivery", source="TMS_EXISTING" if existing else "SYNTHETIC",
                   extra={"pod_condition": "SHORTAGE", "hold_reason": "CLAIM"})
    _claim(s, ev, short / pcs)

def g_ltl_reweigh(s):
    last = len(s["stops"]); d = stop(s, last - 1); e0, _, tz = window(d)
    adj = float(rng.uniform(0.04, 0.18))
    ev = new_event(s, "LTL_REWEIGH_RECLASS", last, e0 + timedelta(days=int(rng.integers(3, 15))), tz, 0,
                   f"Carrier inspection: reweigh + class change, +{adj:.0%} linehaul")
    add_line(ev, "CARRIER_ACCESSORIAL", "BUY", "REWEIGH", 1, a("REWEIGH_RATE"), "Reweigh fee", s.get("carrier_id") or "")
    add_line(ev, "CARRIER_ACCESSORIAL", "BUY", "FRT", 1, round(frt_buy(s) * adj, 2), "Reclass linehaul adjustment", s.get("carrier_id") or "")
    if rng.random() < 0.6:
        add_line(ev, "CUSTOMER_RECOVERY", "SELL", "FRT", 1, round(frt_buy(s) * adj * 1.1 + a("REWEIGH_RATE"), 2), "Reclass + reweigh rebilled to shipper", "", "CUSTOMER", sign=-1)
    ops(ev, 0.75, "inspection certificate dispute")

def g_paperwork(s, existing=False):
    last = len(s["stops"]); d = stop(s, last - 1); e0, _, tz = window(d)
    days = int(rng.integers(5, 26))
    lump = any(c["charge_code"] == "LUM" for c in s.get("vendor_costs", []))
    ev = new_event(s, "PAPERWORK_HOLD", last, e0 + timedelta(days=1), tz, 0,
                   ("Lumper receipt missing; " if lump else "POD missing/illegible; ") + f"invoice delayed {days} days",
                   source="TMS_EXISTING" if existing else "SYNTHETIC", extra={"hold_reason": "PAPERWORK"})
    add_line(ev, "CAPITAL_COST", "OFF_LEDGER", "DSO", days, round((s.get("sell_total") or 0) * a("COST_OF_CAPITAL") / 365, 4), f"Cost of capital on ${s.get('sell_total',0):,.0f} invoice x {days} days")
    if lump and rng.random() < 0.3:
        lv = next(c["amount"] for c in s["vendor_costs"] if c["charge_code"] == "LUM")
        add_line(ev, "UNBILLABLE", "OFF_LEDGER", "LUM", 1, lv, "Lumper not rebillable without receipt — written off")
    ops(ev, rng.uniform(0.5, 1.5), "chasing documents")

# --------------------------------------------------------------------------------------------
# Drive the simulation
# --------------------------------------------------------------------------------------------
for s in sorted(shipments, key=lambda x: x["shipment_id"]):
    sid = s["shipment_id"]
    rc = carrier_risk(s)
    month = int(s["pickup_date"][5:7])
    states = {s["shipper_state"], s["consignee_state"]}
    dfac = fac(stop(s, len(s["stops"]) - 1))
    pfac = fac(stop(s, 0))
    sfr = s.get("service_failure_reason")
    pod = (s.get("pod") or {}).get("condition")
    existing_codes = {c["charge_code"]: c for c in s.get("vendor_costs", [])}

    # ---- 1. Anchor on what the TMS already says happened -------------------------------
    if s["status"] == "TONU":
        p0 = stop(s, 0); e0, _, tz = window(p0)
        ev = new_event(s, "TONU", 1, e0, tz, 0, "Customer cancelled at shipper dock after truck arrived", source="TMS_EXISTING")
        lines.append(_existing_line(s, ev, "TONU"))
        _existing_charge(s, ev, "TONU")
        ops(ev, 1.0)
        continue
    if s["status"] == "CANCELLED":
        continue
    if s.get("hold_reason") == "CUSTOMER_REQUEST":
        p0 = stop(s, 0); e0, _, tz = window(p0)
        ev = new_event(s, "CUSTOMER_HOLD", 1, e0 - timedelta(days=3), tz, 0, (s.get("internal_notes") or "Customer hold")[:140], source="TMS_EXISTING", extra={"hold_reason": "CUSTOMER_REQUEST"})
        ops(ev, 0.75, "re-confirming pickup date")
    if s.get("hold_reason") == "PAPERWORK":
        g_paperwork(s, existing=True)

    forced_types = set()
    if sfr == "T3" or "LAY" in existing_codes:
        if "LAY" in existing_codes:  # 7300111: detention + layover after missing 14:00 receiving window
            last = len(s["stops"]); d = stop(s, last - 1); e0, l0, tz = window(d)
            ev = new_event(s, "MISSED_APPOINTMENT", last, l0, tz, 18, (s.get("internal_notes") or "")[:160], source="TMS_EXISTING",
                           extra={"late_reason_code": sfr or "T3", "appointment_status_after": "RESCHEDULED", "service_failure": True})
            lines.append(_existing_line(s, ev, "DET")); lines.append(_existing_line(s, ev, "LAY"))
            ops(ev, 1.5)
            if dfac.get("mabd_program"):
                po = cargo_value(s)
                ch = new_event(s, "OTIF_CHARGEBACK", last, l0 + timedelta(days=21), tz, 0, f"OTIF fine {a('OTIF_PCT'):.0%} of PO ${po:,.0f}", parent=ev)
                add_line(ch, "CUSTOMER_PENALTY", "OFF_LEDGER", "OTIF", 1, round(po * a("OTIF_PCT"), 2), "OTIF chargeback passed through")
                ops(ch, 1.5, "chargeback dispute package")
            forced_types |= {"DETENTION_DELIVERY"}
        else:
            g_falloff(s, forced=True)
        forced_types.add("CARRIER_FALLOFF")
    elif sfr == "WEATHER":
        g_weather(s, forced=True); forced_types.add("WEATHER")
    elif sfr in ("NS", "LATE"):
        # late without a carrier code: receiver-side delay
        last = len(s["stops"]); d = stop(s, last - 1); e0, l0, tz = window(d)
        ev = new_event(s, "CONSIGNEE_CLOSED", last, l0, tz, float(rng.uniform(3, 10)), "Receiver could not work load in window; delivered next shift",
                       source="TMS_EXISTING", extra={"late_reason_code": sfr, "service_failure": True})
        add_line(ev, "CARRIER_ACCESSORIAL", "BUY", "LAY", 1, a("LAY_RATE"), "Layover — receiver pushed delivery", s.get("carrier_id") or "")
        if rng.random() < 0.5:
            add_line(ev, "CUSTOMER_RECOVERY", "SELL", "LAY", 1, a("LAY_RATE"), "Layover billed to customer (receiver-caused)", "", "CUSTOMER", sign=-1)
        ops(ev, 1.0)
        forced_types.add("CONSIGNEE_CLOSED")
    if "DET" in existing_codes and "LAY" not in existing_codes:
        g_det_delivery(s, existing_hours=existing_codes["DET"]["quantity"]); forced_types.add("DETENTION_DELIVERY")
    if pod == "DAMAGE":
        g_damage(s, existing=True); forced_types.add("CARGO_DAMAGE")
    if pod == "SHORTAGE":
        g_shortage(s, existing=True); forced_types.add("SHORTAGE")

    # ---- 2. Stochastic complications, gated by the stage the load has actually reached --
    def roll(etype, p):
        return etype not in forced_types and reached(s, TYPES[etype][1]) and rng.random() < min(p, 0.9)

    if not s.get("carrier_id"):
        continue  # uncovered loads: nothing can have happened yet
    if roll("CARRIER_FALLOFF", 0.04 * rc):
        g_falloff(s)
    if roll("DETENTION_PICKUP", 0.04 + pfac.get("avg_unload_minutes", 60) / 1500):
        g_det_pickup(s)
    in_transit_hit = False
    if roll("MECHANICAL", 0.03 * rc):
        g_mechanical(s); in_transit_hit = True
    winter_north = month in WINTER and bool(states & NORTHERN)
    if not in_transit_hit and roll("WEATHER", 0.11 if winter_north else 0.025):
        g_weather(s); in_transit_hit = True
    if not in_transit_hit and s["mode"] != "LTL" and (s.get("miles") or 0) > 450 and roll("DRIVER_HOS", 0.035 * rc):
        g_hos(s); in_transit_hit = True
    if not in_transit_hit and roll("DISPATCH_ERROR", 0.015):
        g_dispatch_err(s)
    if roll("DETENTION_DELIVERY", 0.05 + dfac.get("avg_unload_minutes", 60) / 1200 + (0.06 if dfac.get("lumper_policy") == "REQUIRED" else 0)):
        g_det_delivery(s)
    if dfac.get("lumper_policy") in ("REQUIRED", "OPTIONAL") and roll("LUMPER_OVERAGE", 0.10):
        g_lumper_overage(s)
    if s.get("service_level") == "TL_REEFER" and roll("REEFER_TEMP_EXCURSION", 0.045 * rc):
        g_reefer(s)
    if pod not in ("DAMAGE", "SHORTAGE") and roll("CARGO_DAMAGE", 0.012 * rc):
        g_damage(s)
    elif pod not in ("DAMAGE", "SHORTAGE") and roll("SHORTAGE", 0.01 * rc):
        g_shortage(s)
    if s["mode"] == "LTL" and roll("LTL_REWEIGH_RECLASS", 0.12):
        g_ltl_reweigh(s)
    if s.get("hold_reason") != "PAPERWORK" and roll("PAPERWORK_HOLD", 0.03 + (0.04 if "LUM" in existing_codes else 0)):
        g_paperwork(s)

# --------------------------------------------------------------------------------------------
# Solutions & alternatives
# --------------------------------------------------------------------------------------------
SOLUTIONS = {
    # type: (primary solution, avoidable %, alternative 1, alt-1 avoidable %, alternative 2, alt-2 avoidable %, owner, TMS action)
    "CARRIER_FALLOFF": ("Tender to scorecarded primary + pre-approved backup carrier per lane (routing guide)", 0.55,
                        "Dedicated/contract capacity on top lanes", 0.70, "Book 48 h earlier; cover T-2 days instead of T-0", 0.35,
                        "DISPATCH", "POST /shipments/{id}/uncover reason=FELL_THROUGH, then /cover with backup carrier"),
    "DETENTION_PICKUP": ("Pre-loaded / drop-trailer program at shipper", 0.75, "Enforce accessorial schedule: bill DET to shipper on every occurrence", 0.50,
                         "First-slot-of-day pickup appointments", 0.30, "SALES", "POST /shipments/{id}/charges DET; set stop appointment early slot"),
    "MECHANICAL": ("Prefer carriers with >=93% OTD and newer fleet; auto-trigger re-power at 2 h", 0.45, "Short-pay re-power cost to carrier per contract", 0.30,
                   "Power-only backup pool at hubs", 0.40, "DISPATCH", "Check-call status_code SD reason_code AI; /uncover EQUIPMENT if unrecoverable"),
    "WEATHER": ("Weather-aware planning: pull pickups forward 24 h ahead of forecast storms", 0.40, "Force-majeure layover clause in customer contracts (rebill)", 0.35,
                "Reroute via southern corridor on winter lanes", 0.25, "DISPATCH", "Bulk reschedule via PUT /shipments/{id}/stops/{seq}/appointment"),
    "DRIVER_HOS": ("Verify driver available hours at dispatch; team drivers on >600 mi", 0.60, "Relay/slip-seat on long hauls", 0.50,
                   "Add 24H_PICKUP_CONF check with hours confirmation", 0.35, "DISPATCH", "PUT /shipments/{id}/milestones/24H_PICKUP_CONF"),
    "DISPATCH_ERROR": ("Generate rate con from facility master (facility_id) never free-text address", 0.85, "Dispatcher double-check checklist", 0.50,
                       "Driver app geofence confirmation", 0.60, "DISPATCH", "Use facility_id on stops; regenerate rate con"),
    "MISSED_APPOINTMENT": ("Carry 24H_DELIVERY_CONF + proactive re-appointment when ETA slips > slack", 0.50, "Book later appointment with buffer on long lanes", 0.35,
                           "Carrier on-time incentive / penalty clause", 0.30, "CSR", "PUT /shipments/{id}/stops/{seq}/appointment status=RESCHEDULED"),
    "CONSIGNEE_CLOSED": ("Validate ETA vs receiving_hours at dispatch; hold-back if after close", 0.60, "Rebill layover to consignee/customer (receiver-caused)", 0.45,
                         "Night-receiving / drop yard arrangements", 0.40, "CSR", "Compare event ETA with facility receiving_hours"),
    "DETENTION_DELIVERY": ("Rebill detention to customer with in/out times (accessorial schedule)", 0.55, "Drop-trailer at high-dwell DCs", 0.70,
                           "Facility scorecard: price dwell into quotes for slow DCs", 0.40, "ACCOUNTING", "POST /shipments/{id}/charges DET with check-call timestamps"),
    "LUMPER_OVERAGE": ("Pre-pay lumper via vendor (e.g. DOCKHANDS) at contracted rate", 0.80, "Cap reimbursement at facility lumper_fee_max", 0.50,
                       "Rebill overage with receipt", 0.40, "ACCOUNTING", "Vendor cost line LUM to DOCKHANDS"),
    "OTIF_CHARGEBACK": ("MABD buffer: deliver T-1 to MABD DCs; carriers >=93% OTD only", 0.65, "Dispute with carrier-caused evidence; pass 50% to carrier", 0.35,
                        "Negotiate OTIF exclusion for weather/receiver-caused", 0.25, "CSR", "Acknowledge MABD on appointment; track 24H_DELIVERY_CONF"),
    "REEFER_TEMP_EXCURSION": ("Require continuous temp telematics + pre-cool proof at pickup", 0.60, "Use reefer carriers with zero claims history", 0.45,
                              "Shorten dwell: no layovers on reefer, team on >600 mi", 0.30, "DISPATCH", "ON_HOLD reason=CLAIM; upload temp download as DOCUMENT"),
    "CARGO_DAMAGE": ("Load-securement spec (bars/straps) on rate con + photo at pickup", 0.50, "Carriers with zero claims for high-value freight", 0.40,
                     "Declared value / INSD coverage on high-value loads", 0.60, "DISPATCH", "Upload PHOTO docs; ON_HOLD reason=CLAIM"),
    "SHORTAGE": ("Seal + piece-count verification at pickup and BOL sign-off", 0.60, "Shipper-load-and-count clause on BOL", 0.40,
                 "Avoid cross-dock transfers for high-value freight", 0.30, "CSR", "Add SEAL reference; POD condition SHORTAGE triggers claim"),
    "LTL_REWEIGH_RECLASS": ("Dimensioner-verified density class at quote (density_pcf)", 0.65, "Rebill reclass to shipper per terms", 0.55,
                            "Move to volume LTL / partial TL above 6 pallets", 0.40, "SALES", "Use freight_class from density table at quote"),
    "PAPERWORK_HOLD": ("Driver app POD + lumper receipt capture before departure", 0.80, "Pay carrier only on complete docs (quick-pay incentive)", 0.60,
                       "Daily document-exceptions queue", 0.40, "ACCOUNTING", "POST /shipments/{id}/documents (POD, LUMPER_RECEIPT)"),
    "CUSTOMER_HOLD": ("Confirm ready date 24 h before covering", 0.50, "Bill TONU/storage per customer terms", 0.40, "-", 0.0,
                      "CSR", "ON_HOLD reason=CUSTOMER_REQUEST; cover only after release"),
    "TONU": ("Cancellation cutoff (T-24 h) in customer terms; bill TONU", 0.70, "Re-use truck on another load same day", 0.40, "-", 0.0,
             "SALES", "POST /shipments/{id}/cancel TONU + /charges TONU"),
}

# Best alternative carrier per equipment/mode (ACTIVE, GREEN, best OTD)
def best_alt_carrier(s):
    eq, mode = s.get("equipment_type"), s["mode"]
    cands = [v for v in vendors.values()
             if v["status"] == "ACTIVE" and v.get("compliance_light") == "GREEN" and v.get("on_time_delivery_pct")
             and mode in (v.get("service_types") or [mode]) and (not eq or eq in (v.get("equipment_types") or []))
             and v["vendor_id"] != s.get("carrier_id")]
    if mode == "LTL":
        cands = [v for v in cands if v["vendor_type"] == "LTL_CARRIER"]
    cands.sort(key=lambda v: (-(v["on_time_delivery_pct"]), v.get("claims_count", 0)))
    return cands[0] if cands else None

ship_by_id = {s["shipment_id"]: s for s in shipments}
net_by_event = defaultdict(float)
for l in lines:
    net_by_event[l["event_id"]] += l["net_cost"]

recs = []
for ev in events:
    s = ship_by_id[ev["shipment_id"]]
    sol = SOLUTIONS[ev["complication_type"]]
    net = round(net_by_event[ev["event_id"]], 2)
    alt = best_alt_carrier(s)
    cur = vendors.get(ev["carrier_id"] or "", {})
    carrier_note = ""
    if ev["root_cause_owner"] == "CARRIER" and alt:
        carrier_note = (f"Switch lane to {alt['vendor_id']} (OTD {alt['on_time_delivery_pct']}%, claims {alt.get('claims_count',0)})"
                        f" vs {ev['carrier_id']} (OTD {cur.get('on_time_delivery_pct','?')}%, {cur.get('compliance_light','?')})")
    if cur.get("status") == "DO_NOT_USE":
        carrier_note = f"COMPLIANCE: {ev['carrier_id']} is DO_NOT_USE — must not be tendered. " + carrier_note
    recs.append({
        "event_id": ev["event_id"], "root_event_id": ev["root_event_id"], "shipment_id": ev["shipment_id"],
        "complication_type": ev["complication_type"], "root_cause_owner": ev["root_cause_owner"],
        "net_cost": net,
        "primary_solution": sol[0], "primary_avoidable_pct": sol[1], "primary_savings_est": round(net * sol[1], 2),
        "alternative_1": sol[2], "alt1_avoidable_pct": sol[3], "alt1_savings_est": round(net * sol[3], 2),
        "alternative_2": sol[4], "alt2_avoidable_pct": sol[5], "alt2_savings_est": round(net * sol[5], 2),
        "carrier_alternative": carrier_note,
        "action_owner": sol[6], "tms_action": sol[7],
    })

# Shipment rollup
roll = {}
for s in shipments:
    roll[s["shipment_id"]] = {
        "shipment_id": s["shipment_id"], "status": s["status"], "mode": s["mode"], "customer_id": s["customer_id"],
        "carrier_id": s.get("carrier_id") or "", "pickup_date": s.get("pickup_date"), "lane": f'{s["shipper_state"]}>{s["consignee_state"]}',
        "sell_total": s.get("sell_total") or 0, "buy_total": s.get("buy_total") or 0, "gp_amount_tms": s.get("gp_amount") or 0,
        "event_count": 0, "root_events": 0, "delay_hours": 0.0,
        "carrier_accessorial": 0.0, "recovery_premium": 0.0, "customer_penalty": 0.0, "claim_loss": 0.0,
        "ops_labor": 0.0, "capital_and_writeoff": 0.0, "recoveries": 0.0, "net_complication_cost": 0.0,
    }
for ev in events:
    r = roll[ev["shipment_id"]]
    r["event_count"] += 1
    r["root_events"] += ev["parent_event_id"] == ""
    r["delay_hours"] += ev["delay_hours"] if ev["complication_type"] != "PAPERWORK_HOLD" else 0
CATMAP = {"CARRIER_ACCESSORIAL": "carrier_accessorial", "RECOVERY_PREMIUM": "recovery_premium", "CUSTOMER_PENALTY": "customer_penalty",
          "CLAIM_LOSS": "claim_loss", "DISPOSAL": "claim_loss", "OPS_LABOR": "ops_labor", "CAPITAL_COST": "capital_and_writeoff", "UNBILLABLE": "capital_and_writeoff"}
for l in lines:
    r = roll[l["shipment_id"]]
    if l["sign"] < 0:
        r["recoveries"] += l["amount"]
    else:
        r[CATMAP[l["cost_category"]]] += l["amount"]
    r["net_complication_cost"] += l["net_cost"]
for r in roll.values():
    # existing TMS DET/LAY/TONU lines are already inside buy_total; avoid double counting in adjusted GP
    already = sum(l["net_cost"] for l in lines if l["shipment_id"] == r["shipment_id"] and l["in_tms"])
    r["already_in_tms_net"] = round(already, 2)
    r["gp_after_complications"] = round(r["gp_amount_tms"] - (r["net_complication_cost"] - already), 2)
    r["gp_pct_after"] = round(r["gp_after_complications"] / r["sell_total"] * 100, 1) if r["sell_total"] else ""
    for k in ("delay_hours", "carrier_accessorial", "recovery_premium", "customer_penalty", "claim_loss", "ops_labor", "capital_and_writeoff", "recoveries", "net_complication_cost"):
        r[k] = round(r[k], 2)

# --------------------------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------------------------
def write_csv(name, rows, fields=None):
    fields = fields or list(rows[0].keys())
    with open(OUT / name, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"  {name}: {len(rows)} rows")

events.sort(key=lambda e: (e["event_at_utc"], e["event_id"]))
write_csv("delay_events.csv", events)
write_csv("delay_cost_lines.csv", lines)
write_csv("event_recommendations.csv", recs)
write_csv("shipment_cost_rollup.csv", sorted(roll.values(), key=lambda r: r["shipment_id"]))
write_csv("solutions_catalog.csv", [
    {"complication_type": k, "label": TYPES[k][0], "stage": TYPES[k][1], "late_reason_code": TYPES[k][2], "root_cause_owner": TYPES[k][4],
     "primary_solution": v[0], "primary_avoidable_pct": v[1], "alternative_1": v[2], "alt1_avoidable_pct": v[3],
     "alternative_2": v[4], "alt2_avoidable_pct": v[5], "action_owner": v[6], "tms_action": v[7]} for k, v in SOLUTIONS.items()])
write_csv("assumptions.csv", [{"key": k, "value": v[0], "note": v[1]} for k, v in A.items()]
          + [{"key": f"VALUE_PER_LB_{k}", "value": v, "note": "Cargo $/lb by customer commodity (assumed; declared_value used when present)"} for k, v in VALUE_PER_LB.items()])
with open(OUT / "api_payloads.jsonl", "w", encoding="utf-8") as fh:
    for p in payloads:
        fh.write(json.dumps(p) + "\n")
print(f"  api_payloads.jsonl: {len(payloads)} rows")
tot = sum(l["net_cost"] for l in lines)
print(f"Events {len(events)} on {len({e['shipment_id'] for e in events})} shipments; net complication cost ${tot:,.2f}")

