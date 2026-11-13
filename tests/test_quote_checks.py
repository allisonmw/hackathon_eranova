from datetime import date

from meridian.models import FreightLine, Location, QuoteRequest
from meridian.quote_playbook import check_request, missing_for_pricing

TODAY = date(2026, 11, 13)
NORTHSTAR = {"customer_id": "NORTHSTAR", "legal_name": "Northstar", "margin_floor_pct": 18,
             "required_references": [{"ref_type": "PO", "required_at": "QUOTE"}]}


def tl(**kw):
    return QuoteRequest(mode="TL", origin=Location(city="Chicago", state="IL"),
                        destination=Location(city="Dallas", state="TX"), **kw)


def test_required_po_missing_blocks_and_is_asked_for():
    missing, checks = check_request(tl(), NORTHSTAR, [], TODAY)
    assert "PO number" in missing
    assert any(c.name == "Required reference: PO" and not c.passed for c in checks)


def test_required_po_present_passes():
    missing, checks = check_request(tl(references={"PO": "45812"}), NORTHSTAR, [], TODAY)
    assert missing == []
    assert all(c.passed for c in checks)


def test_unknown_customer_is_missing():
    missing, _ = check_request(tl(customer_name="Nobody Inc"), None, [], TODAY)
    assert "customer name" in missing


def test_ltl_needs_dims_or_class():
    req = QuoteRequest(mode="LTL", origin=Location(city="Denver", state="CO"), destination=Location(city="Phoenix", state="AZ"),
                       freight_lines=[FreightLine(pieces=3, weight_lb=1450)])
    assert missing_for_pricing(req) == ["dimensions or freight class"]
    req.freight_lines[0].freight_class = 70
    assert missing_for_pricing(req) == []


def test_past_pickup_date_is_flagged():
    missing, _ = check_request(tl(references={"PO": "1"}, pickup_date=date(2026, 11, 1)), NORTHSTAR, [], TODAY)
    assert "a pickup date that is not in the past" in missing
