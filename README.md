# Broker Desk Copilot

A copilot for a freight broker on the Meridian TMS:

1. **Email-to-quote:** paste a customer email → structured, priced quote plus a draft reply, with the margin floor and required references enforced in code.
2. **At-risk load watch:** one click scans every open load and lists what needs attention, ranked, each with evidence, a suggested action and a draft message.

The broker reviews and approves. Full plan: [project-plan.md](project-plan.md).

## Layout and owners

| Path | What | Owner |
|---|---|---|
| `meridian/models.py` | Shared Pydantic contracts. **Announce changes to the team.** | Agent & evals lead |
| `meridian/llm.py` | Model calls: extract, draft reply, explain + draft risks (batched) | Agent & evals lead |
| `meridian/quote_playbook.py` | `build_proposal(email)`, `create_quote(proposal)` (create → price → verify) | Agent & evals lead |
| `meridian/risk_playbook.py` | `scan()` (gather → rules → rank → explain), `act(item, action)` | Agent & evals lead |
| `agent.py` | Investigate agent (LangGraph + read-only MCP tools) | Agent & evals lead |
| `evals/` | Sample emails, expected JSON, risk labels, `run_evals.py` | Agent & evals lead |
| `meridian/client.py` | API wrapper: reads, dry-run writes, snapshot/demo mode | Tools & rules lead |
| `meridian/pricing.py` | Rate engine → price preview; margin math | Tools & rules lead |
| `meridian/risk_rules.py` | One pure function per risk rule | Tools & rules lead |
| `server.py` | MCP server exposing read tools | Tools & rules lead |
| `tests/` | `pytest` unit tests for rules, pricing, checks | Tools & rules lead |
| `app.py`, `ui/` | Streamlit app: tabs, cards, scoreboard, fake data | UI lead |
| `snapshots/` | API data saved at the start (demo fallback) | Tools & rules lead |
| `baseline.json` | Measured manual times and clicks (**placeholder until measured**) | Agent & evals lead |

## Setup (PowerShell, once)

```powershell
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Fill in `.env`: TMS values from `packet/secret.txt`, plus your model key. `MODEL=fake` runs everything without a key.

## Run

```powershell
streamlit run app.py
```

The sidebar's **Data source** picks where data comes from:

| Source | Use it for |
|---|---|
| Fake data (default) | UI work before the playbooks are ready. No API or key needed. |
| Snapshots (demo mode) | The demo, or when the API/wifi is down. Reads `snapshots/*.json`; writes are always dry-run. |
| Live API | Real data. Writes are still dry-run unless `DRY_RUN=0`. |

## Phase-0 jobs

```powershell
python -m meridian.client snapshot   # tools lead: save live data to snapshots/
pytest                               # rules, pricing and checks
python evals/run_evals.py            # full evals (dry-run, uses the API)
python evals/run_evals.py --extract-only   # extraction only, no TMS API
python agent.py 7300172              # Investigate one load from the CLI
mcp dev server.py                    # inspect the MCP tools (needs Node.js)
```

## Safety

- **Every write goes through `meridian/client.py` and is dry-run by default.** In dry-run a write returns `{"ok": true, "dry_run": true, "would_send": {...}}` and changes nothing. Only the tools lead sets `DRY_RUN=0`, for one planned real run.
- After a real write, re-read the record and confirm the change (`quote_playbook.create_quote` does this).
- Rules (margin floor, required references, insurance) live in code, never in prompts.

## Working agreements

- **Main always runs.** Pull before starting, push small working changes often, never push anything that breaks `streamlit run app.py`.
- **Contracts first.** Changes to `meridian/models.py` are announced to the team.
- **The eval set decides** whether something works. Write expected results before looking at output.
- **Dry-run stays on**, and we never load-test the shared API.
