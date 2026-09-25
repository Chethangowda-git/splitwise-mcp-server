"""MCP server exposing receipt reconciliation and splitting tools."""

from __future__ import annotations

from decimal import Decimal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__
from .allocation import ROUNDING_METHOD, AllocationError, allocate
from .models import AllocationResult, Receipt, Reconciliation, SplitResult
from .split import SplitError, reconcile, split

INSTRUCTIONS = """\
Split receipts between people to the exact cent.

Workflow:
1. Parse the receipt into items, discounts, tax, tip, fees, and the printed total.
   Pass money as strings (e.g. "12.34").
2. Call reconcile_receipt. Fix any reported problems with the user before going on.
   Never absorb an unexplained difference into someone's share.
3. Ask the user whatever is ambiguous: who had which item, whether shared items are
   split equally or by weight, which items are tax-exempt, how each fee is shared,
   and the tip policy if it is not proportional.
4. Call split_receipt.
"""

mcp = MCPServer(name="splitwise-mcp-server", version=__version__, instructions=INSTRUCTIONS)


@mcp.tool()
def reconcile_receipt(receipt: Receipt) -> Reconciliation:
    """Check that the parsed receipt adds up, without assigning anything to people.

    Compares the item lines to the printed subtotal (if given) and
    subtotal - discounts + tax + tip + fees to the printed total. Also reports
    anything that would make a split ambiguous: unassigned items, shared items
    without weights, fees without a policy, amounts finer than one cent, and so on.
    """
    return reconcile(receipt)


@mcp.tool()
def split_receipt(receipt: Receipt) -> SplitResult:
    """Split a reconciled receipt between people.

    Items are split by their weights (equal only when split='equal'). Discounts are
    split by covered item subtotals unless another allocation is given. Tax follows
    each person's taxable amount after discounts. Tip is proportional to tip-eligible
    pre-tax items by default, or equal/custom. Each fee needs an explicit policy.
    Each component is rounded with the largest-remainder method, so everyone's
    shares add up to exactly the printed total. Fails with a list of problems if the
    receipt does not reconcile or anything is ambiguous.
    """
    try:
        return split(receipt)
    except SplitError as e:
        raise ToolError("Cannot split receipt:\n- " + "\n- ".join(e.problems)) from e


@mcp.tool()
def allocate_amount(amount: Decimal, weights: dict[str, Decimal], minor_unit_digits: int = 2) -> AllocationResult:
    """Split one amount across people by weight, to the exact cent.

    Uses the same largest-remainder rounding as split_receipt. Useful for one-off
    amounts such as a shared ride or a deposit.
    """
    try:
        shares, extra = allocate(amount, weights, minor_unit_digits)
    except AllocationError as e:
        raise ToolError(str(e)) from e
    return AllocationResult(amount=amount, shares=shares, extra_minor_units=extra, rounding_method=ROUNDING_METHOD)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
