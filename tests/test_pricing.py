import pytest

from meridian import pricing


def test_sell_from_buy_is_margin_on_sell():
    assert pricing.sell_from_buy(820.0, 18) == 1000.0


def test_sell_from_buy_rejects_bad_margin():
    with pytest.raises(ValueError):
        pricing.sell_from_buy(100.0, 100)


def test_insurance_minimum_and_percentage():
    assert pricing.insurance_charge(None) == 0.0
    assert pricing.insurance_charge(1000) == 25.0          # 0.55% = 5.50 -> minimum 25
    assert pricing.insurance_charge(100_000) == 550.0


def test_choose_margin_never_below_floor():
    customer = {"margin_floor_pct": 20}
    assert pricing.choose_margin(customer) == 20
    assert pricing.choose_margin(customer, target=15) == 20
    assert pricing.choose_margin(customer, target=25) == 25
    assert pricing.choose_margin(None) == pricing.DEFAULT_MARGIN_FLOOR


def test_frt_margin_ignores_fuel():
    buy = [{"charge_code": "FRT", "amount": 820.0}, {"charge_code": "FSC", "amount": 300.0}]
    sell = [{"charge_code": "FRT", "amount": 1000.0}, {"charge_code": "FSC", "amount": 300.0}]
    assert pricing.frt_margin_pct(buy, sell) == 18.0
