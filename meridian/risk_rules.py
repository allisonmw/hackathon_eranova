"""Risk detectors: pure functions, easy to unit test. Owner: tools & rules lead.

Each rule takes a RiskContext and returns list[RiskItem], with `evidence` holding the exact facts
it used (so the LLM explanation and the UI evidence table can only cite those).

Field names to double-check against the reset data (see the plan): rate_con_doc_id / rate_con_number,
stops[].appointment.status, stops[].mabd_date, stops[].scheduled_window.latest_local,
insurance[].insurance_status, and the tracking queue's `overdue`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .models import RiskItem, Severity


@dataclass
class RiskContext:
    today: date
    shipments: list[dict] = field(default_factory=list)         # open shipments (all pages)
    tracking_queue: list[dict] = field(default_factory=list)    # rows carry `overdue`
    vendors: dict[str, dict] = field(default_factory=dict)      # by vendor_id
    facilities: dict[str, dict] = field(default_factory=dict)   # by facility_id
    customers: dict[str, dict] = field(default_factory=dict)    # by customer_id
    quotes: list[dict] = field(default_factory=list)            # QUOTED + SENT


# --------------------------------------------------------------------------- helpers

SEVERITY_BASE = {"HIGH": 300.0, "MEDIUM": 200.0, "LOW": 100.0}


def score(severity: Severity, days_until: int | None = None, money_at_risk: float = 0.0) -> float:
    """Severity first, then sooner is more urgent, then money. Higher = more urgent."""
    s = SEVERITY_BASE[severity]
    if days_until is not None:
        s += max(0, 30 - max(days_until, 0)) * 3     # up to +90 for today/past
    s += min(money_at_risk / 1000, 9.0)              # tie-breaker, up to +9
    return round(s, 2)


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value[:10])


def days_until(value: str | None, today: date) -> int | None:
    d = parse_date(value)
    return (d - today).days if d else None


def _shipment_item(s: dict, **kw) -> RiskItem:
    return RiskItem(record_type="shipment", record_id=str(s["shipment_id"]), **kw)


# --------------------------------------------------------------------------- rules

def uncovered_soon(ctx: RiskContext) -> list[RiskItem]:
    """BOOKED (no carrier) and pickup within 2 days -> HIGH. (Worked example; copy this pattern.)"""
    items = []
    for s in ctx.shipments:
        if s.get("status") != "BOOKED":
            continue
        d = days_until(s.get("pickup_date"), ctx.today)
        if d is None or d > 2:
            continue
        items.append(_shipment_item(
            s,
            kind="UNCOVERED_SOON",
            severity="HIGH",
            score=score("HIGH", d, s.get("sell_total") or 0),
            title="No carrier, pickup in ≤2 days" if d >= 0 else "No carrier, pickup date passed",
            evidence={"status": s["status"], "pickup_date": s.get("pickup_date"), "days_until_pickup": d,
                      "customer_id": s.get("customer_id"),
                      "lane": f"{s.get('shipper_city')},{s.get('shipper_state')} → {s.get('consignee_city')},{s.get('consignee_state')}"},
            suggested_action="Find a carrier",
        ))
    return items


def carrier_compliance(ctx: RiskContext) -> list[RiskItem]:
    """Carrier not ACTIVE / DO_NOT_USE / no CARGO policy / cargo RED -> HIGH; cargo YELLOW -> MEDIUM."""
    return []  # TODO(tools lead)


def missing_rate_con(ctx: RiskContext) -> list[RiskItem]:
    """COVERED with no generated rate confirmation (rate_con_doc_id) -> HIGH if pickup ≤1 day, else MEDIUM."""
    return []  # TODO(tools lead)


def tracking_overdue(ctx: RiskContext) -> list[RiskItem]:
    """In the tracking queue with overdue=true -> MEDIUM (HIGH if delivering today)."""
    return []  # TODO(tools lead)


def mabd_risk(ctx: RiskContext) -> list[RiskItem]:
    """Stop has mabd_date and appointment NEEDED, or window ends after MABD; cite facility mabd_program_note -> HIGH."""
    return []  # TODO(tools lead)


def appointment_needed(ctx: RiskContext) -> list[RiskItem]:
    """Appointment NEEDED with pickup or delivery within 3 days -> MEDIUM."""
    return []  # TODO(tools lead)


def hold_reconfirm(ctx: RiskContext) -> list[RiskItem]:
    """ON_HOLD with pickup date today or past -> MEDIUM."""
    return []  # TODO(tools lead)


def quote_expiring(ctx: RiskContext) -> list[RiskItem]:
    """QUOTED/SENT with valid_until within 2 days -> LOW. (Cut-list item.)"""
    return []  # TODO(tools lead)


def low_margin(ctx: RiskContext) -> list[RiskItem]:
    """Open load gp_pct below the customer's floor -> MEDIUM; negative -> HIGH. (Cut-list item.)"""
    return []  # TODO(tools lead)


ALL_RULES = [
    carrier_compliance,
    missing_rate_con,
    uncovered_soon,
    tracking_overdue,
    mabd_risk,
    appointment_needed,
    hold_reconfirm,
    quote_expiring,
    low_margin,
]


def run_all(ctx: RiskContext) -> list[RiskItem]:
    items: list[RiskItem] = []
    for rule in ALL_RULES:
        items += rule(ctx)
    return items
