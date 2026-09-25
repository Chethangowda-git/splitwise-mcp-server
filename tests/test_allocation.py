from decimal import Decimal as D
from fractions import Fraction

import pytest

from splitwise_mcp_server.allocation import AllocationError, allocate


def test_equal_thirds_tie_broken_by_name():
    shares, extra = allocate(D("10.00"), {"cara": 1, "ann": 1, "bob": 1})
    assert shares == {"cara": D("3.33"), "ann": D("3.34"), "bob": D("3.33")}
    assert extra == ["ann"]


def test_tie_break_ignores_input_order():
    a, _ = allocate(D("0.02"), {"z": 1, "y": 1, "x": 1})
    b, _ = allocate(D("0.02"), {"x": 1, "y": 1, "z": 1})
    assert a == b == {"x": D("0.01"), "y": D("0.01"), "z": D("0.00")}


def test_largest_remainder_wins_over_name():
    # quotas: ann 0.333.. bob 0.666.. -> bob gets the leftover cent
    shares, extra = allocate(D("0.01"), {"ann": 1, "bob": 2})
    assert shares == {"ann": D("0.00"), "bob": D("0.01")}
    assert extra == ["bob"]


def test_negative_amount_mirrors_positive():
    pos, _ = allocate(D("10.00"), {"a": 1, "b": 1, "c": 1})
    neg, _ = allocate(D("-10.00"), {"a": 1, "b": 1, "c": 1})
    assert neg == {p: -v for p, v in pos.items()}


def test_exact_rational_weights():
    shares, _ = allocate(D("1.00"), {"a": Fraction(1, 3), "b": Fraction(2, 3)})
    assert sum(shares.values()) == D("1.00")
    assert shares == {"a": D("0.33"), "b": D("0.67")}


def test_zero_digit_currency():
    shares, _ = allocate(D("1000"), {"a": 1, "b": 1, "c": 1}, digits=0)
    assert shares == {"a": D("334"), "b": D("333"), "c": D("333")}


@pytest.mark.parametrize(
    "amount,weights",
    [(D("1.001"), {"a": 1}), (D("1.00"), {"a": 0}), (D("1.00"), {"a": -1, "b": 2})],
)
def test_rejects_bad_input(amount, weights):
    with pytest.raises(AllocationError):
        allocate(amount, weights)
