# Delay Cost Exposure — prototype handoff

This is a focused panel within **Live tracking**, not a standalone Freight Rewind app or a quotation tool. Work is on `feat/tracking-cost-estimates`, created directly from `origin/feat/tracking-feed` at `fa0b072c39cb8fe00ac65d2e1ccb0f5bf7fcd0e1`. Nothing from the old Freight Rewind branch was merged.

## Inspection and reuse

- `ui/tracking.py` already loads snapshots without credentials, selects a shipment via `track_selected`, and renders the live map, ETA/status metrics, event feed, timeline, and check-call form. All are preserved.
- The panel receives the existing `Tracker` and selected shipment ID. It reads `TrackingStatus.eta`, `minutes_late`, `minutes_late_mabd`, window/MABD deadlines, time zone, shipment financial totals, carrier/customer IDs, and the delivery facility policy note. It does not change tracking estimates or shared models.
- `fake_data` at `b76d73dd85b0546be95d226e2444a0017fc73533` contains `generate_complications.py`, `build_workbook.py`, `Delay_Cost_Model.xlsx`, and the read-only pull/recalculation scripts. Its documented `data/complications/*.csv` outputs and raw JSON inputs are **not committed**. It is not an importable tracking module; importing its generator would execute file-dependent simulation logic.
- The committed workbook is an available generated output: 123 events plus cost lines and formula-driven rollups. Its events match **9 events across 6 of the 27 currently tracked shipment IDs**. `ui/delay_cost_examples.json` preserves only those matched event reference fields and provenance. These historical synthetic rows are never used as current charges or to activate a cost. Workbook labels such as CONFIRMED/TMS_EXISTING are not treated as new verified TMS evidence.
- Workbook arithmetic uses quantity × rate and sign, and subtracts amounts marked `in_tms` before adjusting baseline GP. The prototype reuses that reconciliation principle with explicit event-scope confirmation, not a blind join on charge code.

## Model assumptions

The following values were checked against both the generator's `A` dictionary and workbook `Assumptions` sheet:

| Input | Illustrative default | Provenance / limitation |
|---|---|---|
| Detention | $75/hour, after 2 free hours | Model cites TMS DET defaults; applies to excess facility dwell, not late linehaul arrival |
| Layover | $250/night | Model cites LAY; requires an additional overnight wait |
| Rescheduling | $50 | Model cites APPT; requires explicit rescheduling |
| Storage | $25/day | Model cites STOR; only explicitly assumed storage days |
| Operations labor | $42/hour | Synthetic loaded labor-cost assumption |
| OTIF / MABD | 3% of applicable order value | Example facility-policy assumption, not a universal penalty |
| Delayed billing | 9% annual capital cost | Synthetic financing assumption; days × blocked invoice × annual percent / 36500 |

All rates are editable, all categories start inactive, and no probabilities or automatic chain of downstream costs are introduced. Additional delay is a what-if shift of the existing tracking ETA/lateness; it never activates dwell, layover or penalties. For an arrived shipment this is a counterfactual arrival scenario, not a new live ETA.

Penalty calculation requires an explicitly confirmed/assumed applicable policy, a qualifying MABD violation or explicit hypothetical violation, and a known/entered order value. Missing values remain unknown; freight sell price and guessed cargo dollars/lb are never substituted for order value.

## Accounting and uncertainty

`ui/delay_cost_model.py` contains deterministic Decimal arithmetic, rounding dollar results to cents. Gross additional cost sums reconciled expense lines. Net exposure = gross additional cost − additional potential customer pass-through. Scenario GP = baseline GP − net exposure. Baseline GP uses the TMS `gp_amount` or known `sell_total - buy_total`; missing values do not become zero. GP after off-ledger labor/capital is economic impact, not a posted TMS accounting margin.

For a matching non-cancelled vendor/customer ledger code (including ESTIMATED lines), the user must select:

- **Unconfirmed:** positive additional exposure remains unknown.
- **Same event total:** input is the full event amount; additional = max(0, modeled total − already recorded). Existing costs/recoveries are not charged/recovered twice. No refund is inferred for a smaller modeled amount.
- **Separate incremental event:** user explicitly states the full input is additional to existing ledger lines.

A code match alone cannot prove event identity. Multiple nonzero same-code entries on the same modeled ledger are blocked pending combination into one entry. Detention plus layover requires confirmation that the waiting periods do not overlap. Other off-ledger event matching and responsibility still need human review. Gross/net totals are unknown if any selected additional amount or ledger reconciliation is unknown; no incomplete subtotal is labeled as a full exposure figure.

Potential customer pass-through is optional and never assumed collectible. Existing same-event customer charges are excluded from new recoveries. Carrier reimbursement/claims and vendor-ledger credits require a future dedicated recovery adapter; this slice does not infer them. Recovery exceeding costs produces a warning, not booked profit.

## Owned changes

- `app.py`: force DRY_RUN on for the prototype, including existing check-call actions.
- `ui/tracking.py`: import the new panel and call it below the selected shipment's existing timeline/check-call section.
- `ui/delay_costs.py`: selected-shipment UI, conditional controls, breakdown, evidence, mitigation suggestions.
- `ui/delay_cost_model.py`: pure UI-owned prototype cost arithmetic and ledger reconciliation.
- `ui/delay_cost_examples.json`: read-only, labeled workbook references.
- `ui/test_delay_costs.py`: model and Streamlit regression tests.

No changes to `meridian/`, shared contracts, API clients, existing tests, snapshots, dependency files, or generator/workbook code. No branch merges, commits or pushes are performed for this feature before review.

## Run and test

From the repository root on Linux/cloud:

```bash
MODEL=fake DRY_RUN=1 .venv/bin/python -m streamlit run app.py --server.address 0.0.0.0 --server.port 8504 --server.headless true
.venv/bin/python -m pytest -q tests ui/test_delay_costs.py
```

Open the cloud browser at http://localhost:8504, keep **Fake data** selected and open **Live tracking**. No API credentials are required. The map layer/tile provider may require internet access; the committed shipment snapshots and cost engine do not. In a fresh checkout, create a virtual environment and install `requirements.txt` first.

Demo: select **Excess loading / unloading dwell**, enter 5 total hours, retain 2 free hours and $75/hour. With no same-event costs already recorded, additional cost is $225. If a matching DET line exists, reconcile it before treating the full $225 as additional. Changing only hypothetical linehaul delay adds no charge. Selecting another load updates the panel and its inputs without a second shipment selector.

## Remaining coordinated integration

Backend owners should confirm contractual rates/free time, event/stop IDs on ledger lines, posted-vs-estimated scope and timestamps, actual dwell, applicable OTIF policy and order value, responsibility, and authorized/collectible recoveries. Customer/carrier display-name enrichment is not fetched. No recovery-write, cost-write, policy inference, external CSV upload, or additional dependency is introduced. The workbook references are static; updating the synthetic model requires refreshing the documented fixture, not importing the generator into the application.

## Validation performed

All **76 tests passed**, including the existing backend/tracking suite and UI-owned model/integration tests. Tests cover zero modeled charges, detention free time, late-arrival-only scenarios, policy/value requirements, unknown amounts, same-event cost/recovery reconciliation, duplicate-code blocking, shipment selection changes, all optional controls, credential-free fake mode, and preservation of the map/timeline/check-call components. A Chromium browser check verified selection of the dwell scenario, $225 for five hours at the default terms, slider-driven context updates without extra automatic costs, the original tabs, and zero JavaScript errors or application exception panels. Real API calls and live contractual policies were not exercised.
