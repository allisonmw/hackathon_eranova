"""UI-owned regression checks: financial invariants and interactive scenario state."""
from dataclasses import replace
from decimal import Decimal

from ui.rewind_engine import Scenario, demo_shipments, evaluate, from_tms, recommend


def confirmed(**changes):
    return replace(Scenario(), recovery_fee=100, retained_charge=150, reschedule_fee=75,
                   original_compliance='Confirmed compliant', replacement_compliance='Confirmed compliant',
                   timing_confirmed=True, reschedule_accepted=True, **changes)


def test_replacement_removes_original_carrier_exactly_once():
    s = demo_shipments()[0]
    o = next(o for o in evaluate(s, confirmed()) if o.key == 'replace')
    assert o.total == Decimal('2750.00')  # 3000 - 2800 + 2400 + 150
    assert o.gp == Decimal('1450.00')
    assert o.margin == Decimal('34.52')
    assert o.gp_change == Decimal('250.00')


def test_unknown_fee_is_not_zero_or_recommendable():
    options = evaluate(demo_shipments()[0], Scenario())
    assert all(o.total is None for o in options)
    assert recommend(options) is None


def test_explicit_zero_fee_is_valid():
    o = evaluate(demo_shipments()[0], replace(confirmed(), retained_charge=0))[1]
    assert o.total == Decimal('2600.00')


def test_cancellation_has_no_original_carrier_options():
    assert [o.key for o in evaluate(demo_shipments()[0], replace(confirmed(), disruption='Carrier cancellation'))] == ['replace']


def test_cheapest_noncompliant_plan_is_excluded():
    options = evaluate(demo_shipments()[0], replace(confirmed(), delay=1, replacement_compliance='Noncompliant', replacement_cost=0))
    assert recommend(options).key == 'wait'


def test_cheapest_late_plan_is_excluded():
    options = evaluate(demo_shipments()[0], replace(confirmed(), delay=1, replacement_delay=12, replacement_cost=0))
    assert recommend(options).key == 'wait'


def test_reschedule_never_waives_mabd():
    o = evaluate(demo_shipments()[0], confirmed())[2]
    assert o.timing == 'Misses deadline'
    assert not o.supported


def test_unaccepted_reschedule_requires_confirmation():
    s = replace(demo_shipments()[0], deadline=None)
    o = evaluate(s, replace(confirmed(), reschedule_accepted=False))[2]
    assert o.timing == 'Feasible'
    assert not o.supported


def test_zero_sell_and_missing_cost_do_not_crash():
    for sell in (0, None):
        options = evaluate(replace(demo_shipments()[0], sell=sell, total_cost=None), confirmed())
        assert all(o.margin is None for o in options)
        assert recommend(options) is None


def test_adapter_keeps_unknowns_unknown():
    s = from_tms({'shipment_id': 42, 'buy_total': 'NaN', 'estimated_arrival': '2026-11-14T12:00:00'}, 'Live API')
    assert s.total_cost is None and s.carrier_cost is None and s.arrival is None
    assert s.deadline is None


def test_timeline_moves_and_rewind_preserves_source():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'app.py'), default_timeout=20).run()
    assert not at.exception
    source = at.session_state['rewind_records'][0]
    at.slider(key='rewind_delay').set_value(9).run()
    assert not at.exception
    assert at.session_state['rewind_delay'] == 9
    at.number_input(key='rewind_retained_charge').set_value(150).run()
    at.radio(key='rewind_strategy').set_value('replace').run()
    next(b for b in at.button if 'Rewind Scenario' in b.label).click().run()
    assert not at.exception
    assert at.session_state['rewind_delay'] == 6
    assert at.session_state['rewind_retained_charge'] is None
    assert at.session_state['rewind_strategy'] == 'wait'
    assert at.session_state['rewind_records'][0] == source
    at.radio(key='rewind_disruption').set_value('Carrier cancellation').run()
    assert not at.exception
    assert at.radio(key='rewind_strategy').options == ['Replace the carrier']
    next(b for b in at.button if 'Rewind Scenario' in b.label).click().run()
    assert not at.exception
    assert at.session_state['rewind_disruption'] == 'Carrier breakdown'
    assert at.session_state['rewind_strategy'] == 'wait'


def test_existing_views_still_render():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'app.py'), default_timeout=20).run()
    for view in ['Load watch', 'Email-to-quote', 'Manual vs copilot', 'Crisis Room']:
        at.radio(key='workspace_view').set_value(view).run()
        assert not at.exception, view


def test_missing_timing_and_inconsistent_costs_block_recommendation():
    s = replace(demo_shipments()[0], arrival=None, window_end=None, carrier_cost=4000)
    options = evaluate(s, confirmed())
    assert recommend(options) is None
    assert any('reconcile' in issue for issue in options[1].unknowns)


def test_live_fetch_errors_are_visible_without_writes(monkeypatch):
    from streamlit.testing.v1 import AppTest
    from meridian import client
    from pathlib import Path
    monkeypatch.setattr(client, 'list_shipments', lambda: {'ok': False, 'detail': 'Read-only test failure'})
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'), default_timeout=20).run()
    at.radio(key='data_source').set_value('Live API').run()
    assert not at.exception
    at.button(key='rewind_fetch').click().run()
    assert not at.exception
    assert any('Read-only test failure' in error.value for error in at.error)
    at.radio(key='data_source').set_value('Fake data').run()
    assert not at.exception
    assert at.session_state['rewind_shipment'] == 'FR-7300172'
