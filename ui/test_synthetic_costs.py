"""Automatic simulation checks against the actual bundled workbook, including cached rollups."""
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import json

import pytest
from streamlit.testing.v1 import AppTest

from ui.delay_cost_model import dollars
from ui.synthetic_costs import WORKBOOK, SOURCE_SHA256, load_model, modeled_exposure

APP = str(Path(__file__).resolve().parents[1] / 'app.py')


def test_source_provenance_counts_and_all_rollups_match_to_the_cent():
    assert sha256(WORKBOOK.read_bytes()).hexdigest() == SOURCE_SHA256
    model = load_model()
    assert len(model['Events']) == 123
    assert len(model['Cost_Lines']) == 302
    assert len(model['Recommendations']) == 123
    ids = {event['shipment_id'] for event in model['Events']}
    for sid in ids:
        r = modeled_exposure(sid)
        assert r.gross == dollars(r.source_rollup['gross_complication_cost'])
        assert r.recoveries == dollars(r.source_rollup['recoveries'])
        assert r.net == dollars(r.source_rollup['net_complication_cost'])
        assert r.recorded_net == dollars(r.source_rollup['already_in_tms_net'])
        assert r.adjusted_gp == dollars(r.source_rollup['gp_after_complications'])


def test_weather_and_missed_appointment_cascade_counts_each_source_line_once():
    r = modeled_exposure('7300117')
    assert {e['event_id'] for e in r.events} == {'DLY-00108', 'DLY-00109'}
    assert len(r.lines) == 5  # child + root included, but not a second root subtotal
    assert r.gross == Decimal('630.64')  # 250 + .57*42 + 250 + 50 + 1.35*42
    assert r.recoveries == 0
    assert r.net == Decimal('630.64')
    assert r.baseline_gp == Decimal('469.00')
    assert r.adjusted_gp == Decimal('-161.64')
    assert {line.source['root_event_id'] for line in r.lines} == {'DLY-00108'}
    assert len(r.recommendations) == 2


def test_two_detention_incidents_with_customer_recovery():
    r = modeled_exposure('7300125')
    assert len(r.events) == 2 and len(r.lines) == 5
    assert r.gross == Decimal('342.00')  # 2*75 + .5*42 at each stop
    assert r.recoveries == Decimal('150.00')
    assert r.net == Decimal('192.00')
    assert r.baseline_gp == Decimal('296.00')
    assert r.adjusted_gp == Decimal('104.00')
    assert sum(1 for line in r.lines if line.source['charge_code'] == 'DET') == 3


def test_recorded_costs_and_recorded_recoveries_are_not_applied_twice():
    # Existing source ledger net = 225 cost - 235 recovery = -10.
    r = modeled_exposure('7300012')
    assert r.recorded_net == Decimal('-10.00')
    assert r.gross == Decimal('267.00') and r.recoveries == Decimal('235.00')
    assert r.additional_net == Decimal('42.00')  # 32 net - (-10 already included)
    assert r.adjusted_gp == Decimal('192.65')  # 234.65 - 42


def test_no_event_returns_unavailable_not_zero():
    assert modeled_exposure('7300175') is None
    assert modeled_exposure('UNKNOWN') is None


def test_exact_duplicate_line_is_not_counted_twice_and_conflicting_line_is_rejected():
    model = deepcopy(load_model())
    line = next(line for line in model['Cost_Lines'] if line['shipment_id'] == '7300125')
    model['Cost_Lines'].append(deepcopy(line))
    assert modeled_exposure('7300125', model).net == Decimal('192.00')
    model['Cost_Lines'][-1]['rate'] = '99'
    with pytest.raises(ValueError, match='Conflicting workbook line_id'):
        modeled_exposure('7300125', model)


def test_cross_shipment_and_invalid_event_relationships_are_rejected():
    model = deepcopy(load_model())
    line = next(line for line in model['Cost_Lines'] if line['shipment_id'] == '7300125')
    line['event_id'] = 'DLY-00108'
    with pytest.raises(ValueError, match='does not match'):
        modeled_exposure('7300125', model)
    model = deepcopy(load_model())
    event = next(e for e in model['Events'] if e['event_id'] == 'DLY-00109')
    event['parent_event_id'] = 'DLY-00109'
    with pytest.raises(ValueError, match='Cyclic'):
        modeled_exposure('7300117', model)


def test_unknown_line_financials_are_not_silently_zero():
    model = deepcopy(load_model())
    line = next(line for line in model['Cost_Lines'] if line['shipment_id'] == '7300125')
    line.update(quantity=None, amount=None, net_cost=None)
    r = modeled_exposure('7300125', model)
    assert r.gross is None and r.net is None and r.adjusted_gp is None
    assert r.recoveries == Decimal('150.00')


def test_unknown_ledger_attribution_blocks_adjusted_gp():
    model = deepcopy(load_model())
    line = next(line for line in model['Cost_Lines'] if line['shipment_id'] == '7300125')
    line['in_tms'] = None
    r = modeled_exposure('7300125', model)
    assert r.net == Decimal('192.00')
    assert r.recorded_net is None and r.additional_net is None and r.adjusted_gp is None


def _metric(at, label):
    return next(metric.value for metric in at.metric if metric.label == label)


def test_automatic_selection_two_examples_and_no_event_without_manual_inputs(monkeypatch):
    monkeypatch.setenv('MODEL', 'fake')
    monkeypatch.delenv('TMS_API_KEY', raising=False)
    monkeypatch.delenv('TMS_API_BASE', raising=False)
    at = AppTest.from_file(APP, default_timeout=30).run()
    for sid, gross, recoveries, net, gp in [
        ('7300117', '$630.64', '$0.00', '$630.64', '$-161.64'),
        ('7300125', '$342.00', '$150.00', '$192.00', '$104.00'),
    ]:
        at.selectbox(key='track_selected').set_value(sid).run()
        assert not at.exception
        assert _metric(at, 'Total modeled gross disruption cost') == gross
        assert _metric(at, 'Modeled recoveries') == recoveries
        assert _metric(at, 'Net modeled disruption cost') == net
        assert _metric(at, 'Modeled GP after disruptions') == gp
        assert next(e for e in at.expander if e.label == 'What-if Analysis').proto.expanded is False
    # A real tracked shipment with no synthetic events.
    ships = json.loads((Path(APP).parent / 'snapshots/open_shipments.json').read_text())
    from meridian.tracking import TRACKED_STAGES
    sid = next(s['shipment_id'] for s in ships if s['status'] in TRACKED_STAGES and modeled_exposure(s['shipment_id']) is None)
    at.selectbox(key='track_selected').set_value(sid).run()
    assert not at.exception
    assert any(i.value == 'No modeled complication available' for i in at.info)
    assert not any(m.label == 'Total modeled gross disruption cost' for m in at.metric)


def _live_view():
    from ui.automatic_delay_costs import render
    render({'gp_amount': 296}, '7300125', 'Live API')


def test_live_api_does_not_present_synthetic_results():
    at = AppTest.from_function(_live_view).run()
    assert not at.exception
    assert len(at.metric) == 0
    assert any('disabled in Live API mode' in i.value for i in at.info)


def test_current_gp_mismatch_keeps_model_baseline_and_warns():
    def view():
        from ui.automatic_delay_costs import render
        render({'gp_amount': 999}, '7300125', 'Fake data')
    at = AppTest.from_function(view).run()
    assert not at.exception
    assert _metric(at, 'Baseline shipment GP (model snapshot)') == '$296.00'
    assert _metric(at, 'Modeled GP after disruptions') == '$104.00'
    assert any('differs from' in w.value for w in at.warning)


def test_manual_what_if_cannot_change_automatic_model_results(monkeypatch):
    monkeypatch.setenv('MODEL', 'fake')
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.selectbox(key='track_selected').set_value('7300125').run()
    prefix = 'delaycost:Fake data:7300125:'
    at.multiselect(key=prefix + 'categories').set_value(['detention']).run()
    at.number_input(key=prefix + 'dwell').set_value(5).run()
    assert not at.exception
    assert _metric(at, 'Gross additional modeled cost') == '$225.00'
    assert _metric(at, 'Total modeled gross disruption cost') == '$342.00'
    assert _metric(at, 'Net modeled disruption cost') == '$192.00'
    assert _metric(at, 'Modeled GP after disruptions') == '$104.00'
