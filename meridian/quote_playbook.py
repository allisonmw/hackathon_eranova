"""Email-to-quote playbook. Owner: agent & evals lead.

build_proposal(email)  -> QuoteProposal   (no writes: extract, resolve, check, price, draft)
create_quote(proposal) -> result dict     (on Approve: create -> price -> verify)

`on_step(label)` lets the UI show progress in st.status.
"""

from __future__ import annotations

from datetime import date
from typing import Callable

from . import client, llm, pricing
from .models import Check, QuoteProposal, QuoteRequest

StepFn = Callable[[str], None]


def _noop(_: str) -> None:
    pass


# --------------------------------------------------------------------------- step 2: resolve customer

def resolve_customer(name: str | None) -> tuple[dict | None, list[dict]]:
    """Return (customer record or None, candidate hits). Picks the hit only when it is unambiguous."""
    if not name:
        return None, []
    res = client.search(name)
    if not res["ok"]:
        return None, []
    hits = res["data"].get("customers", [])
    if len(hits) != 1:
        return None, hits
    cust = client.get_customer(hits[0]["entity_id"])
    return (cust["data"] if cust["ok"] else None), hits


# --------------------------------------------------------------------------- step 3: checks

def missing_for_pricing(req: QuoteRequest) -> list[str]:
    missing = []
    if not req.mode:
        missing.append("mode (TL, LTL or expedite)")
    if not req.origin.state or not req.origin.city:
        missing.append("origin city and state")
    if not req.destination.state or not req.destination.city:
        missing.append("destination city and state")
    weighted = [fl for fl in req.freight_lines if fl.weight_lb]
    if req.mode in ("LTL", "EXPEDITE") and not weighted:
        missing.append("freight weight")
    if req.mode == "LTL" and weighted:
        has_dims = all(fl.length_in and fl.width_in and fl.height_in for fl in weighted)
        has_class = any(fl.freight_class for fl in weighted)
        if not (has_dims or has_class):
            missing.append("dimensions or freight class")
    return missing


def check_request(req: QuoteRequest, customer: dict | None, candidates: list[dict], today: date) -> tuple[list[str], list[Check]]:
    missing = missing_for_pricing(req)
    checks: list[Check] = []

    if customer:
        checks.append(Check(name="Customer found", passed=True, detail=f"{customer['customer_id']} ({customer.get('legal_name')})"))
    else:
        who = req.customer_name or "(no name in email)"
        options = ", ".join(h["entity_id"] for h in candidates[:5])
        checks.append(Check(name="Customer found", passed=False,
                            detail=f"No unique match for {who}" + (f"; candidates: {options}" if options else "")))
        missing.append("customer name")

    for ref in (customer or {}).get("required_references", []):
        if ref.get("required_at") not in (None, "QUOTE"):
            continue
        ref_type = ref.get("ref_type")
        ok = bool(req.references.get(ref_type))
        checks.append(Check(name=f"Required reference: {ref_type}", passed=ok,
                            detail=req.references.get(ref_type) or f"{customer['customer_id']} requires a {ref_type} to quote"))
        if not ok:
            missing.append(f"{ref_type} number")

    if req.pickup_date and req.pickup_date < today:
        checks.append(Check(name="Pickup date", passed=False, detail=f"{req.pickup_date} is in the past"))
        missing.append("a pickup date that is not in the past")

    checks.append(Check(name="Freight details complete", passed=not missing_for_pricing(req),
                        detail="; ".join(missing_for_pricing(req)) or "ok"))
    return missing, checks


# --------------------------------------------------------------------------- steps 1-6

def build_proposal(email: str, request: QuoteRequest | None = None, today: date | None = None,
                   target_margin: float | None = None, on_step: StepFn = _noop) -> QuoteProposal:
    """Pass `request` (the user's edited fields) to skip extraction and re-run checks, pricing and the draft."""
    today = today or date.today()

    if request is None:
        on_step("Reading email")
        request = llm.extract_quote_request(email, today)

    on_step("Finding customer")
    customer, candidates = resolve_customer(request.customer_name)

    on_step("Checking requirements")
    missing, checks = check_request(request, customer, candidates, today)

    proposal = QuoteProposal(request=request, customer_id=(customer or {}).get("customer_id"),
                             missing_fields=missing, checks=checks)

    if not missing_for_pricing(request):
        on_step("Pricing")
        _price(proposal, customer, target_margin)

    on_step("Drafting reply")
    proposal.draft_reply = llm.draft_quote_reply(proposal)
    proposal.ready_to_create = not proposal.missing_fields and all(c.passed for c in proposal.checks)
    return proposal


def _price(p: QuoteProposal, customer: dict | None, target_margin: float | None) -> None:
    rate = pricing.rate_lane(p.request)
    if not rate["ok"]:
        p.checks.append(Check(name="Rate found for lane", passed=False, detail=rate.get("detail") or str(rate.get("error"))))
        return
    margin = pricing.choose_margin(customer, target_margin)
    p.rate_id = rate["data"].get("rate_id")
    p.buy_lines = rate["data"]["lines"]
    p.margin_pct_used = margin

    prev = pricing.preview_price(p.buy_lines, margin, p.request.declared_value)
    if not prev["ok"]:
        p.checks.append(Check(name="Price preview", passed=False, detail=prev.get("detail") or str(prev.get("error"))))
        return
    d = prev["data"]
    p.sell_lines, p.buy_total, p.sell_total, p.gp_pct = d["customer_charges"], d["buy_total"], d["sell_total"], d["gp_pct"]
    p.checks.append(margin_check(p, pricing.margin_floor(customer)))


def margin_check(p: QuoteProposal, floor: float) -> Check:
    if p.gp_pct is not None and p.gp_pct >= floor:
        return Check(name="Margin floor", passed=True, detail=f"GP {p.gp_pct}% ≥ floor {floor}%")
    if p.request.mode == "TL":
        frt = pricing.frt_margin_pct(p.buy_lines, p.sell_lines)
        if frt is not None and frt >= floor - 0.1:
            return Check(name="Margin floor", passed=True,
                         detail=f"Linehaul GP {frt}% ≥ floor {floor}%; blended {p.gp_pct}% because TL fuel passes through at cost")
    return Check(name="Margin floor", passed=False, detail=f"GP {p.gp_pct}% is below the floor of {floor}%")


# --------------------------------------------------------------------------- steps 7-8 (on Approve)

def create_quote(p: QuoteProposal) -> dict:
    """Create -> price with the same buy lines and margin -> verify. Dry-run unless DRY_RUN=0."""
    if not p.ready_to_create:
        return {"ok": False, "detail": "Not ready to create: " + "; ".join(p.missing_fields or [c.detail for c in p.checks if not c.passed])}
    r = p.request
    body = {
        "customer_id": p.customer_id,
        "mode": r.mode,
        "origin": r.origin.model_dump(exclude_none=True),
        "destination": r.destination.model_dump(exclude_none=True),
        "freight_lines": [fl.model_dump(exclude_none=True) for fl in r.freight_lines if fl.weight_lb],
        "target_margin_pct": p.margin_pct_used,
    }
    if r.equipment_type:
        body["equipment_type"] = r.equipment_type
    if r.pickup_date:
        body["pickup_date"] = r.pickup_date.isoformat()
    if r.declared_value:
        body["declared_value"] = r.declared_value
    if r.references:
        body["contact"] = {"references": r.references}  # quotes have no references field; TODO confirm where these belong

    created = client.create_quote(body)
    if not created["ok"] or created.get("dry_run"):
        return created
    quote_id = created["data"]["quote_id"]

    priced = client.price_quote(quote_id, p.buy_lines, p.margin_pct_used, rate_ref=p.rate_id)
    if not priced["ok"]:
        return {**priced, "quote_id": quote_id}

    # Verify: re-read and confirm, never trust the write's response alone.
    got = client.get_quote(quote_id)
    if not got["ok"]:
        return {**got, "quote_id": quote_id}
    q = got["data"]
    floor = p.margin_pct_used or pricing.DEFAULT_MARGIN_FLOOR
    verified = q.get("status") == "QUOTED" and margin_check(p.model_copy(update={"gp_pct": q.get("gp_pct")}), floor).passed
    return {"ok": True, "quote_id": quote_id, "status": q.get("status"), "gp_pct": q.get("gp_pct"), "verified": verified, "data": q}


def mark_sent(quote_id: str) -> dict:
    return client.send_quote(quote_id)
