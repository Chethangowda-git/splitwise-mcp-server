from decimal import Decimal as D

import pytest

from splitwise_mcp_server.models import Receipt
from splitwise_mcp_server.split import SplitError, reconcile, split


def receipt(**overrides):
    base = {
        "people": ["ann", "bob", "cara"],
        "items": [
            {"id": "pizza", "amount": "30.00", "split": "equal",
             "shares": [{"person": "ann"}, {"person": "bob"}, {"person": "cara"}]},
            {"id": "wine", "amount": "20.00", "shares": [{"person": "ann"}]},
            {"id": "bread", "amount": "10.00", "taxable": False, "shares": [{"person": "bob"}]},
        ],
        "tax": {"amount": "4.00"},
        "tip": {"amount": "9.00"},
        "printed_subtotal": "60.00",
        "printed_total": "73.00",
    }
    base.update(overrides)
    return Receipt.model_validate(base)


def by_person(result):
    return {p.person: p for p in result.people}


def test_basic_split_sums_to_printed_total():
    r = split(receipt())
    people = by_person(r)
    assert r.grand_total == D("73.00")
    # taxable: ann 10+20=30, bob 10, cara 10 -> tax 4 split 3:1:1 (bread exempt)
    assert people["ann"].tax == D("2.40")
    assert people["bob"].tax == D("0.80")
    assert people["cara"].tax == D("0.80")
    # tip proportional to pre-tax items: ann 30, bob 20, cara 10
    assert people["ann"].tip == D("4.50")
    assert people["bob"].tip == D("3.00")
    assert people["cara"].tip == D("1.50")
    for comp in r.components:
        assert sum(comp.shares.values()) == comp.amount


def test_multi_person_item_requires_weights_or_equal():
    rec = reconcile(receipt(items=[
        {"id": "pizza", "amount": "60.00", "shares": [{"person": "ann"}, {"person": "bob"}]},
    ], printed_subtotal="60.00", tax=None, tip=None, printed_total="60.00"))
    assert not rec.balanced
    assert any("no weight" in p for p in rec.problems)


def test_weighted_item():
    r = split(receipt(items=[
        {"id": "pizza", "amount": "10.00",
         "shares": [{"person": "ann", "weight": "2"}, {"person": "bob", "weight": "1"}]},
    ], printed_subtotal=None, tax=None, tip=None, printed_total="10.00"))
    people = by_person(r)
    assert people["ann"].items == D("6.67")
    assert people["bob"].items == D("3.33")
    assert people["cara"].total == D("0.00")


def test_unexplained_difference_is_not_split():
    with pytest.raises(SplitError) as e:
        split(receipt(printed_total="73.50"))
    assert any("difference 0.50" in p for p in e.value.problems)


def test_subtotal_mismatch_detected():
    rec = reconcile(receipt(printed_subtotal="65.00"))
    assert not rec.balanced
    assert rec.lines[0].difference == D("5.00")


def test_fee_without_policy_is_rejected():
    rec = reconcile(receipt(fees=[{"id": "service", "amount": "5.00"}], printed_total="78.00"))
    assert any("no allocation policy" in p for p in rec.problems)


def test_fee_equal_policy():
    r = split(receipt(fees=[{"id": "service", "amount": "5.00", "policy": "equal"}], printed_total="78.00"))
    fees = {p.person: p.fees for p in r.people}
    assert fees == {"ann": D("1.67"), "bob": D("1.67"), "cara": D("1.66")}
    assert r.grand_total == D("78.00")


def test_discount_by_item_subtotal_reduces_tax_base():
    r = split(receipt(
        discounts=[{"id": "coupon", "amount": "6.00", "applies_to": ["pizza"]}],
        printed_total="67.00",
    ))
    people = by_person(r)
    assert {p: v.discounts for p, v in people.items()} == {
        "ann": D("-2.00"), "bob": D("-2.00"), "cara": D("-2.00")
    }
    # taxable net: ann 8+20=28, bob 8, cara 8 -> 4.00 * 28/44 etc.
    assert sum(p.tax for p in r.people) == D("4.00")
    assert {p.person: p.tax for p in r.people} == {"ann": D("2.54"), "bob": D("0.73"), "cara": D("0.73")}
    assert r.grand_total == D("67.00")


def test_printed_subtotal_after_discount_hint():
    rec = reconcile(receipt(
        discounts=[{"id": "coupon", "amount": "6.00"}],
        printed_subtotal="54.00",
        printed_total="67.00",
    ))
    assert any("after discounts" in p for p in rec.problems)


def test_tax_exempt_only_receipt_with_tax_fails():
    with pytest.raises(SplitError):
        split(Receipt.model_validate({
            "people": ["ann"],
            "items": [{"id": "milk", "amount": "3.00", "taxable": False, "shares": [{"person": "ann"}]}],
            "tax": {"amount": "0.20"},
            "printed_total": "3.20",
        }))


def test_custom_tip_and_unknown_person():
    rec = reconcile(receipt(tip={"amount": "9.00", "policy": "custom", "custom": {"weights": {"dan": 1}}}))
    assert any("unknown people ['dan']" in p for p in rec.problems)

    r = split(receipt(tip={"amount": "9.00", "policy": "custom", "custom": {"weights": {"ann": 1, "bob": 2}}}))
    assert {p.person: p.tip for p in r.people} == {"ann": D("3.00"), "bob": D("6.00"), "cara": D("0.00")}


def test_sub_cent_amount_rejected():
    rec = reconcile(receipt(tax={"amount": "4.005"}, printed_total="73.005"))
    assert any("decimal places" in p for p in rec.problems)


def test_many_people_many_rounding_boundaries_still_exact():
    people = [f"p{i}" for i in range(7)]
    items = [
        {"id": f"i{n}", "amount": f"{n}.{n:02d}", "split": "equal",
         "shares": [{"person": p} for p in people[: (n % 7) + 1]]}
        for n in range(1, 20)
    ]
    subtotal = sum(D(i["amount"]) for i in items)
    r = split(Receipt.model_validate({
        "people": people, "items": items,
        "discounts": [{"id": "d", "amount": "3.33"}],
        "tax": {"amount": "7.77"}, "tip": {"amount": "11.11"},
        "fees": [{"id": "f", "amount": "2.00", "policy": "proportional"}],
        "printed_total": str(subtotal - D("3.33") + D("7.77") + D("11.11") + D("2.00")),
    }))
    assert sum(p.total for p in r.people) == r.reconciliation.printed_total
