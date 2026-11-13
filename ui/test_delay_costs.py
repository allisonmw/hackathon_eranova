"""UI-owned cost and selected-shipment integration tests."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from streamlit.testing.v1 import AppTest
from ui.delay_cost_model import Charge, capital_cost, detention, estimate, lateness_context, penalty, product

APP = str(Path(__file__).resolve().parents[1] / 'app.py')
D = Decimal


def test_no_charges_is_zero_even_with_unknown_baseline():
    e = estimate({}, [])
    assert (e.gross, e.recoveries, e.net) == (D(0), D(0), D(0))
    assert e.adjusted_gp is None


def test_detention_free_time_once():
    assert detention(2, 75, 2) == D(0)
    assert detention(5, 75, 2) == D(225)
    assert detention(6, 75, 2) == D(300)


def test_late_arrival_alone_does_not_incur_fees():
    from types import SimpleNamespace
    from datetime import datetime, timezone
    s = SimpleNamespace(eta=datetime(2026, 11, 13, tzinfo=timezone.utc), minutes_late=180, minutes_late_mabd=None)
    shifted = lateness_context(s, 2)
    assert shifted['minutes_late'] == 300
    assert shifted['minutes_late_mabd'] is None
    assert s.minutes_late == 180
    assert estimate({}, []).gross == 0


def test_penalty_requires_policy_violation_and_order_value():
    assert penalty(False, True, 10000, 3) is None
    assert penalty(True, True, None, 3) is None
    assert penalty(True, None, 10000, 3) is None
    assert penalty(True, False, None, 3) == 0
    assert penalty(True, True, 10000, 3) == D(300)


def test_same_event_does_not_double_count_existing_costs():
    ship = {'gp_amount': 500, 'vendor_costs': [{'charge_code': 'DET', 'amount': 150, 'status': 'ESTIMATED'}]}
    charge = Charge('detention', 'Detention', 'DET', D(225), '5h total dwell', overlap='Same event total')
    e = estimate(ship, [charge])
    assert e.gross == 75 and e.adjusted_gp == 425
    assert estimate(ship, [replace(charge, amount=D(100))]).gross == 0
    assert estimate(ship, [replace(charge, overlap='Unconfirmed')]).gross is None
    assert estimate(ship, [replace(charge, overlap='Separate incremental event')]).gross == 225


def test_existing_sell_recovery_not_counted_twice():
    ship = {'sell_total': 2000, 'buy_total': 1500, 'customer_charges': [{'charge_code': 'DET', 'amount': 100}]}
    costs = [Charge('cost', 'Detention', 'DET', D(225), 'assumed'),
             Charge('recovery', 'Pass-through', 'DET', D(150), 'assumed', True, 'Same event total')]
    e = estimate(ship, costs)
    assert e.recoveries == 50 and e.net == 175 and e.adjusted_gp == 325


def test_unknown_line_or_ledger_amount_blocks_total():
    unknown = Charge('extra', 'Extra', 'DET', None, 'unknown')
    assert estimate({}, [unknown]).net is None
    ship = {'vendor_costs': [{'charge_code': 'DET', 'amount': None}]}
    assert estimate(ship, [replace(unknown, amount=D(200), overlap='Same event total')]).gross is None
    assert product(None, 75) is None
    assert detention(None, 75, 2) is None


def test_cancelled_lines_ignored():
    ship = {'vendor_costs': [{'charge_code': 'DET', 'amount': 100, 'status': 'CANCELLED'}]}
    assert estimate(ship, [Charge('det', 'Dwell', 'DET', D(225), 'assumed')]).gross == 225


def test_precise_rounding_and_capital_cost():
    assert product('0.1', '0.2') == D('.02')
    assert capital_cost(10000, 10, 9) == D('24.66')
    assert capital_cost(None, 10, 9) is None


def _app(monkeypatch):
    monkeypatch.setenv('MODEL', 'fake')
    monkeypatch.delenv('TMS_API_BASE', raising=False)
    monkeypatch.delenv('TMS_API_KEY', raising=False)
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    return at


def test_selected_shipment_and_assumptions_update_without_credentials(monkeypatch):
    at = _app(monkeypatch)
    sid = at.session_state['track_selected']
    prefix = f'delaycost:Fake data:{sid}:'
    assert any('Delay Cost Exposure' in h.value for h in at.subheader)
    at.multiselect(key=prefix + 'categories').set_value(['detention']).run()
    assert not at.exception
    at.number_input(key=prefix + 'dwell').set_value(5).run()
    assert not at.exception
    # Some loads already carry DET. Reconcile this as a genuinely new hypothetical event.
    if any(w.key == prefix + 'detention_scope' for w in at.selectbox):
        at.selectbox(key=prefix + 'detention_scope').set_value('Separate incremental event').run()
    assert next(m.value for m in at.metric if m.label == 'Gross additional modeled cost') == '$225.00'
    before_eta = next(m.value for m in at.metric if m.label == 'Tracking ETA / actual arrival')
    at.slider(key=prefix + 'delay').set_value(3).run()
    assert next(m.value for m in at.metric if m.label == 'Gross additional modeled cost') == '$225.00'
    assert next(m.value for m in at.metric if m.label == 'Tracking ETA / actual arrival') == before_eta
    # Widget exposes formatted options; get another underlying ID from the snapshot.
    import json
    from meridian.tracking import TRACKED_STAGES
    ships = json.loads((Path(APP).parent / 'snapshots/open_shipments.json').read_text())
    other_id = next(str(s['shipment_id']) for s in ships if str(s['shipment_id']) != sid and s['status'] in TRACKED_STAGES)
    at.selectbox(key='track_selected').set_value(other_id).run()
    assert not at.exception
    assert at.session_state['track_selected'] == other_id
    assert next(m.value for m in at.metric if m.label == 'Gross additional modeled cost') == '$0.00'
    assert any(other_id in m.value for m in at.markdown if '**Shipment' in m.value)


def test_original_tracking_components_still_render(monkeypatch):
    at = _app(monkeypatch)
    assert [tab.label for tab in at.tabs] == ['Load watch', 'Live tracking', 'Manual vs copilot']
    assert len(at.get('deck_gl_json_chart')) >= 1
    assert len(at.get('vega_lite_chart')) >= 1
    assert any('Log a check call' in e.label for e in at.expander)
    assert any('Load timeline' in h.value for h in at.subheader)
    assert at.session_state['data_source'] == 'Fake data'


def test_duplicate_modeled_codes_cannot_double_count_an_event():
    charges = [Charge('detention', 'Dwell', 'DET', D(225), 'assumed'),
               Charge('extra', 'Carrier extra', 'DET', D(100), 'possibly same fee')]
    e = estimate({}, charges)
    assert e.net is None
    assert all('Combine repeated' in line.note for line in e.lines)


def test_all_optional_controls_render_and_unknown_inputs_stay_unknown(monkeypatch):
    from ui.delay_costs import CATEGORIES
    at = _app(monkeypatch)
    prefix = f"delaycost:Fake data:{at.session_state['track_selected']}:"
    at.multiselect(key=prefix + 'categories').set_value(list(CATEGORIES)).run()
    assert not at.exception
    at.checkbox(key=prefix + 'reschedule').check().run()
    assert not at.exception
    assert at.number_input(key=prefix + 'appointment_fee').value == 50
    at.checkbox(key=prefix + 'recovery').check().run()
    assert not at.exception
    assert next(m.value for m in at.metric if m.label == 'Net additional brokerage exposure') == 'Unknown'
    assert any('unknown' in w.value.lower() for w in at.warning)
