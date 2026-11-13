# Project plan: Broker Desk Copilot

**Challenge:** build a solution that helps a TMS user be more productive.

**Our answer:** a copilot for a freight broker at a small brokerage who handles both quoting and dispatch. It does two things:

1. **Email-to-quote:** paste a customer's email and get a structured, priced quote plus a draft reply in seconds, with the margin floor and required references enforced.
2. **At-risk load watch:** one click scans every open load and lists what needs attention, ranked by urgency, each with the reason, the evidence, a suggested action and a draft message.

The broker reviews and approves. The copilot does the hunting, re-typing and math.

**The pitch in one line:** *"A broker's morning goes from 15 screens and 40 minutes of hunting to one ranked list and a few approvals, with zero quotes sent below margin."* (Replace the numbers with what we measure; see [Proving productivity](#proving-productivity).)

---

## Contents

1. [Team and ownership](#team-and-ownership)
2. [Architecture](#architecture)
3. [Shared contracts](#shared-contracts)
4. [Feature 1: Email-to-quote](#feature-1-email-to-quote)
5. [Feature 2: At-risk load watch](#feature-2-at-risk-load-watch)
6. [UI plan (Streamlit)](#ui-plan-streamlit)
7. [Proving productivity](#proving-productivity)
8. [Evals](#evals)
9. [Timeline and checkpoints](#timeline-and-checkpoints)
10. [Task lists by person](#task-lists-by-person)
11. [Demo script](#demo-script)
12. [Risks and fallbacks](#risks-and-fallbacks)

---

## Team and ownership

Each person owns separate files, so we rarely conflict.

| Role | Owns | Files | Strengths used |
|---|---|---|---|
| **Agent & evals lead** (me) | LLM steps, playbook flow, evals, baseline measurement, demo pitch | `meridian/models.py`, `meridian/llm.py`, `meridian/quote_playbook.py`, `meridian/risk_playbook.py`, `evals/` | Prompting, structured extraction, measuring quality, storytelling |
| **Tools & rules lead** (teammate A) | API client, pricing, risk detection rules, dry-run, MCP server | `meridian/client.py`, `meridian/pricing.py`, `meridian/risk_rules.py`, `server.py` | Python, REST APIs, business rules |
| **UI lead** (teammate B) | Streamlit app, scoreboard, demo mode | `app.py`, `ui/` | Python only. No TypeScript needed |

**Why Streamlit (not Gradio):** this is a dashboard with tabs, metric tiles, editable forms and progress steps, which Streamlit has built in (`st.tabs`, `st.metric`, `st.data_editor`, `st.status`). Gradio is better suited to a single chat or input/output box.

The React console in `meridian-agent/web` is parked. Keep it as a portfolio piece; don't spend hackathon time on it.

---

## Architecture

```
                 ┌──────────────────────── app.py (Streamlit) ─────────────────────────┐
                 │  Tab: Load watch        Tab: Email-to-quote       Tab: Scoreboard     │
                 └───────────┬───────────────────────┬─────────────────────────────────┘
                             │ plain Python calls     │
              risk_playbook.py                  quote_playbook.py          ← agent & evals lead
                │        │                        │        │
     risk_rules.py    llm.py (explain, draft)   llm.py (extract, draft)    pricing.py
                │                                          │                   │
                └──────────────── client.py (httpx, dry-run on writes) ────────┘  ← tools & rules lead
                                         │
                                  Meridian TMS API

   server.py (MCP) exposes the same client functions as tools → used by the
   "Investigate" button (LangGraph agent from agent.py), and by Claude Code for testing.
```

**Design decisions:**

- **Each playbook is a plain Python function**, not a free-roaming agent. Code does the steps and the rules; the LLM does the language work (reading emails, explaining risks, writing drafts). This is easier to build, test and debug, and safer to demo.
- **The agentic part is the "Investigate" button:** on any risk card, a LangGraph agent with read-only MCP tools digs into that load and explains what's going on. This reuses the hello-world agent we already have.
- **Streamlit calls Python directly**, so we don't need the FastAPI layer for this UI.
- **Every write goes through `client.py` with `DRY_RUN=1` by default.** In dry-run, writes return `{"ok": true, "dry_run": true, "would_send": {...}}` and nothing changes.
- **Verify after writes:** after a real write, re-read the record and confirm the change.

**Folder layout** (inside `meridian-agent/`):

```
meridian/
  __init__.py
  models.py          # shared Pydantic contracts          (agent & evals lead, written first)
  client.py          # API wrapper + dry-run              (tools & rules lead)
  pricing.py         # rate engine → price preview → quote (tools & rules lead)
  risk_rules.py      # risk detectors, pure functions     (tools & rules lead)
  llm.py             # model calls: extract, explain, draft (agent & evals lead)
  quote_playbook.py  # email → QuoteProposal → create      (agent & evals lead)
  risk_playbook.py   # scan → RiskItems → explain/draft    (agent & evals lead)
app.py               # Streamlit entry point              (UI lead)
ui/                  # UI components                      (UI lead)
evals/
  emails/            # sample customer emails (.txt)
  expected/          # expected extraction per email (.json)
  risk_labels.json   # hand-labeled loads from the snapshot
  run_evals.py
snapshots/           # API responses saved at the start (demo fallback)
baseline.json        # measured manual times and clicks
server.py, agent.py  # existing MCP server and agent
```

Add `streamlit` to `requirements.txt`.

---

## Shared contracts

Written in the first hour, in `meridian/models.py`. Everyone codes against these, so all three can work in parallel. **Change them only after telling the team.**

```python
from datetime import date
from typing import Literal
from pydantic import BaseModel, Field

class Location(BaseModel):
    city: str | None = None
    state: str | None = None          # 2-letter
    zip: str | None = None

class FreightLine(BaseModel):
    pieces: int = 1
    weight_lb: float | None = None
    length_in: float | None = None
    width_in: float | None = None
    height_in: float | None = None
    freight_class: float | None = None
    description: str | None = None

class QuoteRequest(BaseModel):
    """What the LLM extracts from a customer email."""
    customer_name: str | None = None
    mode: Literal["TL", "LTL", "EXPEDITE"] | None = None
    equipment_type: str | None = None
    origin: Location = Field(default_factory=Location)
    destination: Location = Field(default_factory=Location)
    pickup_date: date | None = None
    freight_lines: list[FreightLine] = []
    declared_value: float | None = None
    references: dict[str, str] = {}   # e.g. {"PO": "45812"}
    accessorials: list[str] = []      # charge codes, e.g. ["LIFTG"]
    notes: str | None = None          # anything relevant that didn't fit a field

class Check(BaseModel):
    name: str                         # e.g. "Margin floor", "Required reference: PO"
    passed: bool
    detail: str

class QuoteProposal(BaseModel):
    """Everything the UI needs to show before the user approves."""
    request: QuoteRequest
    customer_id: str | None
    missing_fields: list[str]         # fields still needed before pricing/creating
    checks: list[Check]
    buy_total: float | None = None
    sell_total: float | None = None
    gp_pct: float | None = None
    margin_pct_used: float | None = None
    buy_lines: list[dict] = []        # from the rate engine, passed through unchanged
    sell_lines: list[dict] = []       # from price-preview
    rate_id: str | None = None
    draft_reply: str = ""
    ready_to_create: bool = False     # all checks passed and nothing missing

class RiskItem(BaseModel):
    kind: Literal["CARRIER_COMPLIANCE", "MISSING_RATE_CON", "UNCOVERED_SOON",
                  "TRACKING_OVERDUE", "MABD_RISK", "APPOINTMENT_NEEDED",
                  "HOLD_RECONFIRM", "QUOTE_EXPIRING", "LOW_MARGIN"]
    severity: Literal["HIGH", "MEDIUM", "LOW"]
    score: float                      # for ranking; higher = more urgent
    record_type: Literal["shipment", "quote"]
    record_id: str
    title: str                        # short, e.g. "Carrier insurance expired"
    evidence: dict                    # the facts the rule used (dates, statuses, ids)
    suggested_action: str             # e.g. "Re-cover with a compliant carrier"
    explanation: str = ""             # filled by the LLM: one plain-English sentence
    draft_message: str = ""           # filled by the LLM: to customer / carrier / internal
    draft_audience: Literal["customer", "carrier", "internal", ""] = ""
```

**Tool result shape** (unchanged from the hello world): every `client.py` function returns `{"ok": true, "data": ...}` or `{"ok": false, "status": ..., "error": ..., "detail": ...}`.

---

## Feature 1: Email-to-quote

### Flow

| # | Step | Who does it | API |
|---|---|---|---|
| 1 | **Extract** fields from the email into `QuoteRequest` | LLM (structured output) | — |
| 2 | **Resolve customer**: name → `customer_id`; load `margin_floor_pct` and `required_references` | Code | `GET /search`, `GET /customers/{id}` |
| 3 | **Check** what's missing: TL needs origin/destination states; LTL needs weight plus dimensions or class; EXPEDITE needs weight; customer's required references (e.g. PO) present; pickup date not in the past | Code (`Check` list) | — |
| 4 | **Price** (no writes): rate engine gives the buy lines; price-preview gives sell lines and GP at margin = the customer's `margin_floor_pct` (or a higher target) | Code (`pricing.py`) | `POST /rate-engine/quotes`, `POST /quotes/price-preview` |
| 5 | **Draft reply**: price, validity, and a polite request for anything missing | LLM | — |
| 6 | **Show the proposal** for review: fields are editable; changing one re-runs steps 3–5 | UI | — |
| 7 | **On Approve:** create the quote, then price it with the same buy lines and margin; record the rate engine's `rate_id` | Code | `POST /quotes`, `POST /quotes/{id}/price` |
| 8 | **Verify**: re-read the quote; confirm status `QUOTED` and `gp_pct` at or above the floor | Code | `GET /quotes/{id}` |
| 9 | Optional **Mark as sent** (records it only; nothing is emailed) | Code | `POST /quotes/{id}/send` |

### Rules enforced in code (never by the LLM)

- Margin is on sell: sell = buy ÷ (1 − margin/100). Never price below `margin_floor_pct`.
- TL fuel surcharge passes through at cost, so blended GP lands slightly under target. Show this so it doesn't look like a bug.
- Declared value adds an insurance (INSD) line: 0.55% of declared value, minimum $25.
- Don't create a quote while a required reference is missing; put the question in the draft reply instead.
- The `/price` route takes `buy_lines` exactly as the rate engine returned them; pass them through unchanged.
- Rate engine `409` (no rate for the lane) or `422` (missing weight or dimensions) becomes a failed `Check` with the API's `detail`, not a crash.

### LLM prompts (in `llm.py`)

- **`extract_quote_request(email) -> QuoteRequest`**: use `init_chat_model(MODEL).with_structured_output(QuoteRequest)`. The instructions say: extract only what's stated; leave unknown fields empty; convert relative dates ("next Tuesday") using today's date, which is passed into the prompt; map words to codes ("reefer" → the reefer equipment code, "liftgate" → the liftgate charge code).
- **`draft_quote_reply(proposal) -> str`**: short and professional. States the price and validity; if anything is missing, asks for exactly those items.

### Sample emails to prepare (also the eval set)

1. A clean TL request (everything present).
2. LTL with weight and dimensions.
3. LTL missing dimensions and class → asks for them.
4. A customer that requires a PO, with no PO given → no quote; the reply asks for the PO.
5. Same customer with a PO → quote created.
6. A relative date ("next Tuesday", "end of week").
7. Declared value given → insurance line appears.
8. An accessorial mentioned ("needs liftgate at delivery").
9. Unknown customer name → asks who the customer is (or offers matches from search).
10. Messy formatting (forwarded thread, signature, typos).
11. Two loads in one email → handle the first and flag the second. (Stretch: handle both.)
12. Expedite with weight → priced.

Build these from the customers and lanes in the live data once it's reset.

---

## Feature 2: At-risk load watch

### Flow

| # | Step | Who does it | API |
|---|---|---|---|
| 1 | **Gather** open shipments, the tracking queue, carriers, facilities (must-arrive-by programs) and pending quotes | Code (`client.py`) | `GET /shipments?status=BOOKED,COVERED,DISPATCHED,AT_PICKUP,PICKED_UP,IN_TRANSIT,AT_DELIVERY,ON_HOLD` (page through all), `GET /shipments?queue=tracking`, `GET /vendors`, `GET /facilities`, `GET /quotes?status=QUOTED` and `SENT`, `GET /dashboard/counts` |
| 2 | **Detect**: run every rule; each returns `RiskItem`s with the evidence it used | Code (`risk_rules.py`) | — |
| 3 | **Rank** by score: severity, then time until pickup or delivery, then money at risk | Code | — |
| 4 | **Explain and draft** for the top ~10: one plain-English sentence, plus a message to the right audience | LLM | — |
| 5 | **Show** as ranked cards | UI | — |
| 6 | **Act** (on Approve): add an internal note with the explanation, or record a check call | Code | `POST /shipments/{id}/notes`, `POST /shipments/{id}/check-calls` |
| 7 | **Investigate** (button): the LangGraph agent with read-only MCP tools digs into one load and answers "what's going on and what should I do?" | Agent | read tools only |

### Detection rules (`risk_rules.py`, pure functions; easy to unit test)

| Kind | Rule | Severity | Suggested action |
|---|---|---|---|
| `CARRIER_COMPLIANCE` | Open load's carrier is not ACTIVE, is DO_NOT_USE, has no CARGO policy, or the cargo policy is RED | HIGH | Re-cover with a compliant carrier |
| | …or the cargo policy is YELLOW (expires within 30 days) | MEDIUM | Request an updated certificate of insurance |
| `MISSING_RATE_CON` | COVERED with no generated rate confirmation, so it can't be dispatched | HIGH if pickup ≤ 1 day away, else MEDIUM | Generate the rate confirmation |
| `UNCOVERED_SOON` | BOOKED (no carrier) and pickup within 2 days | HIGH | Find a carrier |
| `TRACKING_OVERDUE` | In the tracking queue with `overdue: true` | MEDIUM (HIGH if delivering today) | Make a check call |
| `MABD_RISK` | A stop has `mabd_date` and the appointment is NEEDED or the window ends after it; the facility's `mabd_program_note` describes the chargeback | HIGH | Book or move the appointment before the MABD |
| `APPOINTMENT_NEEDED` | Appointment NEEDED, pickup or delivery within 3 days | MEDIUM | Book the appointment |
| `HOLD_RECONFIRM` | ON_HOLD with a pickup date today or past | MEDIUM | Re-confirm the pickup date with the customer |
| `QUOTE_EXPIRING` | QUOTED/SENT and `valid_until` within 2 days | LOW | Follow up with the customer |
| `LOW_MARGIN` | Open load with `gp_pct` below the customer's floor, or negative | MEDIUM (HIGH if negative) | Review the buy rate |

Field names to double-check against the reset data: `rate_con_doc_id` / `rate_con_number`, `stops[].appointment.status`, `stops[].mabd_date`, `stops[].scheduled_window.latest_local`, `insurance[].insurance_status`, and the tracking queue's `overdue`.

### LLM prompts (in `llm.py`)

- **`explain_risk(item) -> str`**: one sentence a busy broker can act on, using only the facts in `evidence`.
- **`draft_risk_message(item) -> (audience, text)`**: pick the audience by kind (customer for holds, delays and expiring quotes; carrier for insurance and rate confirmations; internal otherwise). Two to four sentences.
- Batch the top ~10 items into one call to save time and tokens.

---

## UI plan (Streamlit)

The UI's job is to make the productivity gain **visible**: the work is found for you, every item has a reason, and the scoreboard counts what was saved.

### Layout

```
┌───────────────────────────────────────────────────────────────────────────────┐
│ Broker Desk Copilot                                  Dry-run: ON ◉   Model: … │
├───────────────┬───────────────────────────────────────────────────────────────┤
│ SIDEBAR       │  [ Load watch ]  [ Email-to-quote ]  [ Manual vs copilot ]    │
│               │                                                               │
│ Today's       │  (tab content)                                                │
│ scoreboard    │                                                               │
│  Items done 7 │                                                               │
│  Time saved   │                                                               │
│   ~52 min     │                                                               │
│  Issues       │                                                               │
│   caught 4    │                                                               │
│  Quotes below │                                                               │
│   margin 0    │                                                               │
│               │                                                               │
│ Demo mode ☐   │                                                               │
│ (use snapshot)│                                                               │
└───────────────┴───────────────────────────────────────────────────────────────┘
```

### Tab 1: Load watch (open this first in the demo)

- **Top row:** `st.metric` tiles from `GET /dashboard/counts` (To cover, Tracking overdue, Delivering today…). Next to them: **"Copilot found N issues across M loads in X seconds."**
- **"Scan my loads" button:** `st.status` shows live progress: *Loading 47 open loads… Checking carriers… Checking appointments… Ranking… Writing explanations…*
- **Ranked risk cards:** one `st.container(border=True)` per `RiskItem`:
  - a severity color bar and badge (HIGH red, MEDIUM amber, LOW gray) with the title and record ID
  - **Why:** the LLM's explanation
  - **Evidence:** a small key/value table from `evidence`, so the claim can be checked
  - **Suggested action**
  - **Draft message** in an editable `st.text_area`, labeled with its audience
  - **Buttons:** ✅ *Approve & log note*, 🔍 *Investigate*, ⏭ *Dismiss*
- **Filters:** severity and kind (`st.pills` or `st.multiselect`).
- Approving or dismissing a card moves it to a "Done" list and updates the scoreboard.

### Tab 2: Email-to-quote

Two columns:

- **Left: Inbox**
  - Sample email picker (`st.selectbox`), plus a text area to paste any email.
  - A **"Build quote"** button.
- **Right: Proposal**, filled in step by step under `st.status`: *Reading email → Finding customer → Checking requirements → Pricing → Drafting reply*.
  - **Extracted fields** in an editable form (`st.data_editor` or inputs). Missing fields are highlighted, and editing re-runs the checks.
  - **Checks list:** ✓/✗ for customer found, required references, freight details complete, margin at or above the floor.
  - **Price breakdown:** buy and sell lines, `st.metric` for Buy / Sell / GP%, and a note when TL fuel pulls GP slightly under target.
  - **Draft reply** in an editable text area.
  - **Buttons:** ✅ *Create quote* (disabled until `ready_to_create`), 📤 *Mark as sent*, 📋 *Copy reply*.
  - After creating: the new quote number, its status, and "verified ✓".
- A small timer: **"Built in 6.2 s"** next to **"Manual baseline: 7 min 30 s"**.

### Tab 3: Manual vs copilot

The productivity evidence on one screen:

| Task | Manual (measured) | Copilot | Saved |
|---|---|---|---|
| Find what needs attention this morning | 14 screens, 22 min | 1 click, 25 s + review | … |
| Price and create a quote from an email | 9 screens, 7.5 min | 1 paste + 1 approve, ~40 s | … |
| Quotes sent below margin | possible | blocked: 0 | — |

Plus the eval results table (pass rates), and optionally a bar chart of time per task (`st.bar_chart`).

### Implementation notes for the UI lead

- Keep state in `st.session_state` (`risk_items`, `done_ids`, `proposal`, `scoreboard`). Streamlit reruns the whole script on every click.
- Wrap slow calls in `st.status` and cache read-only scans with `st.cache_data(ttl=60)`.
- **Demo mode:** a sidebar toggle that loads `snapshots/*.json` instead of calling the API, so the demo still works if the API or wifi is down.
- **Build against fake data first:** a `fake_risk_items()` and `fake_proposal()` that return realistic `RiskItem` and `QuoteProposal` objects. The UI can be finished before the playbooks are.
- Run with `streamlit run app.py`.

---

## Proving productivity

Judges need numbers, not adjectives. Collect them early.

1. **Baseline (first 90 minutes, agent & evals lead):** in the real web app, time and count clicks and screens for:
   - working out which loads need attention this morning (insurance, appointments, tracking, rate confirmations)
   - pricing and creating one quote from an email
   Save the results to `baseline.json`:
   ```json
   {"quote_from_email": {"minutes": 7.5, "screens": 9, "clicks": 41},
    "morning_triage":   {"minutes": 22,  "screens": 14, "clicks": 63},
    "per_risk_item_minutes": 3}
   ```
2. **Copilot timings:** the app records how long each run takes, plus approximate review time.
3. **Scoreboard:** time saved = baseline − (copilot run time + review time), summed over the items handled. **Label it "estimated, based on our measured baseline."** Be honest about the method.
4. **Quality numbers from evals:** extraction accuracy, risks found versus missed, and **zero quotes below margin**.

---

## Evals

Owned by the agent & evals lead. Run `python evals/run_evals.py` after every significant change, and keep a log of scores over time; the improvement story is useful for the demo and for interviews.

**Email-to-quote:**

| Metric | How | Target |
|---|---|---|
| Field accuracy | Compare extracted `QuoteRequest` fields with `evals/expected/*.json`, field by field | ≥ 90% |
| Missing-field detection | Does `missing_fields` match the expected list? | 100% on the "missing" cases |
| Margin safety | `gp_pct` ≥ floor for every priced case (allow for the TL fuel note) | 100% |
| No unsafe creates | Never `ready_to_create` while a required reference is missing | 100% |

**Load watch:**

| Metric | How | Target |
|---|---|---|
| Recall | Hand-label ~15 loads from the snapshot (`evals/risk_labels.json`); did every expected risk appear? | 100% for HIGH |
| Precision | Of the flagged items, how many are real? | ≥ 85% |
| Explanation faithfulness | Spot-check: does each explanation use only facts from `evidence`? | No invented facts |

**Rule of thumb:** write the expected results **before** looking at what the code produces.

---

## Timeline and checkpoints

Times are percentages of the event.

| Phase | Agent & evals lead | Tools & rules lead | UI lead | Checkpoint (all three) |
|---|---|---|---|---|
| **0. Kickoff** (first ~45 min) | Write `models.py` and push it; start the baseline timings | Snapshot all data to `snapshots/`; `client.py` read functions | Streamlit skeleton: tabs, sidebar, fake data | Everyone runs `streamlit run app.py` and sees fake cards |
| **1. First slice** (to ~30%) | `extract_quote_request` + 5 sample emails; `explain_risk` | `pricing.py` (rate engine → price-preview); 4 rules: compliance, rate confirmation, uncovered-soon, tracking | Load watch cards and Email-to-quote layout, using fake data | Paste an email → see real extracted fields and a real price in the UI |
| **2. End to end** (to ~60%) | Both playbooks wired together; drafts; eval script v1 | Remaining rules; write functions with dry-run (`create_quote`, `price_quote`, `add_note`, `check_call`); MCP read tools for Investigate | Real data wired in; approve buttons; scoreboard; demo mode | Both features run end to end in dry-run, from the UI |
| **3. Prove it** (to ~80%) | Full eval sets; fix the top failures; finish baseline numbers | Edge cases (409/422 handling), verify-after-write, unit tests for rules | Manual-vs-copilot tab, polish, loading states | **Feature freeze.** Evals green on core cases; scoreboard shows real numbers |
| **4. Demo** (last ~20%) | Pitch and slides (problem → demo → numbers) | One real (non-dry-run) quote creation, then back to dry-run | Demo mode tested; record a backup video | Two full rehearsals |

At each checkpoint take 10 minutes together: show what works, review failing evals, re-plan. If a checkpoint slips, **cut scope** (see the cut list in [Risks and fallbacks](#risks-and-fallbacks)), never the demo's reliability.

---

## Task lists by person

### Agent & evals lead (me)

- [ ] `meridian/models.py`: contracts above; push within the first 45 minutes
- [ ] Baseline timings in the real web app → `baseline.json`
- [ ] `llm.py`: `extract_quote_request`, `draft_quote_reply`, `explain_risk`, `draft_risk_message` (batched)
- [ ] `quote_playbook.py`: `build_proposal(email) -> QuoteProposal`, `create_quote(proposal) -> result` (create → price → verify)
- [ ] `risk_playbook.py`: `scan() -> list[RiskItem]` (gather → rules → rank → explain/draft top N), `act(item, action)`
- [ ] Hook the "Investigate" button to the existing LangGraph agent with read-only tools
- [ ] `evals/`: 12 emails with expected JSON, 15 labeled loads, `run_evals.py` printing a score table
- [ ] Score log over the day (before/after each fix)
- [ ] Demo pitch and the numbers slide

### Tools & rules lead (teammate A)

- [ ] `snapshots/`: save open shipments, tracking queue, vendors, facilities, quotes and customers as JSON at the start
- [ ] `client.py` reads: `search`, `get_customer`, `list_shipments(status, paginate=True)`, `tracking_queue`, `list_vendors`, `list_facilities`, `list_quotes(status)`, `dashboard_counts`, `get_shipment`, `get_quote`
- [ ] `client.py` writes (dry-run by default): `create_quote`, `price_quote`, `send_quote`, `add_shipment_note`, `record_check_call`
- [ ] `pricing.py`: `rate_lane(request) -> rate result` and `preview_price(buy_lines, margin, declared_value)`; turn 409/422 into readable errors
- [ ] `risk_rules.py`: one function per rule in the table; each returns `list[RiskItem]` with `evidence`
- [ ] Unit tests for each rule against the snapshot (`pytest`)
- [ ] `server.py`: expose read functions as MCP tools for the Investigate agent

### UI lead (teammate B)

- [ ] `app.py` skeleton: title, sidebar (dry-run toggle, model, scoreboard, demo mode), three tabs
- [ ] Fake data helpers so the UI works before the playbooks do
- [ ] Load watch: dashboard tiles, Scan button with `st.status`, ranked cards with evidence, editable draft, Approve / Investigate / Dismiss
- [ ] Email-to-quote: email picker and paste box, step-by-step status, editable fields, checks list, price metrics, draft reply, Create / Mark as sent
- [ ] Scoreboard: items done, estimated time saved (from `baseline.json`), issues caught, quotes below margin
- [ ] Manual vs copilot tab: comparison table, eval results, optional chart
- [ ] Demo mode reading from `snapshots/`
- [ ] Backup screen recording of the full demo

---

## Demo script

About 4 minutes.

1. **The problem (30s):** "This is a broker's morning in the TMS." Show the manual baseline: screens, clicks, minutes. "And quoting means re-typing every email into forms."
2. **Load watch (75s):** click *Scan my loads* and watch the progress steps. Walk through the top three cards: a carrier with expired insurance on an open load, a must-arrive-by delivery with no appointment (chargeback risk), and a covered load missing its rate confirmation. Approve one; the scoreboard ticks up. Click *Investigate* on one to show the agent digging in.
3. **Email-to-quote (75s):** paste an email from a customer that requires a PO, with no PO. The copilot extracts everything, prices it, **refuses to create the quote**, and drafts a reply asking for the PO. Paste the follow-up with the PO and approve: the quote is created, priced at or above the floor, and verified.
4. **The numbers (40s):** the Manual vs copilot tab (time saved, issues caught, zero quotes below margin) and the eval scores.
5. **Close (20s):** what we'd build next (cover and dispatch, batch actions, email integration).

---

## Risks and fallbacks

| Risk | Fallback |
|---|---|
| API slow or down during the demo | Demo mode from `snapshots/` |
| Model extracts a field wrong | Editable fields plus checks; the user fixes it in one click, and the eval set shows how rare it is |
| Rate engine has no rate for a lane (409) | Show the failed check with the API's reason; suggest entering a buy rate manually |
| Field names differ after the data reset | Tools lead checks the field names in the snapshot during phase 0 |
| Streamlit reruns lose state | Everything important lives in `st.session_state` |
| Running out of time | **Cut in this order:** Investigate button → Manual vs copilot chart (keep the table) → two-loads-in-one-email → `LOW_MARGIN` and `QUOTE_EXPIRING` rules. **Never cut** the scoreboard numbers, the margin check, or demo mode |
| Accidental real writes | `DRY_RUN=1` by default; only the tools lead turns it off, for one planned real run |

---

## Working agreements

- **Main always runs.** Pull before starting, push small working changes often, never push anything that breaks `streamlit run app.py`.
- **Contracts first.** Changes to `models.py` are announced to the team.
- **The eval set decides** whether something works.
- **Dry-run stays on**, and we never load-test the shared API.
- **Stuck for 20 minutes?** Say so.
