"""Read-only adapter for the teammate's committed workbook, not a complication generator.

Only its documented cost-line arithmetic is evaluated: ROUND(quantity*rate, 2),
amount*sign, and summation. No Excel macros, arbitrary formulas or probabilities run.
"""
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from posixpath import normpath
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from .delay_cost_model import ZERO, dollars, number, product

WORKBOOK = Path(__file__).with_name('data') / 'Delay_Cost_Model.xlsx'
SOURCE_COMMIT = 'b76d73dd85b0546be95d226e2444a0017fc73533'
SOURCE_SHA256 = 'ffe014e555faaf851f8891903a90cc269d069035531888cc1542789f77a9c997'
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def read_workbook(path=WORKBOOK):
    """Read values and cached results losslessly; no openpyxl/runtime Excel dependency."""
    result = {}
    with ZipFile(path) as archive:
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            strings = [''.join(node.itertext()) for node in ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('s:si', NS)]
        rels = {r.attrib['Id']: r.attrib['Target'] for r in ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))}
        sheets = ET.fromstring(archive.read('xl/workbook.xml')).findall('s:sheets/s:sheet', NS)
        for sheet in sheets:
            rid = sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
            target = rels[rid]
            target = target.lstrip('/') if target.startswith('/') else normpath('xl/' + target)
            rows = []
            headers = {}
            for row in ET.fromstring(archive.read(target)).findall('s:sheetData/s:row', NS):
                values, formulas = {}, {}
                for cell in row.findall('s:c', NS):
                    column = ''.join(c for c in cell.attrib['r'] if c.isalpha())
                    value = cell.find('s:v', NS)
                    value = value.text if value is not None else None
                    kind = cell.attrib.get('t')
                    if kind == 's':
                        value = strings[int(value)]
                    elif kind == 'inlineStr':
                        inline = cell.find('s:is', NS)
                        value = ''.join(inline.itertext()) if inline is not None else ''
                    elif kind == 'b':
                        value = value == '1'
                    values[column] = value
                    formula = cell.find('s:f', NS)
                    if formula is not None:
                        formulas[column] = formula.text or ''
                if not headers:
                    headers = values
                else:
                    record = {name: values.get(column) for column, name in headers.items() if name}
                    record['_row'] = int(row.attrib['r'])
                    record['_formulas'] = {headers.get(column, column): text for column, text in formulas.items()}
                    rows.append(record)
            result[sheet.attrib['name']] = rows
    return result


@lru_cache(maxsize=1)
def load_model():
    if sha256(WORKBOOK.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise ValueError('Bundled workbook differs from the documented source; refresh and verify its provenance.')
    return read_workbook()


@dataclass(frozen=True)
class ModelLine:
    source: dict
    amount: Decimal | None
    signed: Decimal | None


@dataclass(frozen=True)
class ModelExposure:
    events: tuple
    lines: tuple
    recommendations: tuple
    gross: Decimal | None
    recoveries: Decimal | None
    net: Decimal | None
    recorded_net: Decimal | None
    additional_net: Decimal | None
    baseline_gp: Decimal | None
    adjusted_gp: Decimal | None
    categories: tuple
    source_rollup: dict | None


def _sum(values):
    values = list(values)
    return None if any(value is None for value in values) else dollars(sum(values, ZERO))


def _unique(rows, key):
    unique = {}
    for row in rows:
        identity = row.get(key)
        if not identity:
            raise ValueError(f'Missing workbook {key}')
        if identity in unique and row != unique[identity]:
            raise ValueError(f'Conflicting workbook {key}: {identity}')
        unique[identity] = row
    return unique


def modeled_exposure(shipment_id, model=None):
    """Join shipment → events → unique lines. Root IDs describe cascades, not extra totals.

    Matches the workbook: net = gross - recoveries; additional net = net -
    SUM(in_tms signed lines); GP_after = workbook baseline GP - additional net.
    Workbook baseline attribution is not a reconciliation to a current Live API ledger.
    None means no scenario/insufficient data, never a fabricated zero exposure.
    """
    model = load_model() if model is None else model
    all_events = _unique(model['Events'], 'event_id')
    events = {key: row for key, row in all_events.items() if str(row['shipment_id']) == str(shipment_id)}
    if not events:
        return None
    for event_id, event in events.items():
        root = events.get(event.get('root_event_id'))
        parent = event.get('parent_event_id')
        if root is None or root.get('parent_event_id') or root.get('root_event_id') != root['event_id']:
            raise ValueError(f'Invalid workbook root relationship for {event_id}')
        if parent and (parent not in events or events[parent]['root_event_id'] != event['root_event_id']):
            raise ValueError(f'Invalid workbook parent relationship for {event_id}')
        chain, cursor = set(), event_id
        while cursor:
            if cursor in chain:
                raise ValueError('Cyclic workbook event relationship')
            chain.add(cursor)
            cursor = events[cursor].get('parent_event_id')
    selected = [row for row in model['Cost_Lines'] if str(row.get('shipment_id')) == str(shipment_id)
                or row.get('event_id') in events]
    lines = []
    for row in _unique(selected, 'line_id').values():
        event = events.get(row.get('event_id'))
        if event is None or str(row['shipment_id']) != str(shipment_id) or row.get('root_event_id') != event['root_event_id']:
            raise ValueError('Workbook cost line does not match its shipment/event/root IDs')
        amount = product(row.get('quantity'), row.get('rate'))
        cached = dollars(row.get('amount'))
        if cached is not None and amount is not None and cached != amount:
            raise ValueError(f'Workbook amount disagrees with quantity × rate: {row["line_id"]}')
        sign = number(row.get('sign'))
        if sign not in (Decimal(1), Decimal(-1)):
            raise ValueError('Workbook line sign must be +1 cost or -1 recovery')
        signed = dollars(amount * sign) if amount is not None else None
        cached_net = dollars(row.get('net_cost'))
        if cached_net is not None and signed is not None and cached_net != signed:
            raise ValueError('Workbook signed amount disagrees with cost line')
        lines.append(ModelLine(row, amount, signed))
    gross = _sum(line.amount for line in lines if number(line.source['sign']) == 1) if lines else None
    recovery = _sum(line.amount for line in lines if number(line.source['sign']) == -1) if lines else None
    net = dollars(gross - recovery) if gross is not None and recovery is not None else None
    flags_known = all(isinstance(line.source.get('in_tms'), bool) for line in lines)
    recorded = _sum(line.signed for line in lines if line.source.get('in_tms') is True) if flags_known and lines else None
    additional = dollars(net - recorded) if net is not None and recorded is not None else None
    rollups = [r for r in model['Shipment_Rollup'] if str(r.get('shipment_id')) == str(shipment_id)]
    if len(rollups) > 1:
        raise ValueError('Duplicate workbook shipment rollup')
    rollup = rollups[0] if rollups else None
    baseline = dollars(rollup.get('gp_amount_tms')) if rollup else None
    after = dollars(baseline - additional) if baseline is not None and additional is not None else None
    # Validate against the workbook's independently cached rollup, rounding Excel
    # floating-point serialization (e.g. 630.6400000000001) to currency cents.
    if rollup:
        for field, value in [('gross_complication_cost', gross), ('recoveries', recovery),
                             ('net_complication_cost', net), ('already_in_tms_net', recorded), ('gp_after_complications', after)]:
            cached = dollars(rollup.get(field))
            if cached is not None and value is not None and cached != value:
                raise ValueError(f'Workbook rollup validation failed: {shipment_id} / {field}')
    categories = []
    for category in sorted({line.source.get('cost_category') or 'Uncategorized' for line in lines}):
        group = [line for line in lines if (line.source.get('cost_category') or 'Uncategorized') == category]
        cost = _sum(line.amount for line in group if number(line.source['sign']) == 1)
        recoveries = _sum(line.amount for line in group if number(line.source['sign']) == -1)
        signed = _sum(line.signed for line in group)
        included = _sum(line.signed for line in group if line.source.get('in_tms') is True) if flags_known else None
        categories.append({'category': category, 'gross': cost, 'recoveries': recoveries, 'net': signed,
                           'included': included, 'additional': dollars(signed - included) if signed is not None and included is not None else None})
    recs = tuple(row for row in model.get('Recommendations', []) if row.get('event_id') in events
                 and str(row.get('shipment_id')) == str(shipment_id))
    return ModelExposure(tuple(events.values()), tuple(lines), recs, gross, recovery, net, recorded,
                         additional, baseline, after, tuple(categories), rollup)
