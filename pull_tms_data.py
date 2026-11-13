"""Snapshot the Meridian TMS dataset (read-only GETs) into delay_sim/data/raw/.

Credentials come from TMS_API_BASE / TMS_API_KEY env vars, falling back to packet/secret.txt.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data" / "raw"
RAW.mkdir(parents=True, exist_ok=True)


def creds():
    base, key = os.environ.get("TMS_API_BASE"), os.environ.get("TMS_API_KEY")
    if base and key:
        return base.rstrip("/"), key
    txt = (ROOT.parent / "packet" / "secret.txt").read_text(encoding="utf-8")
    base = re.search(r"Base URL:\s*(\S+)", txt).group(1)
    key = re.search(r"API key:\s*(\S+)", txt).group(1)
    return base.rstrip("/"), key


BASE, KEY = creds()
S = requests.Session()
S.headers["x-api-key"] = KEY


def get(path, **params):
    for attempt in range(4):
        r = S.get(f"{BASE}{path}", params=params, timeout=30)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def get_all(path, **params):
    items, cursor = [], None
    while True:
        p = dict(params, limit=200)
        if cursor:
            p["cursor"] = cursor
        page = get(path, **p)
        items.extend(page.get("items", []))
        cursor = page.get("next_cursor")
        if not cursor:
            return items
        time.sleep(0.2)


def save(name, obj):
    (RAW / f"{name}.json").write_text(json.dumps(obj, indent=2), encoding="utf-8")
    n = len(obj) if isinstance(obj, list) else 1
    print(f"  {name}: {n}")


def main():
    print(f"Pulling from {BASE}")
    save("charge_codes", get("/charge-codes"))
    save("fsc", get("/fsc"))
    save("customers", get_all("/customers"))
    save("vendors", get_all("/vendors"))
    save("facilities", get_all("/facilities"))
    rows = get_all("/shipments")
    save("shipments_list", rows)
    full = []
    for i, row in enumerate(rows, 1):
        full.append(get(f"/shipments/{row['shipment_id']}"))
        time.sleep(0.1)  # gentle: the guide asks us not to load-test
        if i % 25 == 0:
            print(f"    detail {i}/{len(rows)}")
    save("shipments", full)


if __name__ == "__main__":
    sys.exit(main())
