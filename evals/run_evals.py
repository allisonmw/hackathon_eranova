"""Eval runner. Owner: agent & evals lead.

    python evals/run_evals.py                 # full: extraction + checks + pricing (dry-run, needs the API)
    python evals/run_evals.py --extract-only  # extraction accuracy only (model only, no TMS API)

Email cases: evals/emails/NN_name.txt + evals/expected/NN_name.json:
    {"today": "2026-11-13",                      # date the email is "received" (for relative dates)
     "request": {...QuoteRequest fields...},     # only the fields you want scored
     "missing_fields": ["PO number"],            # exact strings from quote_playbook
     "ready_to_create": false}

Risk labels: evals/risk_labels.json, scored against snapshots/ (run the snapshot first).

Write the expected results BEFORE looking at what the code produces.
Results: evals/results/latest.json (shown in the app) and a line in evals/score_log.csv per run.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import pathlib
import sys
from datetime import date, datetime

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from meridian import llm, quote_playbook, risk_playbook, risk_rules  # noqa: E402

RESULTS = HERE / "results"


def flatten(value, prefix: str = "") -> dict:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out.update(flatten(v, f"{prefix}{k}."))
        return out
    if isinstance(value, list) and value and isinstance(value[0], dict):
        out = {}
        for i, v in enumerate(value):
            out.update(flatten(v, f"{prefix}{i}."))
        return out
    return {prefix.rstrip("."): value}


def same(expected, actual) -> bool:
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(expected - actual) <= max(0.005 * abs(expected), 0.01)
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.strip().lower() == actual.strip().lower()
    if isinstance(expected, list) and isinstance(actual, list):
        return sorted(map(str, expected)) == sorted(map(str, actual))
    return expected == actual


def eval_emails(extract_only: bool) -> tuple[list[dict], dict]:
    rows, fields_ok, fields_total = [], 0, 0
    missing_ok = missing_total = margin_ok = margin_total = unsafe = 0
    for email_path in sorted((HERE / "emails").glob("*.txt")):
        exp_path = HERE / "expected" / f"{email_path.stem}.json"
        if not exp_path.exists():
            continue
        exp = json.loads(exp_path.read_text(encoding="utf-8"))
        today = date.fromisoformat(exp["today"]) if exp.get("today") else date.today()
        email = email_path.read_text(encoding="utf-8")

        req = llm.extract_quote_request(email, today)
        actual = flatten(req.model_dump(mode="json"))
        expected = flatten(exp.get("request", {}))
        wrong = [k for k, v in expected.items() if not same(v, actual.get(k))]
        fields_ok += len(expected) - len(wrong)
        fields_total += len(expected)
        row = {"case": email_path.stem, "fields": f"{len(expected) - len(wrong)}/{len(expected)}",
               "wrong": ", ".join(f"{k}={actual.get(k)!r}" for k in wrong)}

        if not extract_only:
            p = quote_playbook.build_proposal(email, request=req, today=today)
            if "missing_fields" in exp:
                missing_total += 1
                hit = sorted(exp["missing_fields"]) == sorted(p.missing_fields)
                missing_ok += hit
                row["missing"] = "ok" if hit else f"got {p.missing_fields}"
            margin = next((c for c in p.checks if c.name == "Margin floor"), None)
            if margin:
                margin_total += 1
                margin_ok += margin.passed
            refs_missing = any(not c.passed for c in p.checks if c.name.startswith("Required reference"))
            unsafe += bool(p.ready_to_create and refs_missing)
            row["ready"] = p.ready_to_create
            if "ready_to_create" in exp and exp["ready_to_create"] != p.ready_to_create:
                row["ready"] = f"{p.ready_to_create} (expected {exp['ready_to_create']})"
        rows.append(row)

    pct = lambda a, b: f"{100 * a / b:.0f}%" if b else "n/a"  # noqa: E731
    summary = {"Field accuracy": pct(fields_ok, fields_total)}
    if not extract_only:
        summary |= {"Missing-field detection": pct(missing_ok, missing_total),
                    "Margin safety": pct(margin_ok, margin_total),
                    "Unsafe creates": str(unsafe)}
    return rows, summary


def eval_risks() -> dict:
    labels_path = HERE / "risk_labels.json"
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    if not labels.get("expected"):
        return {}
    os.environ["DEMO_MODE"] = "1"  # score against snapshots, not live data
    today = date.fromisoformat(labels["today"]) if labels.get("today") else date.today()
    ctx = risk_playbook.gather(today)
    found = {(it.record_id, it.kind): it.severity for it in risk_rules.run_all(ctx)}
    expected = {(e["record_id"], e["kind"]): e["severity"] for e in labels["expected"]}
    labeled = set(labels.get("labeled_record_ids", [])) | {rid for rid, _ in expected}

    high = [k for k, sev in expected.items() if sev == "HIGH"]
    flagged = [k for k in found if k[0] in labeled]
    pct = lambda a, b: f"{100 * a / b:.0f}%" if b else "n/a"  # noqa: E731
    return {
        "Risk recall (all)": pct(sum(k in found for k in expected), len(expected)),
        "Risk recall (HIGH)": pct(sum(k in found for k in high), len(high)),
        "Risk precision": pct(sum(k in expected for k in flagged), len(flagged)),
        "Missed": ", ".join(f"{rid}:{kind}" for rid, kind in expected if (rid, kind) not in found) or "none",
    }


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    ap = argparse.ArgumentParser()
    ap.add_argument("--extract-only", action="store_true")
    args = ap.parse_args()

    rows, summary = eval_emails(args.extract_only)
    summary |= eval_risks()

    print(f"\nModel: {llm.MODEL}\n")
    for r in rows:
        print("  " + " | ".join(f"{k}: {v}" for k, v in r.items() if v != ""))
    print()
    for k, v in summary.items():
        print(f"  {k:<26} {v}")

    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    result = {"run_at": stamp, "model": llm.MODEL, "summary": {"metric": list(summary), "score": list(summary.values())}, "cases": rows}
    for name in ("latest.json", f"{stamp}.json"):
        (RESULTS / name).write_text(json.dumps(result, indent=2), encoding="utf-8")
    log = HERE / "score_log.csv"
    new = not log.exists()
    with log.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["run_at", "model", *summary])
        w.writerow([stamp, llm.MODEL, *summary.values()])


if __name__ == "__main__":
    main()
