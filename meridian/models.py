"""Shared contracts. Owner: agent & evals lead.

Everyone codes against these, so all three can work in parallel.
Change them only after telling the team.
"""

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


RiskKind = Literal["CARRIER_COMPLIANCE", "MISSING_RATE_CON", "UNCOVERED_SOON",
                   "TRACKING_OVERDUE", "MABD_RISK", "APPOINTMENT_NEEDED",
                   "HOLD_RECONFIRM", "QUOTE_EXPIRING", "LOW_MARGIN"]
Severity = Literal["HIGH", "MEDIUM", "LOW"]


class RiskItem(BaseModel):
    kind: RiskKind
    severity: Severity
    score: float                      # for ranking; higher = more urgent
    record_type: Literal["shipment", "quote"]
    record_id: str
    title: str                        # short, e.g. "Carrier insurance expired"
    evidence: dict                    # the facts the rule used (dates, statuses, ids)
    suggested_action: str             # e.g. "Re-cover with a compliant carrier"
    explanation: str = ""             # filled by the LLM: one plain-English sentence
    draft_message: str = ""           # filled by the LLM: to customer / carrier / internal
    draft_audience: Literal["customer", "carrier", "internal", ""] = ""
