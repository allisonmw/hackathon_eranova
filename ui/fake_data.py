"""Realistic fake data so the UI can be finished before the playbooks are. Owner: UI lead."""

from __future__ import annotations

from datetime import date, timedelta

from meridian.models import Check, FreightLine, Location, QuoteProposal, QuoteRequest, RiskItem


def fake_dashboard_tiles() -> list[dict]:
    return [
        {"key": "to_cover", "label": "To cover", "count": 6, "alert": False},
        {"key": "dispatched", "label": "Dispatched", "count": 11, "alert": False},
        {"key": "in_transit", "label": "In transit", "count": 14, "alert": False},
        {"key": "delivering_today", "label": "Delivering today", "count": 5, "alert": False},
        {"key": "tracking_overdue", "label": "Tracking overdue", "count": 4, "alert": True},
        {"key": "quotes_pending", "label": "Quotes pending", "count": 9, "alert": False},
    ]


def fake_risk_items() -> list[RiskItem]:
    today = date.today()
    return [
        RiskItem(kind="CARRIER_COMPLIANCE", severity="HIGH", score=389.0, record_type="shipment", record_id="7300172",
                 title="Carrier cargo insurance expired",
                 evidence={"carrier_id": "ACMECARR", "policy_type": "CARGO", "insurance_status": "RED",
                           "expiration_date": str(today - timedelta(days=3)), "shipment_status": "COVERED",
                           "pickup_date": str(today + timedelta(days=1))},
                 suggested_action="Re-cover with a compliant carrier",
                 explanation="ACMECARR's cargo policy expired 3 days ago and they pick up tomorrow, so this load is uninsured unless re-covered.",
                 draft_message="Hi ACMECARR team, our records show your cargo insurance expired. Please send an updated certificate of insurance today, or we'll need to re-cover load 7300172.",
                 draft_audience="carrier"),
        RiskItem(kind="MABD_RISK", severity="HIGH", score=384.0, record_type="shipment", record_id="7300181",
                 title="Must-arrive-by delivery with no appointment",
                 evidence={"facility_id": "BBR-DC135", "mabd_date": str(today + timedelta(days=2)), "appointment_status": "NEEDED",
                           "mabd_program_note": "3% chargeback on invoice for deliveries after MABD"},
                 suggested_action="Book the appointment before the MABD",
                 explanation="BBR-DC135 charges back 3% for late deliveries and this load has no appointment two days before its must-arrive-by date.",
                 draft_message="Internal: book the delivery appointment at BBR-DC135 for 7300181 before the MABD to avoid the 3% chargeback.",
                 draft_audience="internal"),
        RiskItem(kind="MISSING_RATE_CON", severity="HIGH", score=381.0, record_type="shipment", record_id="7300190",
                 title="Covered but no rate confirmation",
                 evidence={"status": "COVERED", "rate_con_doc_id": None, "pickup_date": str(today + timedelta(days=1)), "carrier_id": "BLUELINE"},
                 suggested_action="Generate the rate confirmation",
                 explanation="7300190 is covered by BLUELINE but has no generated rate confirmation, so it can't be dispatched for tomorrow's pickup.",
                 draft_message="Hi BLUELINE dispatch, the rate confirmation for load 7300190 is on its way; please sign and return it today so we can dispatch.",
                 draft_audience="carrier"),
        RiskItem(kind="TRACKING_OVERDUE", severity="MEDIUM", score=287.0, record_type="shipment", record_id="7300166",
                 title="Check call overdue",
                 evidence={"status": "IN_TRANSIT", "next_tracking_at": f"{today}T08:00:00Z", "overdue": True},
                 suggested_action="Make a check call",
                 explanation="7300166 is in transit and its check call was due at 08:00 UTC today.",
                 draft_message="Hi, can you send the current location and ETA for load 7300166?",
                 draft_audience="carrier"),
        RiskItem(kind="QUOTE_EXPIRING", severity="LOW", score=187.0, record_type="quote", record_id="7300201",
                 title="Quote expires in 2 days",
                 evidence={"status": "SENT", "valid_until": str(today + timedelta(days=2)), "customer_id": "REDROCKEL", "sell_total": 2840.0},
                 suggested_action="Follow up with the customer",
                 explanation="The $2,840 quote sent to REDROCKEL expires in two days with no answer yet.",
                 draft_message="Hi, just following up on quote 7300201 ($2,840, valid through Friday). Would you like us to book it?",
                 draft_audience="customer"),
    ]


def fake_proposal(has_po: bool = False) -> QuoteProposal:
    req = QuoteRequest(
        customer_name="Northstar", mode="TL", equipment_type="V",
        origin=Location(city="Chicago", state="IL"), destination=Location(city="Dallas", state="TX"),
        pickup_date=date.today() + timedelta(days=3),
        freight_lines=[FreightLine(pieces=22, weight_lb=38000, description="Packaged goods")],
        references={"PO": "45812"} if has_po else {},
    )
    checks = [
        Check(name="Customer found", passed=True, detail="NORTHSTAR (Northstar Foods Inc.)"),
        Check(name="Required reference: PO", passed=has_po, detail="45812" if has_po else "NORTHSTAR requires a PO to quote"),
        Check(name="Freight details complete", passed=True, detail="ok"),
        Check(name="Margin floor", passed=True, detail="Linehaul GP 18.0% ≥ floor 18%; blended 15.4% because TL fuel passes through at cost"),
    ]
    buy = [{"charge_code": "FRT", "description": "Linehaul", "rate_basis": "FLAT", "quantity": 1, "rate": 2350.0, "amount": 2350.0},
           {"charge_code": "FSC", "description": "Fuel surcharge", "rate_basis": "PER_MILE", "quantity": 925, "rate": 0.52, "amount": 481.0}]
    sell = [{"charge_code": "FRT", "description": "Linehaul", "rate_basis": "FLAT", "quantity": 1, "rate": 2865.85, "amount": 2865.85},
            {"charge_code": "FSC", "description": "Fuel surcharge", "rate_basis": "PER_MILE", "quantity": 925, "rate": 0.52, "amount": 481.0}]
    missing = [] if has_po else ["PO number"]
    reply = ("Thanks for the request. Our all-in rate for Chicago, IL → Dallas, TX is $3,346.85, valid for 7 days.\n\nMeridian Logistics"
             if has_po else
             "Thanks for the request. Before we can confirm a rate for Chicago, IL → Dallas, TX, could you send the PO number for this load?\n\nMeridian Logistics")
    return QuoteProposal(request=req, customer_id="NORTHSTAR", missing_fields=missing, checks=checks,
                         buy_total=2831.0, sell_total=3346.85, gp_pct=15.4, margin_pct_used=18.0,
                         buy_lines=buy, sell_lines=sell, rate_id="RE-FAKE-0001", draft_reply=reply,
                         ready_to_create=has_po)
