"""Pricing: rate engine -> price preview. Owner: tools & rules lead.

Rules (enforced here, never by the LLM):
- Margin is on sell: sell = buy / (1 - margin/100). Never price below the customer's margin_floor_pct.
- TL fuel (FSC per mile) passes through at cost, so blended GP lands slightly under target.
- Declared value adds a sell-only INSD line: 0.55% of declared value, minimum $25.
- buy_lines go to /price and /price-preview exactly as the rate engine returned them.
- Rate engine 409 (no rate) / 422 (missing weight or dims) come back as ok=False with the API's detail.
"""

from __future__ import annotations

from . import client
from .models import QuoteRequest

DEFAULT_MARGIN_FLOOR = 18.0   # API default when a customer has no margin_floor_pct
INSD_PCT = 0.55
INSD_MIN = 25.0


def sell_from_buy(buy: float, margin_pct: float) -> float:
    if not 0 <= margin_pct < 100:
        raise ValueError(f"margin_pct must be in [0, 100), got {margin_pct}")
    return round(buy / (1 - margin_pct / 100), 2)


def insurance_charge(declared_value: float | None) -> float:
    if not declared_value:
        return 0.0
    return round(max(declared_value * INSD_PCT / 100, INSD_MIN), 2)


def margin_floor(customer: dict | None) -> float:
    return float((customer or {}).get("margin_floor_pct") or DEFAULT_MARGIN_FLOOR)


def choose_margin(customer: dict | None, target: float | None = None) -> float:
    """The margin to price at: the customer's floor, or a higher target. Never lower."""
    floor = margin_floor(customer)
    return max(floor, target) if target is not None else floor


def _freight_lines(req: QuoteRequest) -> list[dict]:
    return [fl.model_dump(exclude_none=True) for fl in req.freight_lines if fl.weight_lb]


def rate_engine_body(req: QuoteRequest, miles: float | None = None) -> dict:
    body: dict = {
        "mode": req.mode,
        "origin": req.origin.model_dump(exclude_none=True),
        "destination": req.destination.model_dump(exclude_none=True),
    }
    if req.pickup_date:
        body["pickup_date"] = req.pickup_date.isoformat()
    if req.equipment_type:
        body["equipment_type"] = req.equipment_type
    if lines := _freight_lines(req):
        body["freight_lines"] = lines
    if req.accessorials:
        body["accessorials"] = req.accessorials
    if miles:
        body["miles"] = miles
    return body


def rate_lane(req: QuoteRequest, miles: float | None = None) -> dict:
    """Buy side for the lane: {"ok": True, "data": {rate_id, lines, buy_total, miles, ...}} or the API error."""
    return client.rate_engine_quote(rate_engine_body(req, miles))


def preview_price(buy_lines: list[dict], margin_pct: float, declared_value: float | None = None) -> dict:
    """Sell side at margin_pct: {"ok": True, "data": {customer_charges, buy_total, sell_total, gp_amount, gp_pct}}."""
    body: dict = {"buy_lines": buy_lines, "target_margin_pct": margin_pct}
    if declared_value:
        body["declared_value"] = declared_value
    return client.price_preview(body)


def frt_margin_pct(buy_lines: list[dict], sell_lines: list[dict]) -> float | None:
    """Margin on the linehaul (FRT) alone. For TL this is what must meet the floor, since fuel has no margin."""
    buy = sum(l.get("amount") or 0 for l in buy_lines if l.get("charge_code") == "FRT")
    sell = sum(l.get("amount") or 0 for l in sell_lines if l.get("charge_code") == "FRT")
    if not sell:
        return None
    return round((sell - buy) / sell * 100, 1)
