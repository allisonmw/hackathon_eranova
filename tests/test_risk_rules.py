from datetime import date

from meridian import risk_rules
from meridian.risk_rules import RiskContext

TODAY = date(2026, 11, 13)


def shipment(**kw):
    return {"shipment_id": "7300001", "status": "BOOKED", "pickup_date": "2026-11-14", "customer_id": "NORTHSTAR", **kw}


def ctx(**kw):
    return RiskContext(today=TODAY, **kw)


def kinds(items):
    return [(it.kind, it.severity) for it in items]


# --------------------------------------------------------------------------- uncovered_soon

def test_uncovered_soon_flags_booked_load_picking_up_tomorrow():
    items = risk_rules.uncovered_soon(ctx(shipments=[shipment()]))
    assert len(items) == 1
    assert items[0].kind == "UNCOVERED_SOON" and items[0].severity == "HIGH"
    assert items[0].evidence["days_until_pickup"] == 1


def test_uncovered_soon_ignores_far_pickups_and_covered_loads():
    c = ctx(shipments=[shipment(pickup_date="2026-11-20"), shipment(status="COVERED")])
    assert risk_rules.uncovered_soon(c) == []


# --------------------------------------------------------------------------- carrier_compliance

def carrier(**kw):
    return {"vendor_id": "ACME", "status": "ACTIVE", "authority_status": "ACTIVE",
            "insurance": [{"policy_type": "CARGO", "expiration_date": "2027-06-30", "insurance_status": "GREEN"}], **kw}


def covered(**kw):
    return shipment(**{"status": "COVERED", "carrier_id": "ACME", "delivery_date_target": "2026-11-16", **kw})


def test_compliance_clean_carrier_not_flagged():
    assert risk_rules.carrier_compliance(ctx(shipments=[covered()], vendors={"ACME": carrier()})) == []


def test_compliance_do_not_use_is_high_and_says_recover():
    items = risk_rules.carrier_compliance(ctx(shipments=[covered()],
                                              vendors={"ACME": carrier(status="DO_NOT_USE", status_reason="fraud")}))
    assert kinds(items) == [("CARRIER_COMPLIANCE", "HIGH")]
    assert "DO_NOT_USE" in items[0].title and items[0].suggested_action.startswith("Re-cover")


def test_compliance_expired_or_missing_cargo_is_high():
    expired = carrier(insurance=[{"policy_type": "CARGO", "expiration_date": "2026-11-01"}])
    no_cargo = carrier(insurance=[{"policy_type": "AUTO_LIABILITY", "expiration_date": "2027-01-01"}])
    for v in (expired, no_cargo):
        assert kinds(risk_rules.carrier_compliance(ctx(shipments=[covered()], vendors={"ACME": v}))) == [("CARRIER_COMPLIANCE", "HIGH")]


def test_compliance_cargo_expiring_before_delivery_is_high():
    v = carrier(insurance=[{"policy_type": "CARGO", "expiration_date": "2026-11-15"}])
    items = risk_rules.carrier_compliance(ctx(shipments=[covered()], vendors={"ACME": v}))
    assert kinds(items) == [("CARRIER_COMPLIANCE", "HIGH")]
    assert "before delivery" in items[0].title


def test_compliance_cargo_expiring_within_30_days_is_medium():
    v = carrier(insurance=[{"policy_type": "CARGO", "expiration_date": "2026-12-01"}])
    items = risk_rules.carrier_compliance(ctx(shipments=[covered()], vendors={"ACME": v}))
    assert kinds(items) == [("CARRIER_COMPLIANCE", "MEDIUM")]


def test_compliance_in_transit_suggests_certificate_not_recover():
    s = covered(status="IN_TRANSIT")
    items = risk_rules.carrier_compliance(ctx(shipments=[s], vendors={"ACME": carrier(authority_status="REVOKED")}))
    assert "on the road" in items[0].suggested_action


# --------------------------------------------------------------------------- missing_rate_con

def test_rate_con_missing_tl_pickup_tomorrow_is_high():
    items = risk_rules.missing_rate_con(ctx(shipments=[covered(mode="TL")]))
    assert kinds(items) == [("MISSING_RATE_CON", "HIGH")]


def test_rate_con_missing_far_pickup_is_medium():
    items = risk_rules.missing_rate_con(ctx(shipments=[covered(mode="TL", pickup_date="2026-11-20")]))
    assert kinds(items) == [("MISSING_RATE_CON", "MEDIUM")]


def test_rate_con_ignores_ltl_and_loads_that_have_one():
    c = ctx(shipments=[covered(mode="LTL"), covered(mode="TL", rate_con_doc_id="DOC-1")])
    assert risk_rules.missing_rate_con(c) == []


# --------------------------------------------------------------------------- tracking_overdue

def test_tracking_overdue_medium_and_high_when_delivering_today():
    q = [shipment(status="IN_TRANSIT", overdue=True, delivery_date_target="2026-11-15"),
         shipment(shipment_id="7300002", status="IN_TRANSIT", overdue=True, delivery_date_target="2026-11-13"),
         shipment(shipment_id="7300003", status="IN_TRANSIT", overdue=False, delivery_date_target="2026-11-13")]
    items = risk_rules.tracking_overdue(ctx(tracking_queue=q))
    assert kinds(items) == [("TRACKING_OVERDUE", "MEDIUM"), ("TRACKING_OVERDUE", "HIGH")]


# --------------------------------------------------------------------------- mabd_risk / appointment_needed

def delivery_stop(**kw):
    return {"stop_seq": 2, "stop_type": "DELIVERY", "facility_id": "BBR-DC135",
            "scheduled_window": {"earliest_local": "2026-11-16T08:00:00", "latest_local": "2026-11-16T12:00:00"},
            "appointment": {"status": "CONFIRMED"}, **kw}


FACILITIES = {"BBR-DC135": {"name": "BBR DC 135", "mabd_program": True, "mabd_program_note": "3% chargeback",
                            "scheduling_method": "PORTAL", "appointment_lead_time_hours": 48}}


def test_mabd_window_after_mabd_is_high_and_cites_note():
    s = covered(stops=[delivery_stop(mabd_date="2026-11-15")])
    items = risk_rules.mabd_risk(ctx(shipments=[s], facilities=FACILITIES))
    assert kinds(items) == [("MABD_RISK", "HIGH")]
    assert items[0].evidence["mabd_program_note"] == "3% chargeback"


def test_mabd_appointment_needed_high_if_close_medium_if_far():
    near = covered(stops=[delivery_stop(mabd_date="2026-11-16", appointment={"status": "NEEDED"})])
    far = covered(shipment_id="7300002", stops=[delivery_stop(
        mabd_date="2026-11-25", appointment={"status": "NEEDED"},
        scheduled_window={"earliest_local": "2026-11-24T08:00:00", "latest_local": "2026-11-24T12:00:00"})])
    items = risk_rules.mabd_risk(ctx(shipments=[near, far], facilities=FACILITIES))
    assert kinds(items) == [("MABD_RISK", "HIGH"), ("MABD_RISK", "MEDIUM")]


def test_mabd_confirmed_inside_window_not_flagged():
    s = covered(stops=[delivery_stop(mabd_date="2026-11-17")])
    assert risk_rules.mabd_risk(ctx(shipments=[s], facilities=FACILITIES)) == []


def test_appointment_needed_soon_is_flagged_and_far_is_not():
    near = covered(stops=[delivery_stop(appointment={"status": "NEEDED"})])
    far = covered(shipment_id="7300002", stops=[delivery_stop(
        appointment={"status": "NEEDED"},
        scheduled_window={"earliest_local": "2026-11-24T08:00:00", "latest_local": "2026-11-24T12:00:00"})])
    items = risk_rules.appointment_needed(ctx(shipments=[near, far]))
    assert [it.record_id for it in items] == ["7300001"]
    assert items[0].severity == "MEDIUM"


def test_appointment_inside_lead_time_is_high():
    s = covered(stops=[delivery_stop(appointment={"status": "NEEDED"},
                                     scheduled_window={"earliest_local": "2026-11-14T08:00:00",
                                                       "latest_local": "2026-11-14T12:00:00"})])
    items = risk_rules.appointment_needed(ctx(shipments=[s], facilities=FACILITIES))
    assert kinds(items) == [("APPOINTMENT_NEEDED", "HIGH")]
    assert "portal" in items[0].suggested_action


def test_appointment_on_mabd_stop_left_to_mabd_rule():
    s = covered(stops=[delivery_stop(mabd_date="2026-11-16", appointment={"status": "NEEDED"})])
    assert risk_rules.appointment_needed(ctx(shipments=[s])) == []


def test_pickup_appointment_ignored_after_pickup():
    stop = {"stop_seq": 1, "stop_type": "PICKUP", "appointment": {"status": "NEEDED"},
            "scheduled_window": {"earliest_local": "2026-11-12T08:00:00", "latest_local": "2026-11-12T12:00:00"}}
    s = covered(status="IN_TRANSIT", stops=[stop])
    assert risk_rules.appointment_needed(ctx(shipments=[s])) == []


# --------------------------------------------------------------------------- hold_reconfirm

def test_hold_with_pickup_today_flagged_but_post_pickup_hold_is_not():
    pre = shipment(status="ON_HOLD", held_from="COVERED", hold_reason="CUSTOMER_REQUEST", pickup_date="2026-11-13")
    post = shipment(shipment_id="7300002", status="ON_HOLD", held_from="IN_TRANSIT", hold_reason="CLAIM",
                    pickup_date="2026-11-10")
    future = shipment(shipment_id="7300003", status="ON_HOLD", held_from="BOOKED", pickup_date="2026-11-14")
    items = risk_rules.hold_reconfirm(ctx(shipments=[pre, post, future]))
    assert [it.record_id for it in items] == ["7300001"]


# --------------------------------------------------------------------------- quote_expiring

def test_quote_expiring_within_two_days_is_low():
    quotes = [{"quote_id": "Q1", "status": "SENT", "valid_until": "2026-11-14"},
              {"quote_id": "Q2", "status": "QUOTED", "valid_until": "2026-11-20"},
              {"quote_id": "Q3", "status": "ACCEPTED", "valid_until": "2026-11-13"}]
    items = risk_rules.quote_expiring(ctx(quotes=quotes))
    assert [(it.record_id, it.severity, it.record_type) for it in items] == [("Q1", "LOW", "quote")]


# --------------------------------------------------------------------------- low_margin

def test_low_margin_negative_is_high_and_below_floor_is_medium():
    loads = [shipment(mode="LTL", gp_pct=-4.0, sell_total=1000),
             shipment(shipment_id="7300002", mode="LTL", gp_pct=12.0, sell_total=1000),
             shipment(shipment_id="7300003", mode="LTL", gp_pct=20.0, sell_total=1000)]
    items = risk_rules.low_margin(ctx(shipments=loads, customers={"NORTHSTAR": {"margin_floor_pct": 18}}))
    assert kinds(items) == [("LOW_MARGIN", "HIGH"), ("LOW_MARGIN", "MEDIUM")]
    assert items[1].evidence["shortfall_to_floor"] == 60.0


def test_low_margin_uses_customer_floor():
    s = shipment(mode="LTL", gp_pct=20.0, sell_total=1000)
    items = risk_rules.low_margin(ctx(shipments=[s], customers={"NORTHSTAR": {"margin_floor_pct": 22}}))
    assert kinds(items) == [("LOW_MARGIN", "MEDIUM")]


def test_low_margin_tl_fuel_passthrough_not_flagged():
    """TL priced at the 18% floor: linehaul at 18%, fuel at cost, so blended GP is ~16.5%."""
    s = shipment(mode="TL", gp_pct=16.5, sell_total=2200,
                 vendor_costs=[{"charge_code": "FRT", "amount": 1640, "status": "CONFIRMED"},
                               {"charge_code": "FRT", "amount": 1500, "status": "CANCELLED"},
                               {"charge_code": "FSC", "amount": 200, "status": "CONFIRMED"}],
                 customer_charges=[{"charge_code": "FRT", "amount": 2000}, {"charge_code": "FSC", "amount": 200}])
    assert risk_rules.low_margin(ctx(shipments=[s])) == []


# --------------------------------------------------------------------------- scoring / run_all

def test_score_ranks_severity_first_then_urgency():
    assert risk_rules.score("HIGH", 10) > risk_rules.score("MEDIUM", 0)
    assert risk_rules.score("HIGH", 0) > risk_rules.score("HIGH", 5)


def test_run_all_runs_on_empty_context():
    assert risk_rules.run_all(RiskContext(today=TODAY)) == []


def test_run_all_items_validate_and_have_json_evidence():
    import json
    s = covered(mode="TL", gp_pct=-2.0, sell_total=1500, stops=[delivery_stop(mabd_date="2026-11-15")])
    items = risk_rules.run_all(ctx(shipments=[s], vendors={"ACME": carrier(status="INACTIVE")}, facilities=FACILITIES))
    assert {it.kind for it in items} == {"CARRIER_COMPLIANCE", "MISSING_RATE_CON", "MABD_RISK", "LOW_MARGIN"}
    for it in items:
        json.dumps(it.evidence)
