"""Freight Rewind Crisis Room. UI-owned prototype; never writes TMS records."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import escape

import streamlit as st

from meridian import client
from . import state
from .rewind_engine import Scenario, demo_shipments, evaluate, from_tms, original_financials, recommend

PREFIX = 'rewind_'
COMPLIANCE = ['Unconfirmed', 'Confirmed compliant', 'Noncompliant']


def reset():
    # Explicitly reset mounted widgets too: deleting keys alone can let the browser
    # restore its last widget value after a callback (notably cancellation → breakdown).
    defaults = {
        'disruption': 'Carrier breakdown', 'delay': 6.0, 'replacement_cost': 2400.0,
        'replacement_delay': 2.0, 'retained_charge': None, 'recovery_fee': None,
        'reschedule_fee': None, 'reschedule_delay': 8.0,
        'original_compliance': 'Unconfirmed', 'replacement_compliance': 'Unconfirmed',
        'timing_confirmed': False, 'reschedule_accepted': False,
        'replacement_name': 'Atlas Recovery Transport', 'strategy': 'wait',
    }
    original = next((s for s in st.session_state.get('rewind_records', [])
                     if s.identifier == st.session_state.get('rewind_shipment')), None)
    if original:
        for field in ('sell', 'total_cost', 'carrier_cost'):
            defaults[field] = float(getattr(original, field)) if getattr(original, field) is not None else None
        for field in ('arrival', 'window_start', 'window_end', 'deadline'):
            defaults[field] = getattr(original, field).isoformat() if getattr(original, field) else ''
    preserve = ('rewind_shipment', 'rewind_records', 'rewind_source', 'rewind_load_error')
    for key in list(st.session_state):
        if key.startswith(PREFIX) and key not in preserve:
            field = key[len(PREFIX):]
            if field in defaults:
                st.session_state[key] = defaults[field]
            else:
                del st.session_state[key]


def _cash(value):
    return 'Unknown' if value is None else f'${value:,.2f}'


def _when(value):
    return value.strftime('%b %d · %H:%M UTC') if value else 'Unknown'


def _card(eyebrow, title, detail):
    st.html(f'<div class="fr-card"><div class="fr-eyebrow">{escape(eyebrow)}</div>'
            f'<h3>{escape(title)}</h3><p>{escape(detail)}</p></div>')


def _shipments():
    source = st.session_state.data_source
    if st.session_state.get('rewind_source') != source:
        reset()
        st.session_state.pop('rewind_shipment', None)
        st.session_state.rewind_records = []
        st.session_state.rewind_source = source
        st.session_state.pop('rewind_load_error', None)
    if state.use_fake():
        st.caption('DEMO FIXTURES · Realistic sample shipments, not live TMS records. All scenario controls are hypothetical.')
        # Pin fixture timestamps for this session so reruns do not move the baseline.
        if not st.session_state.rewind_records:
            st.session_state.rewind_records = demo_shipments()
    else:
        st.caption(f'{source.upper()} · Explicit read-only fetch. No recovery actions are sent to the TMS.')
        if st.button('Load shipments (read only)', key='rewind_fetch'):
            try:
                with st.spinner('Reading open shipments…'):
                    result = client.list_shipments()
                if result['ok']:
                    records = [from_tms(row, source) for row in result['data']]
                    reset()
                    st.session_state.rewind_records = records
                    st.session_state.pop('rewind_shipment', None)
                    st.session_state.pop('rewind_load_error', None)
                else:
                    st.session_state.rewind_records = []
                    st.session_state.rewind_load_error = str(result.get('detail') or result.get('error'))
            except (RuntimeError, ValueError, KeyError, TypeError) as exc:
                st.session_state.rewind_records = []
                st.session_state.rewind_load_error = str(exc)
        if error := st.session_state.get('rewind_load_error'):
            st.error(f'Could not load shipments: {error}')
    return st.session_state.rewind_records


def _baseline(s):
    """Keep raw source immutable; all edits are explicitly scenario assumptions."""
    with st.expander('Source values & baseline assumptions', expanded=s.arrival is None):
        st.caption('Source values below are read-only. Overrides apply only to this simulation and are cleared by Rewind. '
                   'Missing API fields are never filled from demo data. All timestamps use UTC.')
        st.json({
            'source': s.source, 'shipment': s.identifier, 'customer_sell': s.sell,
            'original_total_cost': s.total_cost, 'original_carrier_cost': s.carrier_cost,
            'expected_arrival': str(s.arrival) if s.arrival else None,
            'appointment_start': str(s.window_start) if s.window_start else None,
            'appointment_end': str(s.window_end) if s.window_end else None,
            'must_arrive_by': str(s.deadline) if s.deadline else None,
        }, expanded=False)
        cols = st.columns(3)
        values = {}
        for col, field, label in zip(cols, ['sell', 'total_cost', 'carrier_cost'],
                                      ['Customer sell ($)', 'Original total cost ($)', 'Replaceable carrier portion ($)']):
            values[field] = col.number_input(label, min_value=0.0, value=float(getattr(s, field)) if getattr(s, field) is not None else None,
                                            step=50.0, key=PREFIX + field)
        st.caption('The carrier portion is included in original total cost. Replacement retains total cost minus this portion. '
                   'Confirm what has already been earned by the original carrier before entering its retained charge.')
        cols = st.columns(2)
        invalid = False
        for i, (field, label) in enumerate([('arrival', 'Expected arrival'), ('window_start', 'Appointment start'),
                                            ('window_end', 'Appointment end'), ('deadline', 'Must-arrive-by (optional)')]):
            original = getattr(s, field)
            raw = cols[i % 2].text_input(label + ' · ISO UTC', value=original.isoformat() if original else '',
                                        placeholder='2026-11-14T16:00:00+00:00', key=PREFIX + field)
            try:
                parsed = datetime.fromisoformat(raw.replace('Z', '+00:00')) if raw.strip() else None
                if parsed and parsed.tzinfo is None:
                    raise ValueError('Include a timezone offset, for example +00:00.')
                values[field] = parsed.astimezone(timezone.utc) if parsed else None
            except ValueError:
                st.error(f'{label}: use an ISO timestamp with timezone, or leave blank.')
                values[field] = None
                invalid = True
        if values['carrier_cost'] is not None and values['total_cost'] is not None and values['carrier_cost'] > values['total_cost']:
            st.error('Carrier portion cannot exceed original total cost.')
            invalid = True
        if values['window_start'] and values['window_end'] and values['window_start'] > values['window_end']:
            st.error('Appointment start must be at or before appointment end.')
            invalid = True
    return replace(s, **values), invalid


def _inputs():
    st.subheader('02 / Introduce a disruption')
    st.caption('Two complete prototype scenarios. Delay and unexpected-charge scenarios are reserved for the next backend slice.')
    disruption = st.radio('Disruption', ['Carrier breakdown', 'Carrier cancellation'], horizontal=True,
                          key='rewind_disruption')
    left, right = st.columns(2, gap='large')
    with left:
        st.markdown('**Original carrier**')
        delay = st.slider('Breakdown delay (hours)', 0.0, 12.0, 6.0, 0.5, key='rewind_delay') if disruption == 'Carrier breakdown' else 0.0
        if disruption == 'Carrier cancellation':
            st.info('The original carrier is unavailable. Wait and reschedule are excluded because neither restores capacity.')
        original_compliance = st.selectbox('Original carrier compliance', COMPLIANCE, key='rewind_original_compliance') if disruption == 'Carrier breakdown' else 'Unconfirmed'
        recovery_fee = st.number_input('Incremental repair / recovery charge ($)', min_value=0.0, value=None,
                                      step=50.0, key='rewind_recovery_fee') if disruption == 'Carrier breakdown' else None
        if disruption == 'Carrier breakdown':
            reschedule_delay = st.slider('Additional delay for later appointment (hours)', 0.0, 24.0, 8.0, 0.5, key='rewind_reschedule_delay')
            reschedule_fee = st.number_input('Rescheduling fee ($)', min_value=0.0, value=None, step=25.0, key='rewind_reschedule_fee')
            accepted = st.checkbox('Customer and facility accept the simulated later appointment', key='rewind_reschedule_accepted')
        else:
            reschedule_delay, reschedule_fee, accepted = 8.0, None, False
    with right:
        st.markdown('**Replacement intervention**')
        name = st.text_input('Proposed replacement carrier', 'Atlas Recovery Transport', key='rewind_replacement_name')
        cost = st.number_input('Replacement full carrier quote ($)', min_value=0.0, value=2400.0, step=50.0, key='rewind_replacement_cost')
        replacement_delay = st.slider('Replacement arrival delay vs original ETA (hours)', 0.0, 12.0, 2.0, 0.5, key='rewind_replacement_delay')
        retained = st.number_input('Retained original-carrier charge, including penalties ($)', min_value=0.0, value=None,
                                   step=50.0, key='rewind_retained_charge')
        replacement_compliance = st.selectbox('Replacement carrier compliance', COMPLIANCE, key='rewind_replacement_compliance')
    st.caption('Blank fees mean unknown. Enter 0 only after confirming no charge. Quotes, carrier names, compliance selections and transit times are operator assumptions; no carrier availability or insurance lookup is performed.')
    timing = st.checkbox('Arrival estimates, appointment constraints and any must-arrive-by deadline are verified for this exercise', key='rewind_timing_confirmed')
    return Scenario(disruption, delay, cost, replacement_delay, retained, recovery_fee, reschedule_fee,
                    reschedule_delay, original_compliance, replacement_compliance, timing, accepted, name)


def _timeline(s, c, options):
    st.subheader('03 / See the time consequence')
    st.caption('UTC · Estimates move with the sliders. Arrival before the window may require waiting. Rescheduling never waives a must-arrive-by deadline.')
    points = [t for t in [s.arrival, s.window_start, s.window_end, s.deadline] if t]
    points += [o.arrival for o in options if o.arrival]
    if not points or not s.arrival:
        st.info('Add a timezone-aware baseline arrival to see the timeline.')
        return
    start, end = min(points).timestamp() - 3600, max(points).timestamp() + 3600
    def x(t):
        return 205 + (t.timestamp() - start) / (end - start) * 700
    rows = [('Original ETA', s.arrival, '#94a3b8'), ('Disrupted ETA', None if c.disruption == 'Carrier cancellation' else s.arrival + timedelta(hours=c.delay), '#fb7185')]
    rows += [(o.title, o.arrival, '#2dd4bf' if o.timing == 'Feasible' else '#fbbf24') for o in options]
    h = 90 + 48 * len(rows)
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 980 {h}" role="img" aria-label="Arrival estimates, appointment window and delivery deadline"><rect width="980" height="{h}" rx="16" fill="#111e30"/>']
    if s.window_start and s.window_end and s.window_start <= s.window_end:
        svg.append(f'<rect x="{x(s.window_start)}" y="32" width="{max(2,x(s.window_end)-x(s.window_start))}" height="{h-66}" fill="#14b8a6" opacity=".13"/>')
    for i, (label, t, color) in enumerate(rows):
        y = 66 + i * 48
        svg.append(f'<text x="18" y="{y+4}" fill="#cbd5e1" font-size="13">{escape(label)}</text><line x1="205" x2="905" y1="{y}" y2="{y}" stroke="#334155"/>')
        if t:
            svg.append(f'<circle cx="{x(t)}" cy="{y}" r="6" fill="{color}"/><text x="{x(t)}" y="{y-12}" text-anchor="middle" fill="{color}" font-size="12">{t.strftime("%m/%d %H:%M")}</text>')
        else:
            svg.append(f'<text x="225" y="{y-8}" fill="#fb7185" font-size="12">No carrier / no arrival</text>')
    if s.deadline:
        svg.append(f'<line x1="{x(s.deadline)}" x2="{x(s.deadline)}" y1="32" y2="{h-26}" stroke="#fb7185" stroke-dasharray="5 4"/><text x="{x(s.deadline)}" y="20" text-anchor="middle" fill="#fb7185" font-size="12">Must-arrive-by</text>')
    svg.append('</svg>')
    st.image(''.join(svg), use_container_width=True)
    st.caption(f'Appointment: {_when(s.window_start)} → {_when(s.window_end)} · Must-arrive-by: {_when(s.deadline)}')


def render():
    st.html('<div class="fr-hero"><div class="fr-eyebrow">FREIGHT REWIND / DECISION LAB</div><h1>Your next move.<br>Ahead of the crisis.</h1><p>Simulate the disruption. Compare the trade-offs. Rewind with confidence.</p><span>PROTOTYPE · READ-ONLY · DETERMINISTIC SIMULATION</span></div>')
    st.subheader('01 / Choose a shipment')
    records = _shipments()
    if not records:
        st.info('Load open shipments, or choose Fake data in the sidebar to explore the demo. Snapshots require files supplied by the tools lead.')
        return
    by_id = {s.identifier: s for s in records}
    if st.session_state.get('rewind_shipment') not in by_id:
        st.session_state.rewind_shipment = records[0].identifier
        reset()
    identifier = st.selectbox('Shipment', list(by_id), format_func=lambda k: f'{k} · {by_id[k].origin} → {by_id[k].destination}',
                              key='rewind_shipment', on_change=reset)
    original = by_id[identifier]
    a, b, d = st.columns([2, 2, 1])
    with a:
        _card('ROUTE', f'{original.origin} → {original.destination}', original.identifier)
    with b:
        _card('ASSIGNED CARRIER', original.carrier, original.status)
    with d:
        _card('DELIVERY DEADLINE', _when(original.deadline), 'Must-arrive-by' if original.deadline else 'Not supplied')
    s, invalid = _baseline(original)
    gp, margin = original_financials(s)
    cols = st.columns(5)
    for col, label, value in zip(cols, ['Customer sell', 'Original total cost', 'Original carrier portion', 'Original gross profit', 'Original margin'],
                                 [_cash(s.sell), _cash(s.total_cost), _cash(s.carrier_cost), _cash(gp), f'{margin:.2f}%' if margin is not None else 'Unknown']):
        col.metric(label, value)
    st.caption('Baseline above includes any scenario-only overrides. The source shipment remains unchanged.')
    st.button('↶ Rewind Scenario', type='primary', on_click=reset,
              help='Clear every hypothetical input, confirmation and strategy selection; preserve this shipment.')
    c = _inputs()
    if invalid:
        st.warning('Correct the baseline inputs before comparing strategies.')
        return
    options = evaluate(s, c)
    _timeline(s, c, options)
    st.subheader('04 / Compare recovery strategies')
    for col, o in zip(st.columns(len(options), gap='medium'), options):
        with col, st.container(border=True):
            st.caption(o.key.upper() + ' / RECOVERY PLAN')
            st.markdown(f'### {o.title}')
            st.metric('Adjusted gross profit', _cash(o.gp), f'{o.gp_change:+,.2f} USD vs baseline' if o.gp_change is not None else None)
            st.metric('Adjusted total cost', _cash(o.total))
            st.write(f'**Margin:** {o.margin:.2f}%' if o.margin is not None else '**Margin:** Unknown')
            if o.total is not None and s.total_cost is not None:
                st.write(f'**Net recovery cost:** {_cash(o.total - Decimal(str(s.total_cost)))} vs original')
            else:
                st.write('**Net recovery cost:** Unknown')
            st.write(f'**Arrival:** {_when(o.arrival)}')
            st.write(f'**Delivery:** {o.timing}')
            st.write(f'**Compliance:** {o.compliance}')
            if o.supported:
                st.success('Supported under confirmed exercise assumptions')
            elif o.compliance == 'Noncompliant' or o.timing.startswith('Misses'):
                st.error('Not currently feasible')
            else:
                st.warning('Requires confirmation')
            with st.expander('Cost arithmetic & limitations'):
                for label, value in o.breakdown:
                    st.write(f'{label}: **{_cash(value)}**')
                st.write(f'Adjusted GP = {_cash(s.sell)} − {_cash(o.total)} = {_cash(o.gp)}')
                st.caption('Margin = adjusted GP ÷ customer sell × 100. GP change = adjusted GP − original GP. Negative GP is a loss.')
                if o.key == 'replace':
                    st.caption(f'Other costs = {_cash(s.total_cost)} − {_cash(s.carrier_cost)}. The original full carrier cost is removed once; only confirmed retained charges are added back.')
                for item in o.unknowns:
                    st.write('• ' + item)
                if o.key == 'reschedule':
                    st.caption('Assumes a new appointment at the simulated arrival; must-arrive-by remains binding.')
                st.caption('Prototype excludes unentered downstream costs, contractual claims and network effects; verify all-in charges before acting.')
    st.subheader('05 / Explain the decision')
    best = recommend(options)
    st.caption('Deterministic prototype explanation · No AI recovery model is connected. Compliance → delivery feasibility → complete evidence → gross profit → lower operational complexity.')
    if best:
        st.success(f'Best supported plan: {best.title}')
        st.write(f'{best.title} meets the entered compliance and timing constraints and preserves {_cash(best.gp)} '
                 f'in gross profit ({best.margin:.2f}% margin). It ranks highest among fully confirmed feasible plans; complexity breaks financial ties.')
        if any(not o.supported for o in options):
            st.caption('Other plans remain blocked or unconfirmed. This is the best supported plan among the confirmed candidates, not proof of a global optimum.')
    else:
        st.warning('Requires confirmation — there is not enough verified evidence to recommend a recovery plan.')
        st.write('Resolve missing fees, compliance evidence and timing confirmations below. A cheaper plan cannot override a hard safety or delivery constraint.')
    st.subheader('06 / Decision preview')
    chosen = st.radio('Inspect a strategy', [o.key for o in options], format_func=lambda k: next(o.title for o in options if o.key == k),
                      horizontal=True, key='rewind_strategy')
    o = next(o for o in options if o.key == chosen)
    left, right = st.columns(2, gap='large')
    with left, st.container(border=True):
        st.markdown('**Proposed next actions**')
        for i, action in enumerate(o.actions, 1):
            st.write(f'{i}. {action}')
        st.markdown('**TMS fields to review with the tools lead**')
        st.write(', '.join(o.fields))
        st.caption('These are conceptual fields, not a validated write payload.')
    with right, st.container(border=True):
        st.markdown('**Human confirmations required**')
        for item in o.unknowns or ('Revalidate entered quotes, compliance documents and customer approvals before execution.',):
            st.write('• ' + item)
        if o.timing.startswith('Misses'):
            st.error('This plan misses a binding time constraint. Obtain a feasible alternative; a rescheduled appointment does not waive the deadline.')
        st.write(f'Expected GP under stated assumptions: **{_cash(o.gp)}**')
        st.caption('No records changed. This preview does not book a carrier, approve a quote, change an appointment or send a message.')
