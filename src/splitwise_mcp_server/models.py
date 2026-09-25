"""Input and output models.

All money values are `Decimal`. JSON numbers and strings are both accepted, but
strings (e.g. "12.34") are recommended so no float ever touches an amount.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Money = Decimal
Weight = Decimal


class Share(BaseModel):
    """One person's claim on an item."""

    person: str
    weight: Weight | None = Field(
        default=None,
        description="Relative weight (e.g. 2 slices vs 1). Required when an item has "
        "several people and split is 'weighted'.",
    )


class Item(BaseModel):
    id: str
    description: str = ""
    amount: Money = Field(description="Line total before discounts, tax, and tip.")
    shares: list[Share] = Field(description="Who is responsible for this item.")
    split: Literal["weighted", "equal"] = Field(
        default="weighted",
        description="'equal' only when the user has said the item is shared equally; "
        "otherwise every share on a multi-person item needs an explicit weight.",
    )
    taxable: bool = Field(default=True, description="False for tax-exempt items (e.g. some groceries).")
    tip_eligible: bool = Field(default=True, description="False to exclude from a proportional tip base.")


class CustomWeights(BaseModel):
    weights: dict[str, Weight] = Field(description="person -> relative weight")


class Discount(BaseModel):
    id: str
    description: str = ""
    amount: Money = Field(description="Positive number; it is subtracted.")
    applies_to: list[str] | None = Field(
        default=None, description="Item ids the discount applies to. Omit for the whole receipt."
    )
    allocation: Literal["item_subtotal", "equal", "custom"] = "item_subtotal"
    participants: list[str] | None = Field(
        default=None, description="For 'equal': who shares it. Defaults to everyone on the covered items."
    )
    custom: CustomWeights | None = None


class Tax(BaseModel):
    amount: Money


class Tip(BaseModel):
    amount: Money
    policy: Literal["proportional", "equal", "custom"] = Field(
        default="proportional",
        description="'proportional' splits by each person's tip-eligible pre-tax items.",
    )
    basis: Literal["net", "gross"] = Field(
        default="net", description="For 'proportional': item amounts after ('net') or before ('gross') discounts."
    )
    participants: list[str] | None = Field(default=None, description="For 'equal'. Defaults to all people.")
    custom: CustomWeights | None = None


class Fee(BaseModel):
    id: str
    description: str = ""
    amount: Money
    policy: Literal["proportional", "equal", "custom"] | None = Field(
        default=None,
        description="Required. There is no default: ask the user how a fee is shared. "
        "'proportional' uses each person's net pre-tax items.",
    )
    participants: list[str] | None = Field(default=None, description="For 'equal'. Defaults to all people.")
    custom: CustomWeights | None = None


class Receipt(BaseModel):
    currency: str = "USD"
    minor_unit_digits: int = Field(default=2, ge=0, le=4, description="2 for cents, 0 for JPY, etc.")
    people: list[str] = Field(min_length=1)
    items: list[Item] = Field(min_length=1)
    discounts: list[Discount] = []
    tax: Tax | None = None
    tip: Tip | None = None
    fees: list[Fee] = []
    printed_subtotal: Money | None = Field(default=None, description="Subtotal as printed, if shown.")
    printed_total: Money = Field(description="Grand total as printed on the receipt.")

    @model_validator(mode="after")
    def _unique_ids(self) -> Receipt:
        for label, ids in (
            ("people", self.people),
            ("item ids", [i.id for i in self.items]),
            ("discount ids", [d.id for d in self.discounts]),
            ("fee ids", [f.id for f in self.fees]),
        ):
            dupes = sorted({x for x in ids if ids.count(x) > 1})
            if dupes:
                raise ValueError(f"duplicate {label}: {dupes}")
        return self


# ---------------------------------------------------------------- outputs


class ReconciliationLine(BaseModel):
    line: str
    parsed: Money
    printed: Money | None
    difference: Money | None


class Reconciliation(BaseModel):
    currency: str
    balanced: bool
    item_subtotal: Money
    discounts_total: Money
    tax: Money
    tip: Money
    fees_total: Money
    computed_total: Money
    printed_total: Money
    lines: list[ReconciliationLine]
    problems: list[str]


class ComponentAllocation(BaseModel):
    component: str = Field(description="e.g. 'item:burger', 'discount:coupon', 'tax', 'tip', 'fee:delivery'")
    amount: Money
    shares: dict[str, Money]
    extra_minor_units: list[str] = Field(
        description="People who received one leftover minor unit (cent) from largest-remainder rounding."
    )


class PersonTotal(BaseModel):
    person: str
    items: Money
    discounts: Money
    tax: Money
    tip: Money
    fees: Money
    total: Money


class SplitResult(BaseModel):
    currency: str
    reconciliation: Reconciliation
    people: list[PersonTotal]
    components: list[ComponentAllocation]
    grand_total: Money
    rounding_method: str


class AllocationResult(BaseModel):
    amount: Money
    shares: dict[str, Money]
    extra_minor_units: list[str]
    rounding_method: str
