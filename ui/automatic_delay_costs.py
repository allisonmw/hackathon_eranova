"""Primary, read-only view of the teammate's synthetic workbook results."""
import streamlit as st

from .delay_cost_model import dollars
from .synthetic_costs import SOURCE_COMMIT, modeled_exposure


def cash(value):
    return 'Unknown' if value is None else f'${value:,.2f}'


def render(ship, sid, source):
    if source not in ('Fake data', 'Snapshots (demo mode)'):
        st.info('Synthetic cost results are disabled in Live API mode. The workbook is a separate simulation snapshot, not operational evidence.')
        return
    st.caption('SIMULATED ESTIMATES · Automatically matched by shipment ID to Delay_Cost_Model.xlsx. '
               'These are not real invoices, verified penalties, guaranteed recoveries or current operational facts.')
    try:
        result = modeled_exposure(sid)
    except (OSError, ValueError, KeyError) as exc:
        st.error(f'Synthetic workbook could not be validated: {exc}')
        return
    if result is None:
        st.info('No modeled complication available')
        st.caption('This shipment has no matching event in the bundled simulation. Financial exposure is unavailable, not zero. '
                   'The optional What-if Analysis below is independent of the source model.')
        return
    st.markdown('**Simulated complications**')
    for event in result.events:
        parent = f' · follows {event["parent_event_id"]}' if event.get('parent_event_id') else ''
        st.write(f'**{event["complication_label"]}** · {event["event_id"]}{parent}')
        st.caption(event.get('detail') or 'No source event description')
    row = st.columns(3)
    row[0].metric('Total modeled gross disruption cost', cash(result.gross))
    row[1].metric('Modeled recoveries', cash(result.recoveries))
    row[2].metric('Net modeled disruption cost', cash(result.net))
    row = st.columns(3)
    row[0].metric('Baseline shipment GP (model snapshot)', cash(result.baseline_gp))
    row[1].metric('Net cost already in model TMS baseline', cash(result.recorded_net))
    row[2].metric('Modeled GP after disruptions', cash(result.adjusted_gp))
    current_gp = dollars(ship.get('gp_amount'))
    if current_gp is None:
        sell, buy = dollars(ship.get('sell_total')), dollars(ship.get('buy_total'))
        current_gp = sell - buy if sell is not None and buy is not None else None
    if current_gp is None or current_gp != result.baseline_gp:
        st.warning(f'The model baseline GP ({cash(result.baseline_gp)}) differs from, or cannot be checked against, '
                   f'the selected tracking snapshot ({cash(current_gp)}). The modeled GP uses only the workbook baseline, '
                   'not a reconciliation against a newer TMS ledger.')
    st.caption(f'Additional net cost beyond the model baseline: {cash(result.additional_net)} = '
               f'{cash(result.net)} net disruption cost − {cash(result.recorded_net)} already-in-TMS net. '
               f'Modeled GP = {cash(result.baseline_gp)} − {cash(result.additional_net)} = {cash(result.adjusted_gp)}. '
               'Off-ledger labor/capital are economic costs; this is not a posted accounting margin.')
    st.markdown('**Cost breakdown by source category**')
    st.dataframe([{'Category': row['category'], 'Gross cost': cash(row['gross']),
                   'Recoveries': cash(row['recoveries']), 'Net cost': cash(row['net']),
                   'Already in model baseline (net)': cash(row['included']),
                   'Additional net cost': cash(row['additional'])} for row in result.categories],
                 hide_index=True, width='stretch')
    st.markdown('**Model mitigation suggestions**')
    st.caption('Imported from workbook Recommendations. These are simulated planning suggestions, not approved actions or promised savings.')
    for rec in result.recommendations:
        st.write(f'**{rec["event_id"]} · {rec.get("action_owner") or "Broker"}:** {rec.get("primary_solution") or "Requires review"}')
    if not result.recommendations:
        st.caption('No event-level recommendation is available in the source workbook.')
    with st.expander('Source cost lines, event relationships and reconciliation'):
        st.caption(f'Source: fake_data @ {SOURCE_COMMIT} / Delay_Cost_Model.xlsx. '
                   'Each distinct line_id is counted once. Child events are included via their event_id; '
                   'root_event_id groups a cascade but does not create an additional subtotal. '
                   'Repeated charge codes can be separate incidents. No charge-code-only deduction is applied.')
        st.dataframe([{'Line ID': line.source['line_id'], 'Event': line.source['event_id'],
                       'Root event': line.source['root_event_id'], 'Category': line.source['cost_category'],
                       'Description': line.source.get('description'), 'Ledger': line.source['ledger'],
                       'Quantity': str(line.source['quantity']), 'Rate': str(line.source['rate']),
                       'Amount = ROUND(qty × rate, 2)': cash(line.amount), 'Sign': line.source['sign'],
                       'Signed cost': cash(line.signed), 'In model TMS baseline': line.source.get('in_tms'),
                       'Workbook status (simulation)': line.source.get('status'),
                       'Source row': f'Cost_Lines!{line.source["_row"]}'} for line in result.lines],
                     hide_index=True, width='stretch')
        st.write('Costs use sign +1; recoveries use sign −1, regardless of category name. '
                 'For example, RECOVERY_PREMIUM is a carrier-replacement expense, not recovered revenue. '
                 'The workbook in_tms flag excludes already-recorded signed amounts from the GP adjustment, '
                 'including previously recorded recoveries. It is source-model attribution, not proof of payment today.')
        st.write('Distinct same-root consequences are preserved exactly as modeled, including separate layover lines. '
                 'This adapter does not infer additional incidents, collapse same-code events, rerun the random generator, '
                 'or sum parent and child rollup totals. Contractual applicability and potential overlapping waiting periods still require review.')
        if result.source_rollup:
            st.caption(f'Shipment_Rollup row {result.source_rollup["_row"]}: independently cached gross, recoveries, net, '
                       'already-in-TMS net and GP-after figures are checked against the cost-line aggregation to the cent.')
        for rec in result.recommendations:
            st.write(f'**{rec["event_id"]} alternatives:** {rec.get("alternative_1") or "None supplied"}; '
                     f'{rec.get("alternative_2") or "None supplied"}')
