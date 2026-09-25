"""Receipt validation, reconciliation, and allocation.

Reconciliation (does the receipt add up?) is kept separate from allocation (who
pays what?). A receipt that does not reconcile is never split: any unexplained
difference is reported, not quietly assigned to someone.

Rounding boundaries: each item, each discount, the tax, the tip, and each fee is
allocated to people independently with the largest-remainder method, so every
component's shares sum to exactly that component's printed amount. Everything
before those boundaries (item weights, discount attribution, tax and tip bases)
is exact rational arithmetic.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from fractions import Fraction

from .allocation import ROUNDING_METHOD, AllocationError, allocate, minor_unit, to_units
from .models import (
    ComponentAllocation,
    CustomWeights,
    Discount,
    Item,
    PersonTotal,
    Receipt,
    Reconciliation,
    ReconciliationLine,
    SplitResult,
)


class SplitError(ValueError):
    def __init__(self, problems: list[str], reconciliation: Reconciliation | None = None):
        self.problems = problems
        self.reconciliation = reconciliation
        super().__init__("; ".join(problems))


# ---------------------------------------------------------------- validation


def validate(r: Receipt) -> list[str]:
    """Return every problem that makes the receipt ambiguous or unsplittable."""
    problems: list[str] = []
    people = set(r.people)
    item_ids = {i.id for i in r.items}
    d = r.minor_unit_digits

    def check_amount(label: str, amount: Decimal, *, positive: bool = False) -> None:
        try:
            to_units(amount, d)
        except AllocationError:
            problems.append(f"{label}: {amount} has more than {d} decimal places")
        if amount < 0 or (positive and amount == 0):
            problems.append(f"{label}: amount must be {'positive' if positive else 'non-negative'}, got {amount}")

    def check_people(label: str, names: list[str]) -> None:
        unknown = sorted(set(names) - people)
        if unknown:
            problems.append(f"{label}: unknown people {unknown} (not in receipt.people)")

    def check_custom(label: str, custom: CustomWeights | None) -> None:
        if custom is None or not custom.weights:
            problems.append(f"{label}: policy 'custom' requires custom.weights")
            return
        check_people(label, list(custom.weights))
        if any(w < 0 for w in custom.weights.values()):
            problems.append(f"{label}: custom weights must be non-negative")
        elif sum(custom.weights.values()) == 0:
            problems.append(f"{label}: custom weights sum to zero")

    for item in r.items:
        label = f"item '{item.id}'"
        check_amount(label, item.amount)
        if not item.shares:
            problems.append(f"{label}: not assigned to anyone")
            continue
        names = [s.person for s in item.shares]
        check_people(label, names)
        if len(set(names)) != len(names):
            problems.append(f"{label}: a person is listed more than once")
        if item.split == "weighted" and len(item.shares) > 1:
            missing = [s.person for s in item.shares if s.weight is None]
            if missing:
                problems.append(
                    f"{label}: shared by {len(names)} people but no weight for {missing}. "
                    "Give explicit weights, or set split='equal' if the user said it is shared equally."
                )
        if any(s.weight is not None and s.weight <= 0 for s in item.shares):
            problems.append(f"{label}: weights must be positive")

    for disc in r.discounts:
        label = f"discount '{disc.id}'"
        check_amount(label, disc.amount, positive=True)
        if disc.applies_to is not None:
            unknown = sorted(set(disc.applies_to) - item_ids)
            if unknown:
                problems.append(f"{label}: applies_to unknown items {unknown}")
            elif not disc.applies_to:
                problems.append(f"{label}: applies_to is empty")
        if disc.allocation == "custom":
            check_custom(label, disc.custom)
        if disc.allocation == "equal" and disc.participants is not None:
            check_people(label, disc.participants)

    if r.tax is not None:
        check_amount("tax", r.tax.amount)
    if r.tip is not None:
        check_amount("tip", r.tip.amount)
        if r.tip.policy == "custom":
            check_custom("tip", r.tip.custom)
        if r.tip.participants is not None:
            check_people("tip", r.tip.participants)

    for fee in r.fees:
        label = f"fee '{fee.id}'"
        check_amount(label, fee.amount)
        if fee.policy is None:
            problems.append(
                f"{label}: no allocation policy. Ask the user whether it is shared "
                "'proportional', 'equal', or 'custom'."
            )
        elif fee.policy == "custom":
            check_custom(label, fee.custom)
        if fee.participants is not None:
            check_people(label, fee.participants)

    if r.printed_subtotal is not None:
        check_amount("printed_subtotal", r.printed_subtotal)
    check_amount("printed_total", r.printed_total)
    return problems


# ---------------------------------------------------------------- reconciliation


def reconcile(r: Receipt) -> Reconciliation:
    """Check that parsed lines add up to the printed subtotal and total."""
    q = minor_unit(r.minor_unit_digits)
    problems = validate(r)

    subtotal = sum((i.amount for i in r.items), Decimal(0))
    discounts = sum((d.amount for d in r.discounts), Decimal(0))
    tax = r.tax.amount if r.tax else Decimal(0)
    tip = r.tip.amount if r.tip else Decimal(0)
    fees = sum((f.amount for f in r.fees), Decimal(0))
    computed = subtotal - discounts + tax + tip + fees

    def line(name: str, parsed: Decimal, printed: Decimal | None = None) -> ReconciliationLine:
        return ReconciliationLine(
            line=name,
            parsed=parsed.quantize(q),
            printed=None if printed is None else printed.quantize(q),
            difference=None if printed is None else (printed - parsed).quantize(q),
        )

    lines = [
        line("item_subtotal", subtotal, r.printed_subtotal),
        line("discounts", -discounts),
        line("tax", tax),
        line("tip", tip),
        line("fees", fees),
        line("total", computed, r.printed_total),
    ]

    if r.printed_subtotal is not None and r.printed_subtotal != subtotal:
        msg = f"item lines sum to {subtotal}, printed subtotal is {r.printed_subtotal} (difference {r.printed_subtotal - subtotal})"
        if discounts and r.printed_subtotal == subtotal - discounts:
            msg += "; the printed subtotal looks like it is after discounts, so pass the pre-discount subtotal or omit it"
        else:
            msg += "; an item may be missing, duplicated, or misread"
        problems.append(msg)
    if computed != r.printed_total:
        problems.append(
            f"subtotal - discounts + tax + tip + fees = {computed}, printed total is "
            f"{r.printed_total} (difference {r.printed_total - computed}). "
            "Find the missing or misread line; if the user confirms an extra charge, add it as a fee with a policy."
        )

    return Reconciliation(
        currency=r.currency,
        balanced=not problems,
        item_subtotal=subtotal.quantize(q),
        discounts_total=discounts.quantize(q),
        tax=tax.quantize(q),
        tip=tip.quantize(q),
        fees_total=fees.quantize(q),
        computed_total=computed.quantize(q),
        printed_total=r.printed_total.quantize(q),
        lines=lines,
        problems=problems,
    )


# ---------------------------------------------------------------- allocation


def _item_fractions(item: Item) -> dict[str, Fraction]:
    """Each person's exact fraction of an item (fractions sum to 1)."""
    if len(item.shares) == 1:
        return {item.shares[0].person: Fraction(1)}
    if item.split == "equal":
        w = {s.person: Fraction(1) for s in item.shares}
    else:
        w = {s.person: Fraction(s.weight) for s in item.shares}  # validated non-None
    total = sum(w.values())
    return {p: x / total for p, x in w.items()}


def _custom_fractions(custom: CustomWeights) -> dict[str, Fraction]:
    w = {p: Fraction(x) for p, x in custom.weights.items()}
    total = sum(w.values())
    return {p: x / total for p, x in w.items()}


def _equal_fractions(names: list[str]) -> dict[str, Fraction]:
    return {p: Fraction(1, len(names)) for p in names}


def _discount_matrix(
    disc: Discount, items: dict[str, Item], fractions: dict[str, dict[str, Fraction]]
) -> dict[str, dict[str, Fraction]]:
    """Exact discount attributed to each (item, person) pair.

    'item_subtotal': spread across covered items by amount, then within each item
    by that item's weights. 'equal'/'custom': split across people first, then each
    person's portion is spread across covered items by amount (used only to work
    out how much the discount lowers their taxable and tip bases).
    """
    covered = disc.applies_to if disc.applies_to is not None else list(items)
    covered_total = sum((Fraction(items[i].amount) for i in covered), Fraction(0))
    if covered_total == 0:
        raise SplitError([f"discount '{disc.id}': covered items total zero"])
    d = Fraction(disc.amount)
    matrix: dict[str, dict[str, Fraction]] = defaultdict(dict)

    if disc.allocation == "item_subtotal":
        for i in covered:
            item_part = d * Fraction(items[i].amount) / covered_total
            for p, f in fractions[i].items():
                matrix[i][p] = matrix[i].get(p, Fraction(0)) + item_part * f
        return matrix

    if disc.allocation == "custom":
        person_frac = _custom_fractions(disc.custom)  # validated
    else:
        names = disc.participants or sorted({p for i in covered for p in fractions[i]})
        person_frac = _equal_fractions(names)
    for i in covered:
        item_ratio = Fraction(items[i].amount) / covered_total
        for p, f in person_frac.items():
            matrix[i][p] = matrix[i].get(p, Fraction(0)) + d * f * item_ratio
    return matrix


def split(r: Receipt) -> SplitResult:
    rec = reconcile(r)
    if not rec.balanced:
        raise SplitError(rec.problems, rec)

    digits = r.minor_unit_digits
    items = {i.id: i for i in r.items}
    fractions = {i.id: _item_fractions(i) for i in r.items}
    components: list[ComponentAllocation] = []
    totals: dict[str, dict[str, Decimal]] = {
        p: defaultdict(Decimal) for p in r.people
    }

    def book(component: str, bucket: str, amount: Decimal, weights: dict[str, Fraction]) -> None:
        try:
            shares, extra = allocate(amount, weights, digits)
        except AllocationError as e:
            raise SplitError([f"{component}: {e}"], rec) from e
        for p, v in shares.items():
            totals[p][bucket] += v
        components.append(
            ComponentAllocation(component=component, amount=amount, shares=shares, extra_minor_units=extra)
        )

    # Exact per-person gross amounts per item.
    gross = {i.id: {p: Fraction(i.amount) * f for p, f in fractions[i.id].items()} for i in r.items}
    for i in r.items:
        book(f"item:{i.id}", "items", i.amount, gross[i.id])

    # Exact per-person net amounts per item, after discounts.
    net = {i: dict(v) for i, v in gross.items()}
    for disc in r.discounts:
        matrix = _discount_matrix(disc, items, fractions)
        per_person: dict[str, Fraction] = defaultdict(Fraction)
        for i, row in matrix.items():
            for p, x in row.items():
                net[i][p] = net[i].get(p, Fraction(0)) - x
                per_person[p] += x
        book(f"discount:{disc.id}", "discounts", -disc.amount, per_person)

    def base(select, amounts) -> dict[str, Fraction]:
        out: dict[str, Fraction] = defaultdict(Fraction)
        for i in r.items:
            if select(i):
                for p, x in amounts[i.id].items():
                    out[p] += x
        return dict(out)

    if r.tax is not None and r.tax.amount:
        taxable = base(lambda i: i.taxable, net)
        if sum(taxable.values(), Fraction(0)) <= 0:
            raise SplitError(["tax is charged but no item is marked taxable"], rec)
        book("tax", "tax", r.tax.amount, taxable)

    if r.tip is not None and r.tip.amount:
        t = r.tip
        if t.policy == "proportional":
            weights = base(lambda i: i.tip_eligible, net if t.basis == "net" else gross)
        elif t.policy == "equal":
            weights = _equal_fractions(t.participants or r.people)
        else:
            weights = _custom_fractions(t.custom)
        book("tip", "tip", t.amount, weights)

    for fee in r.fees:
        if not fee.amount:
            continue
        if fee.policy == "proportional":
            weights = base(lambda i: True, net)
        elif fee.policy == "equal":
            weights = _equal_fractions(fee.participants or r.people)
        else:
            weights = _custom_fractions(fee.custom)
        book(f"fee:{fee.id}", "fees", fee.amount, weights)

    q = minor_unit(digits)
    people = []
    for p in r.people:
        t = totals[p]
        parts = {k: t[k].quantize(q) for k in ("items", "discounts", "tax", "tip", "fees")}
        people.append(PersonTotal(person=p, total=sum(parts.values(), Decimal(0)).quantize(q), **parts))

    grand = sum((p.total for p in people), Decimal(0))
    if grand != r.printed_total:  # guaranteed by construction; guard anyway
        raise SplitError([f"internal error: shares sum to {grand}, expected {r.printed_total}"], rec)

    return SplitResult(
        currency=r.currency,
        reconciliation=rec,
        people=people,
        components=components,
        grand_total=grand.quantize(q),
        rounding_method=ROUNDING_METHOD,
    )
