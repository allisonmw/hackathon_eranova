"""UI-only deterministic prototype adapter. No writes, network calls, or LLM math."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP


def money(value):
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Shipment:
    identifier: str
    origin: str
    destination: str
    carrier: str
    status: str
    sell: float | None
    total_cost: float | None
    carrier_cost: float | None
    arrival: datetime | None
    window_start: datetime | None
    window_end: datetime | None
    deadline: datetime | None
    source: str


@dataclass(frozen=True)
class Scenario:
    disruption: str = 'Carrier breakdown'
    delay: float = 6
    replacement_cost: float = 2400
    replacement_delay: float = 2
    retained_charge: float | None = None
    recovery_fee: float | None = None
    reschedule_fee: float | None = None
    reschedule_delay: float = 8
    original_compliance: str = 'Unconfirmed'
    replacement_compliance: str = 'Unconfirmed'
    timing_confirmed: bool = False
    reschedule_accepted: bool = False
    replacement_name: str = 'Proposed replacement carrier'


@dataclass(frozen=True)
class Option:
    key: str
    title: str
    total: Decimal | None
    gp: Decimal | None
    margin: Decimal | None
    gp_change: Decimal | None
    arrival: datetime | None
    compliance: str
    timing: str
    unknowns: tuple[str, ...]
    breakdown: tuple[tuple[str, Decimal | None], ...]
    actions: tuple[str, ...]
    fields: tuple[str, ...]
    complexity: int

    @property
    def supported(self):
        return self.compliance == 'Confirmed compliant' and self.timing == 'Feasible' and not self.unknowns


def original_financials(s):
    if s.sell is None or s.total_cost is None:
        return None, None
    gp = money(s.sell) - money(s.total_cost)
    return gp, (gp / money(s.sell) * 100).quantize(Decimal('.01')) if s.sell > 0 else None


def evaluate(s: Shipment, c: Scenario) -> list[Option]:
    """Replace only the carrier portion; retain all other original costs.

    None fees remain unknown. A displayed known subtotal is never a final cost.
    Rescheduling may move the appointment, but never waives a must-arrive-by deadline.
    """
    original_gp, _ = original_financials(s)
    specs = []
    if c.disruption != 'Carrier cancellation':
        specs.append(('wait', 'Wait for original carrier', c.delay, c.original_compliance,
                      [('Original total cost', s.total_cost), ('Incremental recovery charge', c.recovery_fee)],
                      ['Confirm repair time and driver hours', 'Notify customer and confirm arrival window'],
                      ['ETA / tracking', 'Internal recovery note'], 1))
    other = (money(s.total_cost) - money(s.carrier_cost)) if s.total_cost is not None and s.carrier_cost is not None else None
    specs.append(('replace', 'Replace the carrier', c.replacement_delay, c.replacement_compliance,
                  [('Other original costs (retained)', other), ('Replacement carrier quote', c.replacement_cost),
                   ('Original-carrier retained charge / cancellation fee', c.retained_charge)],
                  [f'Confirm quote and availability with {c.replacement_name}', 'Confirm original-carrier release and retained charges',
                   'Verify insurance, authority, driver hours and transfer feasibility'],
                  ['Carrier assignment', 'Buy charges / rate confirmation', 'ETA / tracking'], 3))
    if c.disruption != 'Carrier cancellation':
        specs.append(('reschedule', 'Reschedule delivery', c.delay + c.reschedule_delay, c.original_compliance,
                      [('Original total cost', s.total_cost), ('Incremental recovery charge', c.recovery_fee),
                       ('Rescheduling fee', c.reschedule_fee)],
                      ['Obtain customer and facility approval for the later appointment', 'Confirm detention and rescheduling charges'],
                      ['Delivery appointment', 'ETA / tracking', 'Accessorial charges'], 2))
    result = []
    for key, title, delay, compliance, lines, actions, fields, complexity in specs:
        unknown = [name + ' is unknown' for name, value in lines if value is None]
        if other is not None and other < 0 and key == 'replace':
            unknown.append('Carrier cost exceeds original total cost; reconcile cost components')
        if s.sell is None or s.sell <= 0:
            unknown.append('A positive customer sell price is required')
        if not c.timing_confirmed:
            unknown.append('Arrival, appointment, deadline and transit assumptions need confirmation')
        if compliance != 'Confirmed compliant':
            unknown.append('Carrier compliance evidence needs verification' if compliance == 'Unconfirmed' else 'Carrier fails a hard compliance requirement')
        arrival = s.arrival + timedelta(hours=delay) if s.arrival else None
        if key == 'reschedule' and not c.reschedule_accepted:
            unknown.append('Later appointment has not been accepted by customer and facility')
        if not arrival or not s.window_end:
            timing = 'Unknown'
            unknown.append('Arrival or appointment window is missing')
        elif s.window_start and s.window_start > s.window_end:
            timing = 'Unknown'
            unknown.append('Appointment start is later than its end')
        elif s.deadline and arrival > s.deadline:
            timing = 'Misses deadline'
        elif key != 'reschedule' and arrival > s.window_end:
            timing = 'Misses appointment'
        else:
            timing = 'Feasible'
        values = tuple((name, money(value) if value is not None else None) for name, value in lines)
        total = sum((value for _, value in values), Decimal(0)) if all(value is not None for _, value in values) else None
        gp = money(s.sell) - total if s.sell is not None and total is not None else None
        margin = (gp / money(s.sell) * 100).quantize(Decimal('.01')) if gp is not None and s.sell > 0 else None
        change = gp - original_gp if gp is not None and original_gp is not None else None
        result.append(Option(key, title, total, gp, margin, change, arrival, compliance, timing,
                             tuple(unknown), values, tuple(actions), tuple(fields), complexity))
    return result


def recommend(options):
    """Hard compliance and delivery feasibility first, then evidence, GP and complexity."""
    eligible = [o for o in options if o.supported and o.gp is not None]
    return max(eligible, key=lambda o: (o.gp, -o.complexity)) if eligible else None


def demo_shipments():
    day = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return [Shipment('FR-7300172', 'Chicago, IL', 'Dallas, TX', 'Blue Line Transport', 'IN_TRANSIT',
                     4200, 3000, 2800, day, day - timedelta(hours=1), day + timedelta(hours=4), day + timedelta(hours=6), 'Demo fixture'),
            Shipment('FR-7300181', 'Atlanta, GA', 'Charlotte, NC', 'Summit Freight', 'COVERED',
                     2600, 1900, 1750, day + timedelta(hours=1), day, day + timedelta(hours=3), day + timedelta(hours=5), 'Demo fixture'),
            Shipment('FR-7300190', 'Columbus, OH', 'Nashville, TN', 'Northstar Carriers', 'IN_TRANSIT',
                     3400, 2500, 2300, day + timedelta(hours=2), day, day + timedelta(hours=6), None, 'Demo fixture')]


def _timestamp(value):
    if not isinstance(value, str) or 'T' not in value:
        return None  # date-only and timezone-free local values cannot establish an ETA
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def _amount(value):
    try:
        result = float(value)
        import math
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def from_tms(row, source):
    """Conservative provisional mapping; absent evidence remains None. See REWIND_INTERFACES.md."""
    return Shipment(str(row['shipment_id']),
                    ', '.join(filter(None, [row.get('shipper_city'), row.get('shipper_state')])) or 'Unknown origin',
                    ', '.join(filter(None, [row.get('consignee_city'), row.get('consignee_state')])) or 'Unknown destination',
                    str(row.get('carrier_id') or row.get('vendor_id') or 'Unassigned / unknown'),
                    row.get('status', 'Unknown'), _amount(row.get('sell_total')), _amount(row.get('buy_total')),
                    _amount(row.get('carrier_cost')), _timestamp(row.get('estimated_arrival')),
                    _timestamp(row.get('delivery_window_start')), _timestamp(row.get('delivery_window_end')),
                    _timestamp(row.get('must_arrive_by')), source)
