"""Build delay_sim/Delay_Cost_Model.xlsx from data/complications/*.csv.

Raw rows are values; every total, rollup, scorecard, cumulative and savings figure is an Excel
formula over those rows, so editing a rate in Assumptions-driven columns or an avoidable % in
Solutions recalculates the model.
"""
import csv
import json
from pathlib import Path

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

ROOT = Path(__file__).resolve().parent
D = ROOT / "data" / "complications"
RAW = ROOT / "data" / "raw"
OUT = ROOT / "Delay_Cost_Model.xlsx"

def rd(name):
    with open(D / name, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))

def num(v):
    if v in ("", None):
        return None
    if v in ("True", "False"):
        return v == "True"
    try:
        f = float(v)
        return int(f) if f.is_integer() and "." not in v else f
    except ValueError:
        return v

events, lines, recs = rd("delay_events.csv"), rd("delay_cost_lines.csv"), rd("event_recommendations.csv")
rollup, sols, assumptions = rd("shipment_cost_rollup.csv"), rd("solutions_catalog.csv"), rd("assumptions.csv")
vendors = {v["vendor_id"]: v for v in json.loads((RAW / "vendors.json").read_text(encoding="utf-8"))}
facilities = {f["facility_id"]: f for f in json.loads((RAW / "facilities.json").read_text(encoding="utf-8"))}
ev_by_id = {e["event_id"]: e for e in events}

FONT = "Arial"
HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(name=FONT, bold=True, color="FFFFFF")
BLUE = Font(name=FONT, color="0000FF")
GREEN = Font(name=FONT, color="008000")
BOLD = Font(name=FONT, bold=True)
BASE = Font(name=FONT)
YELLOW = PatternFill("solid", fgColor="FFFF00")
USD = '$#,##0;($#,##0);"-"'
USD2 = '$#,##0.00;($#,##0.00);"-"'
PCT = '0.0%;(0.0%);"-"'
thin = Side(style="thin", color="BFBFBF")

wb = Workbook()

def sheet(title, headers, widths=None):
    ws = wb.create_sheet(title)
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=i, value=h)
        c.font, c.fill = HDR_FONT, HDR_FILL
        c.alignment = Alignment(wrap_text=True, vertical="center")
    ws.freeze_panes = "A2"
    for i, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(h, max(11, min(len(h) + 3, 28)))
    ws.row_dimensions[1].height = 32
    return ws

def table(ws, name, ncols, nrows):
    t = Table(displayName=name, ref=f"A1:{get_column_letter(ncols)}{nrows + 1}")
    t.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
    ws.add_table(t)

def fmt(ws, col_letters, numfmt, first=2, last=None):
    last = last or ws.max_row
    for L in col_letters:
        for r in range(first, last + 1):
            ws[f"{L}{r}"].number_format = numfmt

def basefont(ws):
    for row in ws.iter_rows(min_row=2):
        for c in row:
            if c.font is None or c.font.name != FONT:
                c.font = Font(name=FONT, bold=c.font.bold if c.font else False, color=c.font.color if c.font else None)

# ------------------------------------------------------------------ README
ws = wb.active
ws.title = "README"
readme = [
    ("Meridian TMS — Shipment Delay & Complication Cost Model", BOLD),
    ("", None),
    ("What this is", BOLD),
    ("A synthetic but realistic set of delay/complication events layered onto the real Meridian TMS snapshot (160 shipments, 18 vendors, 30 facilities).", None),
    ("Every event references a real shipment_id, carrier vendor_id, facility_id and customer_id and uses the API's own codes", None),
    ("(late_reason_code, X12 status code, charge codes, hold reasons, POD condition, appointment status), so rows can be replayed through the API.", None),
    ("Events marked source = TMS_EXISTING mirror exceptions the TMS already records (late T3/WEATHER/NS deliveries, DET/LAY/TONU lines, damage/shortage PODs, holds).", None),
    ("Cost lines flagged in_tms = TRUE are already on the TMS ledger and are excluded when computing GP after complications (no double count).", None),
    ("", None),
    ("Sheets", BOLD),
    ("Assumptions — rates and cargo values driving the costs (blue = editable input).", None),
    ("Events — one row per complication; parent_event_id / root_event_id link cascades (e.g. breakdown → missed appointment → OTIF fine).", None),
    ("Cost_Lines — every cost or recovery: amount = quantity × rate; net = amount × sign (+1 cost, −1 recovery).", None),
    ("Shipment_Rollup — accumulated complication cost per shipment and GP after complications (all SUMIFS).", None),
    ("By_Complication — totals per complication type with best-solution savings.", None),
    ("Monthly_Accumulated — month-by-month and cumulative (running) complication cost.", None),
    ("Carrier_Scorecard / Facility_Scorecard — where the cost concentrates.", None),
    ("Solutions — solution + two alternatives per complication; edit avoidable % (blue) to re-run savings.", None),
    ("Recommendations — per-event recommended fix, alternatives, alternative carrier and the TMS action to take.", None),
    ("", None),
    ("Sign convention: positive = cost to Meridian; negative = recovery (billed to customer, charged back to carrier, claim recovery).", None),
    ("Cost categories: CARRIER_ACCESSORIAL, RECOVERY_PREMIUM (spot/re-power), CUSTOMER_PENALTY (OTIF), CLAIM_LOSS, DISPOSAL, OPS_LABOR, CAPITAL_COST, UNBILLABLE; recoveries: CUSTOMER_RECOVERY, CARRIER_CHARGEBACK, CLAIM_RECOVERY.", None),
    ("Status: cost_status CONFIRMED for closed/POD files; ESTIMATED for loads still moving (cost still accruing).", None),
    ("Regenerate: python delay_sim/pull_tms_data.py → python delay_sim/generate_complications.py → python delay_sim/build_workbook.py (seeded, deterministic).", None),
]
for i, (t, f) in enumerate(readme, 1):
    c = ws.cell(row=i, column=1, value=t)
    c.font = f or BASE
ws["A1"].font = Font(name=FONT, bold=True, size=14)
ws.column_dimensions["A"].width = 45

# ------------------------------------------------------------------ Assumptions
ws = sheet("Assumptions", ["key", "value", "note"], {"key": 28, "value": 12, "note": 90})
for i, a in enumerate(assumptions, 2):
    ws.cell(row=i, column=1, value=a["key"]).font = BASE
    c = ws.cell(row=i, column=2, value=num(a["value"]))
    c.font = BLUE
    c.number_format = PCT if a["key"] in ("OTIF_PCT", "COST_OF_CAPITAL") else USD2
    ws.cell(row=i, column=3, value=a["note"]).font = BASE
table(ws, "tblAssumptions", 3, len(assumptions))

# ------------------------------------------------------------------ Events
ecols = list(events[0].keys())
ws = sheet("Events", ecols, {"detail": 60, "complication_label": 34, "lane": 30, "event_at_local": 19, "event_at_utc": 21, "event_timezone": 18})
for r, e in enumerate(events, 2):
    for c, k in enumerate(ecols, 1):
        ws.cell(row=r, column=c, value=num(e[k]) if k not in ("shipment_id", "pickup_date", "month", "event_at_local", "event_at_utc") else e[k]).font = BASE
table(ws, "tblEvents", len(ecols), len(events))

# ------------------------------------------------------------------ Cost_Lines (amount & net are formulas)
lcols = ["line_id", "event_id", "root_event_id", "shipment_id", "month", "carrier_id", "facility_id", "customer_id",
         "complication_type", "cost_category", "ledger", "charge_code", "rate_basis", "quantity", "rate", "sign",
         "amount", "net_cost", "in_tms", "status", "vendor_id", "recoverable_from", "description"]
ws = sheet("Cost_Lines", lcols, {"description": 60, "complication_type": 24, "cost_category": 22})
for r, l in enumerate(lines, 2):
    e = ev_by_id[l["event_id"]]
    row = dict(l, month=e["month"], carrier_id=e["carrier_id"], facility_id=e["facility_id"], customer_id=e["customer_id"])
    for c, k in enumerate(lcols, 1):
        if k == "amount":
            v = f"=ROUND(N{r}*O{r},2)"
        elif k == "net_cost":
            v = f"=Q{r}*P{r}"
        elif k in ("quantity", "rate", "sign"):
            v = num(row[k])
        elif k == "in_tms":
            v = row[k] == "True"
        else:
            v = row[k]
        cell = ws.cell(row=r, column=c, value=v)
        cell.font = BLUE if k in ("quantity", "rate") else BASE
fmt(ws, ["O"], USD2); fmt(ws, ["Q", "R"], USD2)
table(ws, "tblCostLines", len(lcols), len(lines))
NL = len(lines) + 1
CL = lambda col: f"Cost_Lines!${col}$2:${col}${NL}"
# column map
C_SHIP, C_MONTH, C_CARR, C_FAC, C_TYPE, C_CAT, C_SIGN, C_AMT, C_NET, C_INTMS = "D", "E", "F", "G", "I", "J", "P", "Q", "R", "S"

# ------------------------------------------------------------------ Shipment_Rollup
rcols = ["shipment_id", "status", "mode", "customer_id", "carrier_id", "pickup_date", "lane", "sell_total", "buy_total",
         "gp_amount_tms", "events", "delay_hours", "gross_complication_cost", "recoveries", "net_complication_cost",
         "already_in_tms_net", "gp_after_complications", "gp_pct_tms", "gp_pct_after", "margin_erosion_pts"]
ws = sheet("Shipment_Rollup", rcols)
NE = len(events) + 1
def E(k):
    L = get_column_letter(ecols.index(k) + 1)
    return f"Events!${L}$2:${L}${NE}"
for r, s in enumerate(rollup, 2):
    vals = {
        "shipment_id": s["shipment_id"], "status": s["status"], "mode": s["mode"], "customer_id": s["customer_id"],
        "carrier_id": s["carrier_id"], "pickup_date": s["pickup_date"], "lane": s["lane"],
        "sell_total": num(s["sell_total"]), "buy_total": num(s["buy_total"]), "gp_amount_tms": num(s["gp_amount_tms"]),
        "events": f'=COUNTIF({E('shipment_id')},A{r})',
        "delay_hours": f'=SUMIFS({E('delay_hours')},{E('shipment_id')},A{r},{E('complication_type')},"<>PAPERWORK_HOLD")',
        "gross_complication_cost": f'=SUMIFS({CL(C_AMT)},{CL(C_SHIP)},A{r},{CL(C_SIGN)},1)',
        "recoveries": f'=SUMIFS({CL(C_AMT)},{CL(C_SHIP)},A{r},{CL(C_SIGN)},-1)',
        "net_complication_cost": f"=M{r}-N{r}",
        "already_in_tms_net": f'=SUMIFS({CL(C_NET)},{CL(C_SHIP)},A{r},{CL(C_INTMS)},TRUE)',
        "gp_after_complications": f"=J{r}-(O{r}-P{r})",
        "gp_pct_tms": f'=IF(H{r}=0,"",J{r}/H{r})',
        "gp_pct_after": f'=IF(H{r}=0,"",Q{r}/H{r})',
        "margin_erosion_pts": f'=IF(H{r}=0,"",R{r}-S{r})',
    }
    for c, k in enumerate(rcols, 1):
        ws.cell(row=r, column=c, value=vals[k]).font = BASE
NR = len(rollup) + 1
fmt(ws, ["H", "I", "J", "M", "N", "O", "P", "Q"], USD); fmt(ws, ["R", "S", "T"], PCT); fmt(ws, ["L"], "0.0")
tr = NR + 1
ws.cell(row=tr, column=1, value="TOTAL").font = BOLD
for L in "HIJKLMNOPQ":
    c = ws[f"{L}{tr}"]; c.value = f"=SUM({L}2:{L}{NR})"; c.font = BOLD
    c.number_format = USD if L not in "KL" else "#,##0"
ws[f"R{tr}"] = f"=J{tr}/H{tr}"; ws[f"S{tr}"] = f"=Q{tr}/H{tr}"; ws[f"T{tr}"] = f"=R{tr}-S{tr}"
for L in "RST":
    ws[f"{L}{tr}"].number_format = PCT; ws[f"{L}{tr}"].font = BOLD
ws.auto_filter.ref = f"A1:{get_column_letter(len(rcols))}{NR}"

# Events sheet column letters used above: shipment_id must be C, complication_type S, delay_hours Z

# ------------------------------------------------------------------ Solutions
scols = ["complication_type", "label", "root_cause_owner", "net_cost_observed", "primary_solution", "primary_avoidable_pct",
         "primary_savings", "alternative_1", "alt1_avoidable_pct", "alt1_savings", "alternative_2", "alt2_avoidable_pct",
         "alt2_savings", "best_option", "best_savings", "action_owner", "tms_action"]
ws = sheet("Solutions", scols, {"primary_solution": 55, "alternative_1": 45, "alternative_2": 45, "tms_action": 60, "label": 34, "best_option": 14})
for r, s in enumerate(sols, 2):
    vals = {
        "complication_type": s["complication_type"], "label": s["label"], "root_cause_owner": s["root_cause_owner"],
        "net_cost_observed": f'=SUMIFS({CL(C_NET)},{CL(C_TYPE)},A{r},{CL(C_INTMS)},FALSE)+SUMIFS({CL(C_NET)},{CL(C_TYPE)},A{r},{CL(C_INTMS)},TRUE)',
        "primary_solution": s["primary_solution"], "primary_avoidable_pct": num(s["primary_avoidable_pct"]),
        "primary_savings": f"=MAX(0,D{r})*F{r}",
        "alternative_1": s["alternative_1"], "alt1_avoidable_pct": num(s["alt1_avoidable_pct"]), "alt1_savings": f"=MAX(0,D{r})*I{r}",
        "alternative_2": s["alternative_2"], "alt2_avoidable_pct": num(s["alt2_avoidable_pct"]), "alt2_savings": f"=MAX(0,D{r})*L{r}",
        "best_option": f'=IF(MAX(G{r},J{r},M{r})=0,"-",IF(G{r}=MAX(G{r},J{r},M{r}),"Primary",IF(J{r}=MAX(G{r},J{r},M{r}),"Alternative 1","Alternative 2")))',
        "best_savings": f"=MAX(G{r},J{r},M{r})",
        "action_owner": s["action_owner"], "tms_action": s["tms_action"],
    }
    for c, k in enumerate(scols, 1):
        cell = ws.cell(row=r, column=c, value=vals[k])
        cell.font = BLUE if k.endswith("avoidable_pct") else BASE
        if k.endswith("avoidable_pct"):
            cell.fill = YELLOW
NS = len(sols) + 1
fmt(ws, ["D", "G", "J", "M", "O"], USD); fmt(ws, ["F", "I", "L"], PCT)
tr = NS + 1
ws.cell(row=tr, column=1, value="TOTAL").font = BOLD
for L in "DGJMO":
    ws[f"{L}{tr}"] = f"=SUM({L}2:{L}{NS})"; ws[f"{L}{tr}"].font = BOLD; ws[f"{L}{tr}"].number_format = USD
ws.cell(row=tr + 2, column=1, value="Avoidable % values are planning estimates (assumed) — edit the yellow cells to test scenarios.").font = Font(name=FONT, italic=True)
ws["F1"].comment = Comment("Share of this complication's net cost the solution is expected to remove (assumption; industry-typical ranges).", "model")

# ------------------------------------------------------------------ By_Complication
bcols = ["complication_type", "events", "root_events", "share_of_events", "total_delay_hours", "gross_cost", "recoveries",
         "net_cost", "net_cost_per_event", "share_of_net_cost", "best_solution", "best_savings", "net_after_best_solution"]
ws = sheet("By_Complication", bcols, {"best_solution": 55})
types = [s["complication_type"] for s in sols]
for r, t in enumerate(types, 2):
    m = f"MATCH(A{r},Solutions!$A$2:$A${NS},0)"
    vals = {
        "complication_type": t,
        "events": f"=COUNTIF({E('complication_type')},A{r})",
        "root_events": f'=COUNTIFS({E('complication_type')},A{r},{E('parent_event_id')},"")',
        "share_of_events": f"=IF(SUM($B$2:$B${len(types)+1})=0,0,B{r}/SUM($B$2:$B${len(types)+1}))",
        "total_delay_hours": f"=SUMIFS({E('delay_hours')},{E('complication_type')},A{r})",
        "gross_cost": f"=SUMIFS({CL(C_AMT)},{CL(C_TYPE)},A{r},{CL(C_SIGN)},1)",
        "recoveries": f"=SUMIFS({CL(C_AMT)},{CL(C_TYPE)},A{r},{CL(C_SIGN)},-1)",
        "net_cost": f"=F{r}-G{r}",
        "net_cost_per_event": f"=IF(B{r}=0,0,H{r}/B{r})",
        "share_of_net_cost": f"=IF(SUM($H$2:$H${len(types)+1})=0,0,H{r}/SUM($H$2:$H${len(types)+1}))",
        "best_solution": f'=IF(INDEX(Solutions!$N$2:$N${NS},{m})="Alternative 1",INDEX(Solutions!$H$2:$H${NS},{m}),IF(INDEX(Solutions!$N$2:$N${NS},{m})="Alternative 2",INDEX(Solutions!$K$2:$K${NS},{m}),INDEX(Solutions!$E$2:$E${NS},{m})))',
        "best_savings": f"=INDEX(Solutions!$O$2:$O${NS},{m})",
        "net_after_best_solution": f"=H{r}-L{r}",
    }
    for c, k in enumerate(bcols, 1):
        ws.cell(row=r, column=c, value=vals[k]).font = GREEN if k in ("best_solution", "best_savings") else BASE
NB = len(types) + 1
fmt(ws, ["F", "G", "H", "I", "L", "M"], USD); fmt(ws, ["D", "J"], PCT); fmt(ws, ["E"], "#,##0.0")
tr = NB + 1
ws.cell(row=tr, column=1, value="TOTAL").font = BOLD
for L in "BCEFGHLM":
    ws[f"{L}{tr}"] = f"=SUM({L}2:{L}{NB})"; ws[f"{L}{tr}"].font = BOLD
    ws[f"{L}{tr}"].number_format = USD if L in "FGHLM" else "#,##0"
ws[f"I{tr}"] = f"=IF(B{tr}=0,0,H{tr}/B{tr})"; ws[f"I{tr}"].number_format = USD; ws[f"I{tr}"].font = BOLD

# ------------------------------------------------------------------ Monthly_Accumulated
months = sorted({e["month"] for e in events})
mcols = ["month", "events", "gross_cost", "recoveries", "net_cost", "cumulative_net_cost", "cumulative_after_best_solutions"]
ws = sheet("Monthly_Accumulated", mcols)
for r, mth in enumerate(months, 2):
    ws.cell(row=r, column=1, value=mth).font = BASE
    ws.cell(row=r, column=2, value=f"=COUNTIF({E('month')},A{r})")
    ws.cell(row=r, column=3, value=f"=SUMIFS({CL(C_AMT)},{CL(C_MONTH)},A{r},{CL(C_SIGN)},1)")
    ws.cell(row=r, column=4, value=f"=SUMIFS({CL(C_AMT)},{CL(C_MONTH)},A{r},{CL(C_SIGN)},-1)")
    ws.cell(row=r, column=5, value=f"=C{r}-D{r}")
    ws.cell(row=r, column=6, value=f"=E{r}" if r == 2 else f"=F{r-1}+E{r}")
    # scale each month by the portfolio-wide share the best solutions remove
    ws.cell(row=r, column=7, value=f"=F{r}*(1-By_Complication!$L${NB+1}/By_Complication!$H${NB+1})")
NM = len(months) + 1
fmt(ws, ["C", "D", "E", "F", "G"], USD)
from openpyxl.chart import LineChart, Reference, BarChart
ch = LineChart(); ch.title = "Accumulated complication cost"; ch.y_axis.title = "USD"; ch.x_axis.title = "Pickup month"
ch.add_data(Reference(ws, min_col=6, min_row=1, max_row=NM), titles_from_data=True)
ch.add_data(Reference(ws, min_col=7, min_row=1, max_row=NM), titles_from_data=True)
ch.set_categories(Reference(ws, min_col=1, min_row=2, max_row=NM))
ch.roundedCorners = False; ch.legend.overlay = False; ch.title.overlay = False
ch.x_axis.delete = False; ch.y_axis.delete = False; ch.height, ch.width = 9, 20
ws.add_chart(ch, "I2")

# ------------------------------------------------------------------ Carrier_Scorecard
carriers = sorted({s["carrier_id"] for s in rollup if s["carrier_id"]})
kcols = ["carrier_id", "legal_name", "status", "compliance_light", "on_time_delivery_pct", "claims_count", "loads", "events",
         "events_per_load", "carrier_caused_events", "net_cost", "net_cost_per_load", "recommendation"]
ws = sheet("Carrier_Scorecard", kcols, {"legal_name": 28, "recommendation": 60})
for r, cid in enumerate(carriers, 2):
    v = vendors.get(cid, {})
    vals = {
        "carrier_id": cid, "legal_name": v.get("legal_name"), "status": v.get("status"), "compliance_light": v.get("compliance_light"),
        "on_time_delivery_pct": (v.get("on_time_delivery_pct") or 0) / 100, "claims_count": v.get("claims_count"),
        "loads": f"=COUNTIF(Shipment_Rollup!$E$2:$E${NR},A{r})",
        "events": f"=COUNTIF({E('carrier_id')},A{r})",
        "events_per_load": f"=IF(G{r}=0,0,H{r}/G{r})",
        "carrier_caused_events": f'=COUNTIFS({E('carrier_id')},A{r},{E('root_cause_owner')},"CARRIER")',
        "net_cost": f"=SUMIFS({CL(C_NET)},{CL(C_CARR)},A{r})",
        "net_cost_per_load": f"=IF(G{r}=0,0,K{r}/G{r})",
        "recommendation": f'=IF(C{r}="DO_NOT_USE","Stop tendering — DO_NOT_USE carrier still has loads",IF(D{r}="RED","Hold: compliance RED — fix insurance/authority before tendering",IF(AND(L{r}>AVERAGE($L$2:$L${len(carriers)+1}),J{r}>=2),"Reduce share on this lane; move to higher-OTD backup",IF(D{r}="YELLOW","Insurance expiring <30 days — request renewed COI","Keep / grow share"))))',
    }
    for c, k in enumerate(kcols, 1):
        ws.cell(row=r, column=c, value=vals[k]).font = BASE
NK = len(carriers) + 1
fmt(ws, ["E"], PCT); fmt(ws, ["I"], "0.00"); fmt(ws, ["K", "L"], USD)

# ------------------------------------------------------------------ Facility_Scorecard
facs = sorted({e["facility_id"] for e in events if e["facility_id"]})
fcols = ["facility_id", "name", "facility_type", "appointment_required", "avg_unload_minutes", "lumper_policy", "mabd_program",
         "events", "detention_events", "otif_events", "net_cost", "recommendation"]
ws = sheet("Facility_Scorecard", fcols, {"name": 32, "recommendation": 60})
for r, fid in enumerate(facs, 2):
    f = facilities.get(fid, {})
    vals = {
        "facility_id": fid, "name": f.get("name"), "facility_type": f.get("facility_type"), "appointment_required": f.get("appointment_required"),
        "avg_unload_minutes": f.get("avg_unload_minutes"), "lumper_policy": f.get("lumper_policy"), "mabd_program": f.get("mabd_program"),
        "events": f"=COUNTIF({E('facility_id')},A{r})",
        "detention_events": f'=COUNTIFS({E('facility_id')},A{r},{E('complication_type')},"DETENTION*")',
        "otif_events": f'=COUNTIFS({E('facility_id')},A{r},{E('complication_type')},"OTIF_CHARGEBACK")',
        "net_cost": f"=SUMIFS({CL(C_NET)},{CL(C_FAC)},A{r})",
        "recommendation": f'=IF(J{r}>0,"MABD DC: deliver T-1, only >=93% OTD carriers, 24H_DELIVERY_CONF",IF(I{r}>=2,"High dwell: drop-trailer or price detention into quotes",IF(F{r}="REQUIRED","Pre-pay lumper at contracted rate","Monitor")))',
    }
    for c, k in enumerate(fcols, 1):
        ws.cell(row=r, column=c, value=vals[k]).font = BASE
fmt(ws, ["K"], USD)

# ------------------------------------------------------------------ Recommendations
qcols = list(recs[0].keys())
ws = sheet("Recommendations", qcols, {"primary_solution": 50, "alternative_1": 40, "alternative_2": 40, "carrier_alternative": 60, "tms_action": 55})
for r, q in enumerate(recs, 2):
    for c, k in enumerate(qcols, 1):
        ws.cell(row=r, column=c, value=num(q[k]) if k not in ("shipment_id",) else q[k]).font = BASE
for L, k in ((get_column_letter(qcols.index(k) + 1), k) for k in qcols):
    if k.endswith("_pct"):
        fmt(ws, [L], PCT)
    elif k.endswith("savings_est") or k == "net_cost":
        fmt(ws, [L], USD2)
table(ws, "tblRecommendations", len(qcols), len(recs))

# Summary on README
ws = wb["README"]
r0 = len(readme) + 2
ws.cell(row=r0, column=1, value="Headline (live formulas)").font = BOLD
heads = [
    ("Shipments in snapshot", f"=COUNTA(Shipment_Rollup!A2:A{NR})", "#,##0"),
    ("Shipments with ≥1 complication", f'=COUNTIF(Shipment_Rollup!K2:K{NR},">0")', "#,##0"),
    ("Complication events", f"=COUNTA(Events!A2:A{NE})", "#,##0"),
    ("Gross complication cost", f"=By_Complication!F{NB+1}", USD),
    ("Recoveries (customer, carrier, insurance)", f"=By_Complication!G{NB+1}", USD),
    ("Net accumulated complication cost", f"=By_Complication!H{NB+1}", USD),
    ("GP per TMS", f"=Shipment_Rollup!J{NR+1}", USD),
    ("GP after complications", f"=Shipment_Rollup!Q{NR+1}", USD),
    ("Savings if best solution adopted per type", f"=Solutions!O{NS+1}", USD),
]
for i, (lbl, f, nf) in enumerate(heads, 1):
    ws.cell(row=r0 + i, column=1, value=lbl).font = BASE
    c = ws.cell(row=r0 + i, column=2, value=f); c.number_format = nf; c.font = GREEN
ws.column_dimensions["B"].width = 16
ws.column_dimensions["A"].width = 45

for w in wb.worksheets:
    w.sheet_view.zoomScale = 90
wb.save(OUT)
print("saved", OUT)
