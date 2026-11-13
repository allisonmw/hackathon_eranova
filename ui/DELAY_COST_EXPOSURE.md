# Delay Cost Exposure — automatic synthetic results

The selected shipment in **Live tracking** automatically displays the teammate's modeled complications, costs, recoveries, reconciled gross profit and mitigation suggestions. The existing manual calculator remains in a collapsed **What-if Analysis** section. Its inputs/results are independent and are never added to the automatic totals.

## Source and coverage

- Workbook: `ui/data/Delay_Cost_Model.xlsx`, an unmodified copy of `fake_data` branch `Delay_Cost_Model.xlsx` at commit `b76d73dd85b0546be95d226e2444a0017fc73533`.
- SHA256: `ffe014e555faaf851f8891903a90cc269d069035531888cc1542789f77a9c997`. Loading verifies this digest, so refreshing the workbook requires an explicit provenance update.
- The source has **123 Events**, **302 Cost_Lines**, **160 shipment rollups** (plus a total row) and **123 Recommendations**. With the committed tracking snapshot, **9 events / 21 lines across 6 of 27 tracked shipments** match by ID.
- Documented generated CSV/raw JSON outputs are not committed on `fake_data`. This integration therefore consumes the workbook itself, not an assumed CSV or an import of the executable generator. No Excel/openpyxl runtime dependency is added.
- Event rows, quantities, rates, signs, ledger attribution, descriptions and recommendations are read from the workbook. No new complication probabilities, quotes, fees, policies, rates or events are invented. The old event-only JSON reference fixture was replaced by the complete source workbook.

`ui/synthetic_costs.py` reads XLSX XML values/cached results with the standard library. It implements only the workbook's documented line arithmetic and aggregation, without evaluating arbitrary Excel formulas, running macros, regenerating complications or duplicating the teammate's simulation logic. Cached currency figures are rounded to cents to remove Excel serialization artifacts such as `630.6400000000001`.

## Identity and reconciliation

Join: shipment ID → event IDs → unique cost-line IDs. Each line must agree with its event's shipment ID and root ID. Parent/root relationships are validated, including cycle checks. Identical repeated line IDs are counted once; conflicting duplicates or cross-shipment/event references are rejected. Root IDs group cascades; they do not add a second subtotal. Separate same-code incidents remain separate because charge codes do not establish event identity.

The calculations reproduce `build_workbook.py`:

1. Line amount = `ROUND(quantity × rate, 2)`.
2. Signed line cost = `amount × sign`, where +1 is expense and −1 is recovery.
3. Gross cost = sum of +1 line amounts; recoveries = sum of −1 line amounts; net = gross − recoveries.
4. Already-in-TMS net = sum of signed lines whose source `in_tms` flag is true.
5. Additional net = net − already-in-TMS net.
6. Modeled GP after disruptions = source `gp_amount_tms` − additional net.

Cached Cost_Lines amounts/signed values and Shipment_Rollup totals are checked against these calculations. `RECOVERY_PREMIUM` is an expense, despite its name. Customer recoveries, carrier chargebacks and claim recoveries follow their source signs. Previously recorded recoveries are excluded from the GP adjustment along with previously recorded expenses.

The `in_tms` flag refers to **the workbook's TMS baseline**, not proof that a current invoice is real, paid, collectible or present in a newer ledger. If the selected tracking GP differs from the workbook GP (or is missing), the UI warns and keeps the source-model baseline; it never applies historical ledger attribution to a new live baseline. Same-root consequences such as weather layover and missed-appointment layover are preserved as supplied in the source model. Overlap/contract validity still require human review; no additional costs are inferred from late arrival alone.

## Demonstrated workbook examples

| Shipment | Source events | Gross | Recoveries | Net | Baseline GP | Modeled GP after |
|---|---|---:|---:|---:|---:|---:|
| 7300117 | DLY-00108 WEATHER → DLY-00109 MISSED_APPOINTMENT | $630.64 | $0.00 | $630.64 | $469.00 | −$161.64 |
| 7300125 | DLY-00113 DETENTION_PICKUP + DLY-00114 DETENTION_DELIVERY | $342.00 | $150.00 | $192.00 | $296.00 | $104.00 |

For **7300117**: $250 weather layover + $23.94 labor (0.57 × $42) + $250 next-slot layover + $50 appointment fee + $56.70 labor (1.35 × $42) = **$630.64**. No flagged existing ledger amounts or recoveries; $469 − $630.64 = **−$161.64**.

For **7300125**: each detention incident has 2 **billable** hours × $75 plus 0.5 labor hours × $42, or $171 per incident. Gross $171 + $171 = **$342**; the source supplies a $150 customer recovery for the delivery incident. Net = **$192**; $296 − $192 = **$104**. The two-hour free time is not deducted again: source Cost_Lines quantities are already billable hours.

An existing-ledger example elsewhere in the workbook, **7300012**, has gross $267, recoveries $235, net $32 and already-in-TMS signed net **−$10** ($225 recorded costs less $235 recorded recoveries). Additional net is $32 − (−$10) = **$42**, and GP is $234.65 − $42 = **$192.65**. Deducting the entire $32 from a GP already containing those ledger entries would be wrong.

**7300166** is tracked but has no matching simulated event. The primary view says **“No modeled complication available”** and displays no modeled cost total or invented zero exposure.

## Source modes and uncertainty

Automatic workbook results appear only in **Fake data** and **Snapshots (demo mode)**, explicitly labeled **SIMULATED ESTIMATES**. They are disabled in **Live API** mode. No match means unavailable, not zero; missing line amounts, baseline financials or ledger attribution remain unknown. Mitigations are imported planning suggestions from Recommendations, never approved TMS actions or promises of savings. Simulation statuses such as CONFIRMED are labeled as workbook statuses, not verified operational charges.

The unchanged manual cost engine stays available separately under What-if Analysis for all modes. It uses Decimal arithmetic, conditional triggers, unknown amounts and explicit ledger/event-scope reconciliation. Its default rates remain illustrative (DET $75/hr after two free hours; LAY $250/night; APPT $50; STOR $25/day; labor $42/hr; OTIF example 3%; capital 9% annually). It never changes automatic source totals, tracking ETA, shipment records or the map.

## Scope and execution

All changes remain on `feat/tracking-cost-estimates`. No merge into a teammate branch is performed. The tracking integration call and existing map, ETA algorithm, event feed, timeline and check-call form are preserved. No `meridian/`, API-client, shared-model, dependency or tracking-snapshot changes are needed. The app continues to force DRY_RUN on.

From the repository root:

```bash
MODEL=fake DRY_RUN=1 .venv/bin/python -m streamlit run app.py --server.port 8501
.venv/bin/python -m pytest -q tests ui/test_delay_costs.py ui/test_synthetic_costs.py
```

Windows PowerShell after installing dependencies:

```powershell
$env:MODEL = "fake"
$env:DRY_RUN = "1"
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Open Live tracking with Fake data selected. Select 7300117 or 7300125; costs appear without selecting any categories. Choose 7300166 to see the no-model state. All source-data processing is read-only; no live credentials are required for this demo. Map tiles may use the existing external map provider.

## Validation

**89 tests passed** across the existing suite, manual estimator regressions and automatic-model tests. All modeled shipments are checked against the source workbook's cached rollups to the cent. Tests cover parent/child cascades, separate same-code incidents, repeated/conflicting line IDs, cross-shipment identity errors, recorded costs and recoveries, unknown quantities/ledger attribution, current-baseline mismatches, Live API isolation, automatic shipment switching and independence from manual inputs. Chromium demonstrations verified 7300117 and 7300125 without selecting manual categories, and the no-model state for 7300166, with no JavaScript errors or application exception panels. Tracking/backend files and the DRY_RUN setting are unchanged.
