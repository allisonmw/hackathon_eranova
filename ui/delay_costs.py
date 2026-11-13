"""Selected-shipment Delay Cost Exposure panel. All outputs are scenario previews."""
from dataclasses import replace

import streamlit as st

from meridian import geo
from . import automatic_delay_costs
from .delay_cost_model import (Charge, RATES, capital_cost, detention, estimate, lateness_context,
                               ledger_matches, penalty, product)

CATEGORIES = {
    'detention': 'Excess loading / unloading dwell', 'layover': 'Additional overnight waiting',
    'appointment': 'Appointment rescheduling', 'penalty': 'OTIF / MABD chargeback',
    'labor': 'Additional broker operations labor', 'storage': 'Storage',
    'extra': 'Additional carrier charge', 'billing': 'Delayed billing / paperwork',
}
MITIGATIONS = {
    'detention': 'Capture in/out timestamps and free-time terms; request dock priority and document any customer rebill.',
    'layover': 'Confirm the next receiving slot, overnight requirement and carrier terms; exclude dwell already billed as detention.',
    'appointment': 'Contact the receiver before the window closes; obtain a new slot and confirm who pays the fee.',
    'penalty': 'Verify the facility/customer policy and PO value; preserve arrival evidence and review waiver/dispute rights.',
    'labor': 'Assign a single recovery owner and batch updates to avoid duplicate operations effort.',
    'storage': 'Confirm facility capacity, storage duration and responsibility before authorizing storage.',
    'extra': 'Obtain an itemized carrier quote and confirm contractual responsibility and any customer pass-through.',
    'billing': 'Collect POD and receipts promptly; verify the blocked invoice balance and release date.',
}


def cash(value):
    return 'Unknown' if value is None else f'${value:,.2f}'


def _late(value):
    return 'Unknown' if value is None else f'{max(0, value) / 60:.2f} h late' + (f' ({-value / 60:.2f} h slack)' if value < 0 else '')


def render(tr, sid):
    ship, status = tr.shipments[sid], tr.statuses[sid]
    st.subheader('Delay Cost Exposure')
    st.caption('Read-only disruption cost exposure · No bookings or TMS writes. '
               'Late linehaul arrival alone does not incur detention, layover, storage or a chargeback.')
    st.markdown(f'**Shipment {sid}** · {status.stage} · tracking status **{status.status}** · '
                f'customer `{ship.get("customer_id") or "unknown"}` · carrier `{ship.get("carrier_id") or "unknown"}`')
    c1, c2, c3 = st.columns(3)
    c1.metric('Tracking ETA / actual arrival', geo.fmt_local(status.eta, status.delivery_tz))
    c2.metric('Delivery window ends', geo.fmt_local(status.window_end, status.delivery_tz))
    c3.metric('Projected late duration', _late(status.minutes_late))
    st.caption(f'Tracking MABD: {geo.fmt_local(status.mabd_end, status.delivery_tz)} · '
               f'MABD lateness: {_late(status.minutes_late_mabd)} · '
               f'as of {geo.fmt_local(status.as_of, status.delivery_tz)}. These are existing tracking outputs, not recalculated ETAs.')
    automatic_delay_costs.render(ship, sid, st.session_state.data_source)
    with st.expander('What-if Analysis', expanded=False):
        st.caption('Independent manual scenario. Its results do not replace, edit or add to the automatic workbook totals above. '
                   'Open this section only to explore alternative assumptions; zero selected charges means zero in this manual scenario only.')
        _render_what_if(tr, sid)


def _render_what_if(tr, sid):
    ship, status = tr.shipments[sid], tr.statuses[sid]
    # Each shipment AND data source gets independent widget state; no duplicate selector.
    scope = f'delaycost:{st.session_state.data_source}:{sid}:'
    def key(name):
        return scope + name
    def amount(label, name, default=None, step=25.0):
        return st.number_input(label, min_value=0.0, value=float(default) if default is not None else None,
                               step=step, key=key(name))
    def rate(label, name, default):
        return amount(label, name, default, 1.0)

    summary = st.container()
    st.markdown('**What-if inputs**')
    extra_delay = st.slider('Additional hypothetical delay (hours)', 0.0, 24.0, 0.0, 0.5, key=key('delay'))
    ctx = lateness_context(status, extra_delay)
    st.caption(f'Shifted tracking ETA (illustration): {geo.fmt_local(ctx["arrival"], status.delivery_tz)} · '
               f'tracking window lateness + extra delay: {_late(ctx["minutes_late"])} · '
               f'tracking MABD lateness + extra delay: {_late(ctx["minutes_late_mabd"])}. '
               'These shift existing tracking outputs only; the map, timeline and tracking state are unchanged. '
               'Tracking lateness uses the as-of clock when a truck has not arrived and its ETA is past; '
               'it may therefore exceed the displayed ETA minus the deadline.')
    selected = st.multiselect('Model consequences only when incurred or explicitly assumed', list(CATEGORIES),
                              format_func=CATEGORIES.get, key=key('categories'))
    charges = []
    for category in selected:
        with st.container(border=True):
            st.markdown('**' + CATEGORIES[category] + '**')
            if category == 'detention':
                st.caption('Trigger: excess loading/unloading dwell. Enter total dwell; free time is subtracted once. Do not use linehaul delay here.')
                a, b, c = st.columns(3)
                with a:
                    dwell = amount('Total facility dwell (hours)', 'dwell', 0, .5)
                with b:
                    free = rate('Free dwell time (hours)', 'free', RATES['free_hours'])
                with c:
                    unit = rate('Detention rate ($/hour)', 'det_rate', RATES['detention'])
                value = detention(dwell, unit, free)
                if 'layover' in selected and not st.checkbox('Detention hours exclude all time charged as layover', key=key('distinct_wait')):
                    value = None
                    st.warning('Confirm non-overlapping detention and layover time to avoid double-counting.')
                code, reason = 'DET', f'max(0, {dwell} h − {free} free h) × ${unit}/h; hypothetical facility dwell'
            elif category == 'layover':
                nights = st.number_input('Additional layover nights', min_value=0, value=0, step=1, key=key('nights'))
                unit = rate('Layover rate ($/night)', 'lay_rate', RATES['layover'])
                value, code, reason = product(nights, unit), 'LAY', f'{nights} additional overnight waits × ${unit}; not inferred from arrival delay'
            elif category == 'appointment':
                required = st.checkbox('Rescheduling is required or explicitly assumed', key=key('reschedule'))
                fee = amount('Rescheduling fee ($)', 'appointment_fee', RATES['appointment']) if required else None
                value, code, reason = product(fee) if required else product(0), 'APPT', 'One rescheduling fee only when explicitly selected'
            elif category == 'penalty':
                stops = ship.get('stops') or []
                facility = tr.facilities.get(stops[-1].get('facility_id'), {}) if stops else {}
                st.caption('Facility policy evidence (read-only): ' + str(facility.get('mabd_program_note') or 'No policy note available'))
                policy = st.checkbox('Applicable penalty policy confirmed or explicitly assumed', key=key('policy'))
                violation = ctx['minutes_late_mabd'] is not None and ctx['minutes_late_mabd'] > 0
                assumed = st.checkbox('Explicitly assume a qualifying OTIF violation beyond tracking evidence', key=key('violation'))
                violation = violation or assumed
                pct = amount('Penalty (% of order value)', 'penalty_pct', RATES['otif_pct'], .5)
                order = amount('Applicable PO / order value ($), not freight price', 'order_value')
                value = penalty(policy, violation if status.mabd_end or assumed else None, order, pct)
                code, reason = 'OTIF', f'Applicable policy AND qualifying violation; order value ${order} × {pct}%. No cargo-value guess.'
                if not policy:
                    st.warning('Policy is unconfirmed; the charge remains unknown.')
            elif category == 'labor':
                hours = amount('Additional broker labor (hours)', 'labor_hours', 0, .25)
                unit = rate('Loaded labor rate ($/hour)', 'labor_rate', RATES['labor'])
                value, code, reason = product(hours, unit), 'OPS', f'{hours} additional labor hours × ${unit}/h (off-ledger assumption)'
            elif category == 'storage':
                days = amount('Additional storage days', 'storage_days', 0, .5)
                unit = rate('Storage rate ($/day)', 'storage_rate', RATES['storage'])
                value, code, reason = product(days, unit), 'STOR', f'{days} explicitly assumed storage days × ${unit}/day'
            elif category == 'extra':
                value = amount('Additional carrier charge ($); blank means unknown', 'extra_amount')
                code = st.text_input('Carrier charge code for ledger reconciliation', 'RECOVERY', key=key('extra_code')).strip().upper()
                reason = 'User-entered carrier charge; confirm responsibility and whether already included in other categories'
            else:
                days = amount('Additional billing delay (days)', 'billing_days', 0, 1.0)
                balance = amount('Invoice balance blocked by paperwork ($)', 'invoice_balance')
                annual = amount('Annual cost of capital (%)', 'capital_pct', RATES['capital_pct'], .5)
                value, code = capital_cost(balance, days, annual), 'CAPITAL'
                reason = f'Invoice balance × {days} days × {annual}% ÷ 365; financing estimate, not a carrier fee'
            charge = Charge(category, CATEGORIES[category], code or 'UNKNOWN', value, reason)
            charges.append(_reconcile(ship, charge, key))
    if st.checkbox('Model a potential customer pass-through', key=key('recovery')):
        st.caption('Assumed recovery, not guaranteed revenue or cash received. Include each recovery once; do not assume automatic customer pass-through.')
        recovery = amount('Total potential recovery for this event ($)', 'recovery_amount')
        code = st.text_input('Recovery charge code on customer ledger, if applicable', 'RECOVERY', key=key('recovery_code')).strip().upper()
        charges.append(_reconcile(ship, Charge('recovery', 'Potential customer pass-through', code or 'UNKNOWN', recovery,
                                               'User-entered recovery; confirm responsible party, collectibility and scope', True), key))
    else:
        st.caption('Recovery assumption: none modeled. This does not assert that contractual recoveries are unavailable.')
    result = estimate(ship, charges)
    with summary:
        st.markdown('**Potential additional financial exposure**')
        cols = st.columns(3)
        cols[0].metric('Gross additional modeled cost', cash(result.gross))
        cols[1].metric('Potential additional recoveries', cash(result.recoveries))
        cols[2].metric('Net additional brokerage exposure', cash(result.net))
        if any(line.incremental is None for line in result.lines):
            st.warning('Total exposure is unknown: complete the selected inputs or reconcile matching TMS lines. Unknown amounts are not counted as zero.')
        if result.net is not None and result.net < 0:
            st.warning('Assumed recoveries exceed additional costs. Verify recovery scope and collectibility; this is not booked profit.')
    if result.lines:
        st.dataframe([{'Consequence': line.charge.label, 'Modeled event total': cash(line.charge.amount),
                       'Already on matching ledger code': cash(line.recorded), 'Additional amount': cash(line.incremental),
                       'Direction': 'Potential recovery' if line.charge.recovery else 'Cost',
                       'Treatment': line.note} for line in result.lines], hide_index=True, width='stretch')
    else:
        st.info('No consequences selected: additional modeled cost is $0.00, even if this shipment is late. This is not a claim of no real-world exposure.')
    st.caption(f'Baseline shipment GP: {cash(result.original_gp)} · GP after net additional exposure: {cash(result.adjusted_gp)}. '
               'Includes off-ledger economic costs such as labor/capital; not a posted TMS margin. Unknown baseline financials stay unknown.')
    with st.container(border=True):
        st.markdown('**What-if assumptions and arithmetic**')
        st.write('Gross = sum of additional costs after ledger reconciliation. Net = gross − potential additional recoveries. '
                 'Scenario GP = baseline GP − net. All cost arithmetic uses Decimal and rounds dollars to cents.')
        st.write('Illustrative rates come from fake_data / generate_complications.py, assumption dictionary A '
                 '(commit b76d73d), checked against Delay_Cost_Model.xlsx / Assumptions. The model attributes '
                 'DET/LAY/APPT/STOR defaults to TMS charge codes; these are not verified contracts for this shipment. '
                 'Labor $42/hour and annual cost of capital 9% are synthetic assumptions. OTIF 3% is an example policy, not universal.')
        st.write('Matching TMS lines are all non-cancelled vendor costs or customer charges, including estimated lines. '
                 'A matching code is not an event match. For the same event, enter the full event total; '
                 'only the excess above existing lines is additional. No automatic refunds are inferred. '
                 'For a separate event, confirm it is genuinely incremental. Do not enter a carrier charge already modeled as another category.')
        for line in result.lines:
            st.write(f'{line.charge.label}: {line.charge.reason}')
    if selected:
        st.markdown('**Possible mitigation actions**')
        for category in selected:
            st.write('• ' + MITIGATIONS[category])
    st.caption('All entries are conditional scenario assumptions unless explicitly identified as tracking/TMS values. '
               'No cost is activated by the delay slider alone. Validate policies, ledger scope, distinct consequences and responsibility before acting.')


def _reconcile(ship, charge, key):
    rows = ledger_matches(ship, charge.code, charge.recovery)
    if not rows:
        return charge
    st.warning(f'{charge.code}: {len(rows)} matching non-cancelled TMS line(s) already exist. Confirm the event scope to prevent double-counting.')
    overlap = st.selectbox(f'{charge.label}: how do these lines relate?',
                           ['Unconfirmed', 'Same event total', 'Separate incremental event'], key=key(charge.key + '_scope'))
    with st.container(border=True):
        st.caption('Matching ledger evidence · ' + charge.label)
        st.dataframe(rows, hide_index=True, width='stretch')
    return replace(charge, overlap=overlap)
