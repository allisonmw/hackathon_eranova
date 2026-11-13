"""UI-owned, deterministic cost-exposure prototype. No network calls or TMS writes."""
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

ZERO = Decimal('0.00')
RATES = {'detention': Decimal('75'), 'free_hours': Decimal('2'), 'layover': Decimal('250'),
         'appointment': Decimal('50'), 'labor': Decimal('42'), 'storage': Decimal('25'),
         'otif_pct': Decimal('3'), 'capital_pct': Decimal('9')}


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except InvalidOperation:
        return None


def dollars(value):
    n = number(value)
    return n.quantize(Decimal('.01'), rounding=ROUND_HALF_UP) if n is not None else None


def product(*values):
    nums = [number(v) for v in values]
    if any(n is None or n < 0 for n in nums):
        return None
    result = Decimal(1)
    for n in nums:
        result *= n
    return dollars(result)


def detention(dwell, rate, free):
    nums = [number(v) for v in (dwell, rate, free)]
    if any(n is None or n < 0 for n in nums):
        return None
    return product(max(ZERO, nums[0] - nums[2]), nums[1])


def capital_cost(balance, days, annual_pct):
    nums = [number(v) for v in (balance, days, annual_pct)]
    if any(n is None or n < 0 for n in nums):
        return None
    return dollars(nums[0] * nums[1] * nums[2] / Decimal('36500'))


def penalty(policy_applies, violation, value, pct):
    # Selecting a conditional charge without its evidence must not create a zero.
    if not policy_applies or violation is None:
        return None
    if not violation:
        return ZERO
    return product(value, pct, Decimal('.01'))


@dataclass(frozen=True)
class Charge:
    key: str
    label: str
    code: str
    amount: Decimal | None
    reason: str
    recovery: bool = False
    overlap: str = 'Unconfirmed'   # Unconfirmed / Same event total / Separate incremental event


@dataclass(frozen=True)
class Line:
    charge: Charge
    recorded: Decimal | None
    incremental: Decimal | None
    note: str


@dataclass(frozen=True)
class Exposure:
    lines: tuple[Line, ...]
    gross: Decimal | None
    recoveries: Decimal | None
    net: Decimal | None
    original_gp: Decimal | None
    adjusted_gp: Decimal | None


def ledger_matches(shipment, code, recovery=False):
    ledger = 'customer_charges' if recovery else 'vendor_costs'
    return [row for row in (shipment.get(ledger) or [])
            if str(row.get('charge_code', '')).upper() == code.upper()
            and str(row.get('status', '')).upper() != 'CANCELLED']


def _sum(values):
    values = list(values)
    return None if any(v is None for v in values) else dollars(sum(values, ZERO))


def estimate(shipment, charges):
    """Scope reconciliation is explicit; matching a code alone cannot establish an event match.

    Same-event input is the full event total, not a second incremental charge.
    Existing buy/sell lines are already reflected in baseline GP; only the excess
    is subtracted/added. Unknown ledger values or event scope block a full total.
    """
    charges = list(charges)
    counts = Counter((c.code.upper(), c.recovery) for c in charges if c.amount != ZERO)
    lines = []
    seen = set()
    for c in charges:
        if c.key in seen:
            raise ValueError('A consequence may only be modeled once: ' + c.key)
        seen.add(c.key)
        rows = ledger_matches(shipment, c.code, c.recovery)
        recorded = _sum(number(r.get('amount')) for r in rows)
        amount = dollars(c.amount)
        if amount is not None and amount < 0:
            raise ValueError('Enter nonnegative costs/recoveries separately')
        if amount != ZERO and counts[(c.code.upper(), c.recovery)] > 1:
            incremental, note = None, 'Combine repeated same-ledger charge codes into one full-event entry to avoid duplicate accounting.'
        elif amount == ZERO:
            incremental, note = ZERO, 'No additional modeled amount.'
        elif amount is None:
            incremental, note = None, 'Amount or triggering evidence is unknown.'
        elif not rows or c.overlap == 'Separate incremental event':
            incremental = amount
            note = 'User states a separate, incremental event.' if rows else 'No matching non-cancelled ledger code; confirm contract and unposted charges.'
        elif c.overlap == 'Same event total' and recorded is not None:
            incremental = max(ZERO, amount - recorded)
            note = 'Only modeled total above already-recorded amount is additional; no refunds inferred.'
        else:
            incremental, note = None, 'Reconcile the matching TMS lines and event scope before calculating additional exposure.'
        lines.append(Line(c, recorded, incremental, note))
    gross = _sum(l.incremental for l in lines if not l.charge.recovery)
    recoveries = _sum(l.incremental for l in lines if l.charge.recovery)
    net = dollars(gross - recoveries) if gross is not None and recoveries is not None else None
    gp = number(shipment.get('gp_amount'))
    if gp is None:
        sell, buy = number(shipment.get('sell_total')), number(shipment.get('buy_total'))
        gp = sell - buy if sell is not None and buy is not None else None
    gp = dollars(gp)
    adjusted = dollars(gp - net) if gp is not None and net is not None else None
    return Exposure(tuple(lines), gross, recoveries, net, gp, adjusted)


def lateness_context(status, additional_hours):
    """What-if shift of the existing tracking result, not a new routing/ETA algorithm."""
    hours = number(additional_hours)
    if hours is None or hours < 0:
        raise ValueError('Additional delay must be nonnegative')
    minutes = hours * 60
    return {
        'arrival': status.eta + timedelta(minutes=float(minutes)) if status.eta else None,
        'minutes_late': number(status.minutes_late) + minutes if status.minutes_late is not None else None,
        'minutes_late_mabd': number(status.minutes_late_mabd) + minutes if status.minutes_late_mabd is not None else None,
    }
