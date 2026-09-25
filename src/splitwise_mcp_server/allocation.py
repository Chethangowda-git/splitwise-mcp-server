"""Largest-remainder allocation of an amount into whole minor units (cents).

Quotas are computed with exact rational arithmetic (`Fraction`), so a 1/3 share is
exactly 1/3 rather than 0.3333... Only the final per-person amounts are rounded.

Method:
  1. Convert the amount to an integer number of minor units N.
  2. Each person's exact quota is N * weight / total_weight.
  3. Everyone gets floor(quota).
  4. The leftover L = N - sum(floors) units go one each to the L people with the
     largest fractional remainders.
  5. Tie-breaker: equal remainders are ordered by person name, ascending by Unicode
     code point. This depends only on the names, not on input order.

Negative amounts are allocated by magnitude and the sign is restored, so a
discount's shares mirror the rounding of an equivalent charge.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from math import floor

ROUNDING_METHOD = (
    "Largest remainder per component (item, discount, tax, tip, fee), exact rational quotas; "
    "ties broken by person name ascending (Unicode code point)."
)


class AllocationError(ValueError):
    pass


def minor_unit(digits: int) -> Decimal:
    return Decimal(1).scaleb(-digits)


def to_units(amount: Decimal, digits: int) -> int:
    scaled = amount.scaleb(digits)
    if scaled != scaled.to_integral_value():
        raise AllocationError(f"{amount} has more than {digits} decimal places")
    return int(scaled)


def from_units(units: int, digits: int) -> Decimal:
    return (Decimal(units) * minor_unit(digits)).quantize(minor_unit(digits))


def allocate(
    amount: Decimal,
    weights: dict[str, Fraction | Decimal | int],
    digits: int = 2,
) -> tuple[dict[str, Decimal], list[str]]:
    """Split `amount` across `weights` so the shares sum to exactly `amount`.

    Returns (shares, people_who_received_a_leftover_unit).
    """
    total_units = to_units(amount, digits)
    fw = {p: Fraction(w) for p, w in weights.items()}
    if any(w < 0 for w in fw.values()):
        negative = sorted(p for p, w in fw.items() if w < 0)
        raise AllocationError(f"negative allocation weight for {negative}")

    if total_units == 0:
        return {p: from_units(0, digits) for p in fw}, []

    weight_sum = sum(fw.values(), Fraction(0))
    if weight_sum == 0:
        raise AllocationError(f"cannot allocate {amount}: all weights are zero")

    sign = -1 if total_units < 0 else 1
    n = abs(total_units)
    quotas = {p: n * w / weight_sum for p, w in fw.items()}
    units = {p: floor(q) for p, q in quotas.items()}
    leftover = n - sum(units.values())

    candidates = sorted(
        (p for p in fw if fw[p] > 0),
        key=lambda p: (-(quotas[p] - units[p]), p),
    )
    extra = candidates[:leftover]
    for p in extra:
        units[p] += 1

    return {p: from_units(sign * u, digits) for p, u in units.items()}, sorted(extra)
