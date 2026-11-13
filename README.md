# Shipment delay & complication cost model (Meridian TMS)

Synthetic but realistic delay/complication data built on the real TMS snapshot (160 shipments,
18 vendors, 30 facilities, 14 customers). Every row points at real `shipment_id`, carrier `vendor_id`,
`facility_id` and `customer_id` values, and it uses the API's own codes: late reason, X12 status,
charge codes, hold reasons, POD condition, appointment status, and cost/charge line status.

## Run

```bash
python delay_sim/pull_tms_data.py            # read-only GETs -> data/raw/*.json
python delay_sim/generate_complications.py   # seeded simulation -> data/complications/*
python delay_sim/build_workbook.py           # -> Delay_Cost_Model.xlsx (formula-driven)
powershell -File delay_sim/recalc_excel.ps1  # optional: recalc in Excel + formula-error scan
```

## Outputs (`data/complications/`)

| File | What it is |
|---|---|
| `delay_events.csv` | One row per complication. `parent_event_id` / `root_event_id` link cascades (e.g. breakdown → missed appointment → OTIF fine). `source=TMS_EXISTING` rows match exceptions the TMS already records. |
| `delay_cost_lines.csv` | Each cost or recovery: `ledger` BUY/SELL (TMS money lines) or OFF_LEDGER (claims, OTIF, labor, capital). `sign` is +1 for a cost and −1 for a recovery. `in_tms=True` means the line is already on the TMS ledger. |
| `shipment_cost_rollup.csv` | Total complication cost per shipment, and GP after complications |
| `solutions_catalog.csv` | Each complication type: a main fix, 2 alternatives, the share of cost each one can avoid, the owner, and the TMS action to take |
| `event_recommendations.csv` | A recommended fix for each event, with estimated savings and a better carrier for the same equipment |
| `api_payloads.jsonl` | Ready-to-send `POST …/check-calls`, `…/costs`, `…/charges`, `…/status` bodies. **Nothing is posted.** |
| `assumptions.csv` | Every rate and cargo value used, each with its source |

## How costs are modelled

- **TMS charge-code rates:** DET $75/hr after 2 hours free, LAY $250, STOR $25/day, TONU $250, REWEIGH $35, APPT $50.
- **OTIF fines:** 3% of PO value at facilities with a MABD program (BBR, VMART, GP-DC5), taken from the facility notes. PO value comes from `declared_value`, or chargeable lb × an assumed $/lb for each customer.
- **Probability of each complication:** scaled by the carrier's on-time %, extra if the carrier is RED or DO_NOT_USE, by facility unload time and lumper policy, and by winter on northern lanes. An event only happens at stages the shipment has actually reached.
- **Claims:** under the Carmack rule the carrier is liable, and the carrier pays 85% of the time, less the deductible. When the carrier denies the claim, the customer settles for less.
- **Labor and cash:** ops labor at $42/hr. Invoices delayed by paperwork carry a 9% annual cost of capital.
