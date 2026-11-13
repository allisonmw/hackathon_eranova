# Freight Rewind — UI prototype and backend handoff

Freight Rewind is a dedicated recovery simulator alongside the existing Broker Desk Copilot views. Ownership is limited to `app.py` and `ui/`. No backend contracts or API write behavior were edited. The entry point forces `DRY_RUN=1` for all views in this UI branch.

## Run / preview

From the repository root:

```bash
source .venv/bin/activate
MODEL=fake DRY_RUN=1 streamlit run app.py --server.address 0.0.0.0 --server.port 8501 --server.headless true --theme.base dark
```

In the Codex cloud Desktop browser, visit `http://localhost:8501`. In clients with a forwarded Ports/Preview panel, open port 8501. Localhost on your own laptop is a different machine unless that port is forwarded. No public deployment is created by running the app.

Choose Fake data for an entirely offline demo. Live API requires TMS credentials configured separately; do not commit `.env`. Snapshots require the tools lead's snapshot files. Switching into these sources does not fetch until **Load shipments (read only)** is clicked.

## Implemented and interactive

- Shipment selection; separate immutable source values and editable scenario assumptions.
- Breakdown: wait, replacement and later-appointment options. Cancellation: replacement only, because waiting/rescheduling does not restore capacity.
- Delay sliders, full replacement quote, retained original-carrier charges, recovery/rescheduling fees, proposed carrier, compliance attestations and timing/appointment confirmations.
- Deterministic Decimal cost/GP/margin comparison, cost arithmetic and GP deltas. Blank fees remain unknown. Explicit zero means a confirmed no-charge assumption.
- Arrival timeline with original/disrupted/recovery ETAs, appointment window and MABD. SVG is rendered as an image and regenerated on each input change; no Plotly dependency is required.
- Recommendation filters hard compliance, timing and evidence first; among confirmed feasible options it maximizes GP and uses complexity to break ties. Unconfirmed plans cannot win. This is conditional decision support, not an optimizer over all possible routes/carriers.
- Decision preview for any option, and Rewind clears all scenario widgets/selection/attestations while preserving the selected source shipment.
- Existing Load watch, Email-to-quote and Manual vs copilot views remain available. Only the selected view executes.

Demo example: first shipment sell $4,200, total cost $3,000, carrier portion $2,800. A replacement quote of $2,400 plus $150 retained original-carrier charge costs **$2,750 = $3,000 - $2,800 + $2,400 + $150**. GP = $1,450; margin = 34.52%; GP change = +$250. The original full carrier cost is not charged twice.

## Mocked / not connected

Demo shipments are fixtures, not real TMS data. Costs, quotes, carrier availability, insurance/compliance and transit times are entered assumptions. No recovery-specific LLM explanation exists: recommendations and explanations are deterministic and labeled. Decision previews never write, send, book, tender, dispatch or reschedule. No automatic refund, claim, detention or penalty calculation is provided. The additional-charge and delivery-delay disruption types are deferred; only the two implemented types are offered.

## Provisional read adapter — tools lead to validate

`ui.rewind_engine.from_tms(row, source) -> Shipment` uses only conservative explicit fields from `client.list_shipments()`:

| UI value | Provisional source |
|---|---|
| ID / status | `shipment_id`, `status` |
| Route | `shipper_city`, `shipper_state`, `consignee_city`, `consignee_state` |
| Carrier | `carrier_id` or `vendor_id` |
| Sell / total original cost | `sell_total`, `buy_total` |
| Replaceable original carrier cost | `carrier_cost` (never assumed equal to total cost) |
| ETA | `estimated_arrival` |
| Appointment window | `delivery_window_start`, `delivery_window_end` |
| MABD | `must_arrive_by` |

Timestamp fields require ISO timestamps with timezone offsets; date-only/local timestamps remain unknown. Real TMS stops may use nested fields: this adapter deliberately does not guess their timezone, appointment semantics or MABD cutoff. Missing financial/timing values can be supplied as labeled scenario overrides. List payload fields, cost scope and nested stop mappings need backend-owner confirmation before live-data readiness can be claimed.

## Requested backend interfaces (proposal only)

1. `get_recovery_context(shipment_id)` — normalized totals, replaceable carrier portion, already-earned/nonrefundable charges, original ETA, timezone-aware appointment and MABD, compliance evidence and provenance timestamps.
2. `get_recovery_quotes(context, disruption)` — quoted all-in replacement costs, retained original charges/known refunds, availability, transit and handoff feasibility, expiry, and documentary compliance evidence. Unknowns must remain nullable.
3. `evaluate_recovery(context, scenario)` — validated applicable options with explicit cost components, time feasibility, evidence, exclusions and unknowns. Current pure UI engine is replaceable.
4. Optional `explain_recovery(evaluated_options)` — explanation grounded only in deterministic outputs, never LLM arithmetic.

No write interface is requested in this prototype. Coordinate shared-contract changes with the models owner; nothing in `meridian/` was modified.

## Verification

```bash
python -m pytest -q tests ui/test_rewind.py
```

UI-owned tests cover replacement accounting, unknown vs zero charges, cancellation applicability, noncompliance/late-plan exclusion, MABD preservation, invalid baselines, source normalization, interactive state reset and legacy view rendering. Browser verification should additionally check the generated timeline image changes and mobile overflow. Scratch browser scripts/screenshots live in `work/` and are not intended for commits.
