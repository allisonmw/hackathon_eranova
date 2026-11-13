"""Shared contracts. Owner: agent & evals lead.

Everyone codes against these, so all three can work in parallel.
Change them only after telling the team.
"""

from datetime import date, datetime
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
                   "HOLD_RECONFIRM", "QUOTE_EXPIRING", "LOW_MARGIN", "LATE_ETA"]
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


# --------------------------------------------------------------------------- tracking

TrackStatus = Literal["ON_TIME", "AT_RISK", "LATE", "ARRIVED", "NOT_TRACKED"]


class PositionReport(BaseModel):
    """Where a truck was at a moment: a check call, or a known stop (pickup/delivery facility)."""
    at: datetime                      # UTC
    city: str | None = None
    state: str | None = None
    lat: float | None = None
    lon: float | None = None
    approximate: bool = False         # city not in the coordinate table: placed at the state's centre
    source: Literal["check_call", "pickup", "delivery"] = "check_call"
    reported_eta: datetime | None = None   # the ETA the driver/carrier gave on this call (UTC)
    reason_code: str | None = None    # late reason code reported (AI mechanical, AO weather, ...)
    hours_left: float | None = None   # driving hours left, if the call note says ("6 h left")
    note: str = ""


class TrackingStatus(BaseModel):
    """Where a load is and whether it will make its delivery deadline, as of `as_of`.
    Built only from TMS data: status, milestones and check calls (meridian.tracking)."""
    shipment_id: str
    as_of: datetime                   # UTC
    stage: str                        # TMS status: DISPATCHED, AT_PICKUP, IN_TRANSIT, AT_DELIVERY, ...
    status: TrackStatus
    position: PositionReport | None = None   # latest known position
    trail: list[PositionReport] = []  # every known position, oldest first
    hours_since_report: float | None = None
    miles_remaining: float | None = None
    eta: datetime | None = None       # our estimate at legal driving hours (UTC); actual arrival once arrived
    driver_eta: datetime | None = None   # the latest ETA the driver/carrier reported
    eta_gap_minutes: int | None = None   # our ETA − driver ETA; positive = the driver's ETA looks optimistic
    departed_at: datetime | None = None
    arrived_at: datetime | None = None
    window_end: datetime | None = None   # delivery window end (UTC)
    mabd_end: datetime | None = None     # end of the must-arrive-by day (UTC)
    minutes_late: int | None = None      # vs window end; negative = minutes of slack
    minutes_late_mabd: int | None = None
    delivery_tz: str | None = None
    exceptions: list[str] = []        # plain-English reasons it needs attention
    assumptions: list[str] = []       # what our ETA assumed


class FeedEvent(BaseModel):
    at: datetime                      # UTC
    shipment_id: str
    source: Literal["check_call", "status", "appointment", "copilot"]
    level: Literal["info", "warning", "alert"] = "info"
    text: str
