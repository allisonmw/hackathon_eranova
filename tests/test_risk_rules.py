from datetime import date

from meridian import risk_rules
from meridian.risk_rules import RiskContext

TODAY = date(2026, 11, 13)


def shipment(**kw):
    return {"shipment_id": "7300001", "status": "BOOKED", "pickup_date": "2026-11-14", "customer_id": "NORTHSTAR", **kw}


def test_uncovered_soon_flags_booked_load_picking_up_tomorrow():
    items = risk_rules.uncovered_soon(RiskContext(today=TODAY, shipments=[shipment()]))
    assert len(items) == 1
    assert items[0].kind == "UNCOVERED_SOON" and items[0].severity == "HIGH"
    assert items[0].evidence["days_until_pickup"] == 1


def test_uncovered_soon_ignores_far_pickups_and_covered_loads():
    ctx = RiskContext(today=TODAY, shipments=[shipment(pickup_date="2026-11-20"), shipment(status="COVERED")])
    assert risk_rules.uncovered_soon(ctx) == []


def test_score_ranks_severity_first_then_urgency():
    assert risk_rules.score("HIGH", 10) > risk_rules.score("MEDIUM", 0)
    assert risk_rules.score("HIGH", 0) > risk_rules.score("HIGH", 5)


def test_run_all_runs_on_empty_context():
    assert risk_rules.run_all(RiskContext(today=TODAY)) == []
