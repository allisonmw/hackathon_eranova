"""Meridian TMS API client. Owner: tools & rules lead.

Result shape (every function):
    {"ok": True, "data": ...}
    {"ok": False, "status": 409, "error": "conflict", "detail": "<sentence from the API>"}

List functions return `data` as a plain list of items (all pages when paginate=True).

Safety:
- Writes are DRY-RUN unless DRY_RUN=0. In dry-run they return
  {"ok": True, "dry_run": True, "would_send": {"method", "path", "body"}} and change nothing.
- DEMO_MODE=1 serves reads from snapshots/*.json (no network) and forces dry-run on writes.

Snapshot the live data (phase 0):   python -m meridian.client snapshot
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = ROOT / "snapshots"
load_dotenv(ROOT / ".env")

OPEN_STATUSES = "BOOKED,COVERED,DISPATCHED,AT_PICKUP,PICKED_UP,IN_TRANSIT,AT_DELIVERY,ON_HOLD"


def dry_run() -> bool:
    return os.environ.get("DRY_RUN", "1") != "0" or demo_mode()


def demo_mode() -> bool:
    return os.environ.get("DEMO_MODE", "0") == "1"


# --------------------------------------------------------------------------- transport

_api: httpx.Client | None = None


def _client() -> httpx.Client:
    global _api
    if _api is None:
        base, key = os.environ.get("TMS_API_BASE"), os.environ.get("TMS_API_KEY")
        if not base or not key:
            raise RuntimeError("TMS_API_BASE / TMS_API_KEY are not set: copy .env.example to .env and fill it in.")
        _api = httpx.Client(base_url=base, headers={"x-api-key": key}, timeout=30)
    return _api


def call(method: str, path: str, **kwargs) -> dict:
    """Call the API and always return a result dict, including for errors."""
    try:
        r = _client().request(method, path, **kwargs)
    except httpx.HTTPError as e:
        return {"ok": False, "status": None, "error": "network_error", "detail": str(e)}
    if r.status_code >= 400:
        try:
            body = r.json()
        except ValueError:
            body = {"error": r.text[:500]}
        # 409s carry a 'detail' sentence explaining which business rule blocked the call
        return {"ok": False, "status": r.status_code, "error": body.get("error"), "detail": body.get("detail")}
    return {"ok": True, "data": r.json()}


def _paginate(path: str, params: dict | None = None) -> dict:
    """Follow next_cursor until the last page; data = all items."""
    params = {**(params or {}), "limit": 200}
    items: list = []
    while True:
        res = call("GET", path, params=params)
        if not res["ok"]:
            return res
        items += res["data"]["items"]
        cursor = res["data"].get("next_cursor")
        if not cursor:
            return {"ok": True, "data": items}
        params["cursor"] = cursor


def _write(method: str, path: str, body: dict | None = None) -> dict:
    if dry_run():
        return {"ok": True, "dry_run": True, "would_send": {"method": method, "path": path, "body": body or {}}}
    return call(method, path, json=body or {})


# --------------------------------------------------------------------------- demo mode

def _snapshot(name: str) -> dict:
    path = SNAPSHOT_DIR / f"{name}.json"
    if not path.exists():
        return {"ok": False, "status": None, "error": "no_snapshot",
                "detail": f"{path.name} not found; run `python -m meridian.client snapshot` first."}
    return {"ok": True, "data": json.loads(path.read_text(encoding="utf-8")), "snapshot": True}


def _snapshot_find(name: str, key: str, value: str) -> dict:
    res = _snapshot(name)
    if not res["ok"]:
        return res
    for item in res["data"]:
        if str(item.get(key)) == str(value):
            return {"ok": True, "data": item, "snapshot": True}
    return {"ok": False, "status": 404, "error": "not_found", "detail": f"{key}={value} not in snapshot {name}."}


# --------------------------------------------------------------------------- reads

def dashboard_counts() -> dict:
    """{tiles: [{key, label, count, filter_route, alert}]}"""
    return _snapshot("dashboard_counts") if demo_mode() else call("GET", "/dashboard/counts")


def search(q: str) -> dict:
    """{references, customers, vendors, facilities}, each a list of {entity_type, entity_id, label, sublabel, route}."""
    if demo_mode():
        res = _snapshot("customers")
        if not res["ok"]:
            return res
        words = q.lower().split()
        hits = [{"entity_type": "CUSTOMER", "entity_id": c["customer_id"], "label": c.get("legal_name"), "sublabel": None, "route": None}
                for c in res["data"]
                if all(w in f"{c['customer_id']} {c.get('legal_name', '')} {c.get('dba_name') or ''}".lower() for w in words)]
        return {"ok": True, "data": {"references": [], "customers": hits[:10], "vendors": [], "facilities": []}, "snapshot": True}
    return call("GET", "/search", params={"q": q})


def list_customers() -> dict:
    return _snapshot("customers") if demo_mode() else _paginate("/customers")


def get_customer(customer_id: str) -> dict:
    """Includes margin_floor_pct and required_references[{ref_type, required_at}]."""
    if demo_mode():
        return _snapshot_find("customers", "customer_id", customer_id)
    return call("GET", f"/customers/{customer_id}")


def list_shipments(status: str = OPEN_STATUSES, paginate: bool = True) -> dict:
    if demo_mode():
        return _snapshot("open_shipments")
    if paginate:
        return _paginate("/shipments", {"status": status})
    res = call("GET", "/shipments", params={"status": status})
    return {**res, "data": res["data"]["items"]} if res["ok"] else res


def tracking_queue() -> dict:
    """Open shipments due for a check call; each row carries `overdue`."""
    if demo_mode():
        return _snapshot("tracking_queue")
    res = call("GET", "/shipments", params={"queue": "tracking", "limit": 200})
    return {**res, "data": res["data"]["items"]} if res["ok"] else res


def get_shipment(shipment_id: str) -> dict:
    if demo_mode():
        return _snapshot_find("open_shipments", "shipment_id", shipment_id)
    return call("GET", f"/shipments/{shipment_id}")


def get_shipment_events(shipment_id: str) -> dict:
    """Activity log for one shipment (check calls, status changes, notes, …)."""
    if demo_mode():
        res = _snapshot("shipment_events")
        return {**res, "data": res["data"].get(str(shipment_id), [])} if res["ok"] else res
    res = call("GET", f"/shipments/{shipment_id}/events")
    return {**res, "data": res["data"].get("items", res["data"])} if res["ok"] and isinstance(res["data"], dict) else res


def events_for(shipment_ids: list[str]) -> dict:
    """Events for many shipments: {"ok": True, "data": {shipment_id: [events]}, "failed": [ids]}."""
    out, failed = {}, []
    for sid in shipment_ids:
        res = get_shipment_events(sid)
        if res["ok"]:
            out[str(sid)] = res["data"]
        else:
            failed.append(str(sid))
    return {"ok": True, "data": out, "failed": failed}


def snapshot_taken_at() -> str | None:
    res = _snapshot("snapshot_meta")
    return res["data"].get("taken_at") if res["ok"] else None


def list_vendors() -> dict:
    """Carriers with compliance_light, status and insurance[]."""
    return _snapshot("vendors") if demo_mode() else _paginate("/vendors")


def get_vendor(vendor_id: str) -> dict:
    if demo_mode():
        return _snapshot_find("vendors", "vendor_id", vendor_id)
    return call("GET", f"/vendors/{vendor_id}")


def list_facilities() -> dict:
    """Facilities with mabd_program / mabd_program_note and appointment rules."""
    return _snapshot("facilities") if demo_mode() else _paginate("/facilities")


def get_facility(facility_id: str) -> dict:
    if demo_mode():
        return _snapshot_find("facilities", "facility_id", facility_id)
    return call("GET", f"/facilities/{facility_id}")


def list_quotes(status: str) -> dict:
    """status: QUOTED, SENT, EXPIRED, ..."""
    if demo_mode():
        return _snapshot(f"quotes_{status.lower()}")
    return _paginate("/quotes", {"status": status})


def get_quote(quote_id: str) -> dict:
    if demo_mode():
        for name in ("quotes_quoted", "quotes_sent"):
            res = _snapshot_find(name, "quote_id", quote_id)
            if res["ok"]:
                return res
        return {"ok": False, "status": 404, "error": "not_found", "detail": f"quote {quote_id} not in snapshots."}
    return call("GET", f"/quotes/{quote_id}")


# Pricing calls are POSTs but change nothing, so they are not dry-run gated.

def rate_engine_quote(body: dict) -> dict:
    """POST /rate-engine/quotes -> {rate_id, lines, buy_total, miles, market_context, ...}. 409 no rate, 422 missing data."""
    if demo_mode():
        return {"ok": False, "status": None, "error": "demo_mode", "detail": "Rate engine is not available in demo mode."}
    return call("POST", "/rate-engine/quotes", json=body)


def price_preview(body: dict) -> dict:
    """POST /quotes/price-preview -> {customer_charges, buy_total, sell_total, gp_amount, gp_pct}."""
    if demo_mode():
        return {"ok": False, "status": None, "error": "demo_mode", "detail": "Price preview is not available in demo mode."}
    return call("POST", "/quotes/price-preview", json=body)


# --------------------------------------------------------------------------- writes (dry-run by default)

def create_quote(body: dict) -> dict:
    """POST /quotes -> new DRAFT quote (201)."""
    return _write("POST", "/quotes", body)


def price_quote(quote_id: str, buy_lines: list[dict], target_margin_pct: float, rate_ref: str | None = None) -> dict:
    """POST /quotes/{id}/price: buy_lines exactly as the rate engine returned them."""
    body = {"buy_lines": buy_lines, "target_margin_pct": target_margin_pct}
    if rate_ref:
        body["rate_ref"] = rate_ref
    return _write("POST", f"/quotes/{quote_id}/price", body)


def send_quote(quote_id: str) -> dict:
    """Records that the quote was sent; nothing is emailed."""
    return _write("POST", f"/quotes/{quote_id}/send", {})


def add_shipment_note(shipment_id: str, note: str, visibility: str = "INTERNAL") -> dict:
    return _write("POST", f"/shipments/{shipment_id}/notes", {"note": note, "visibility": visibility})


def record_check_call(shipment_id: str, note: str, **fields) -> dict:
    """fields: city, state, eta, status_code, next_tracking_at, ... (see API_DOCS)."""
    return _write("POST", f"/shipments/{shipment_id}/check-calls", {"note": note, **fields})


# --------------------------------------------------------------------------- snapshots

def save_snapshots() -> None:
    """Save everything the load watch and demo mode need to snapshots/*.json."""
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    jobs = {
        "dashboard_counts": dashboard_counts,
        "open_shipments": list_shipments,
        "tracking_queue": tracking_queue,
        "vendors": list_vendors,
        "facilities": list_facilities,
        "customers": list_customers,
        "quotes_quoted": lambda: list_quotes("QUOTED"),
        "quotes_sent": lambda: list_quotes("SENT"),
    }
    for name, fn in jobs.items():
        res = fn()
        if not res["ok"]:
            print(f"  {name}: FAILED {res.get('status')} {res.get('detail')}")
            continue
        (SNAPSHOT_DIR / f"{name}.json").write_text(json.dumps(res["data"], indent=2), encoding="utf-8")
        n = len(res["data"]) if isinstance(res["data"], list) else "1"
        print(f"  {name}: {n}")
    # Activity logs for the open loads (check calls, status changes) for tracking.
    ships = json.loads((SNAPSHOT_DIR / "open_shipments.json").read_text(encoding="utf-8"))
    ev = events_for([s["shipment_id"] for s in ships])
    (SNAPSHOT_DIR / "shipment_events.json").write_text(json.dumps(ev["data"], indent=2), encoding="utf-8")
    print(f"  shipment_events: {sum(map(len, ev['data'].values()))} events for {len(ev['data'])} loads"
          + (f" (FAILED for {ev['failed']})" if ev["failed"] else ""))
    taken = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    (SNAPSHOT_DIR / "snapshot_meta.json").write_text(json.dumps({"taken_at": taken}, indent=2), encoding="utf-8")
    print(f"  snapshot_meta: taken_at {taken}")


if __name__ == "__main__":
    if sys.argv[1:] == ["snapshot"]:
        os.environ["DEMO_MODE"] = "0"
        print(f"Saving snapshots to {SNAPSHOT_DIR}")
        save_snapshots()
    else:
        print("Usage: python -m meridian.client snapshot")
