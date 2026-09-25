"""MCP server exposing receipt reconciliation, splitting, and Splitwise tools."""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Callable
from decimal import Decimal

import httpx2

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .allocation import ROUNDING_METHOD, AllocationError, allocate
from .models import AllocationResult, Receipt, Reconciliation, SplitResult
from .oauth import CALLBACK_PATH, CONSENT_PATH, OAuthConfig, SplitwiseAccessToken, SplitwiseOAuthProvider
from .split import SplitError, reconcile, split
from .splitwise_api import (
    CreatedExpense,
    SplitwiseClient,
    SplitwiseError,
    SplitwisePeople,
    expense_form,
    list_people,
)

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Split receipts between people to the exact cent, and optionally record them in Splitwise.

Workflow:
1. Parse the receipt into items, discounts, tax, tip, fees, and the printed total.
   Pass money as strings (e.g. "12.34").
2. Call reconcile_receipt. Fix any reported problems with the user before going on.
   Never absorb an unexplained difference into someone's share.
3. Ask the user whatever is ambiguous: who had which item, whether shared items are
   split equally or by weight, which items are tax-exempt, how each fee is shared,
   and the tip policy if it is not proportional.
4. Call split_receipt and show the result.
5. To record it in Splitwise: call list_splitwise_people, match each person on the
   receipt to a Splitwise user (confirm any uncertain match with the user), ask who
   paid, then call add_expense_to_splitwise. Confirm with the user before adding.
"""

TokenSource = Callable[[], str | None]


def oauth_token_source() -> str | None:
    token = get_access_token()
    return token.splitwise_token if isinstance(token, SplitwiseAccessToken) else None


def create_server(
    oauth: OAuthConfig | None = None,
    splitwise_token: TokenSource | None = None,
    http: httpx2.AsyncClient | None = None,
) -> MCPServer:
    """Build the server.

    oauth: enable per-user Splitwise sign-in (remote HTTP deployments).
    splitwise_token: where Splitwise tools get a token. Defaults to the signed-in
        user's token when oauth is set. Splitwise tools are registered only when
        one of the two is given.
    http: optional shared HTTP client for calls to Splitwise (used by tests).
    """
    provider = SplitwiseOAuthProvider(oauth, http) if oauth else None
    mcp = MCPServer(
        name="splitwise-mcp-server",
        version=__version__,
        instructions=INSTRUCTIONS,
        auth_server_provider=provider,
        auth=oauth.auth_settings() if oauth else None,
    )
    if splitwise_token is None and oauth is not None:
        splitwise_token = oauth_token_source

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    if provider is not None:

        @mcp.custom_route(CONSENT_PATH, methods=["GET", "POST"])
        async def consent(request: Request):
            return await provider.handle_consent(request)

        @mcp.custom_route(CALLBACK_PATH, methods=["GET"])
        async def splitwise_callback(request: Request):
            return await provider.handle_callback(request)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    def reconcile_receipt(receipt: Receipt) -> Reconciliation:
        """Check that the parsed receipt adds up, without assigning anything to people.

        Compares the item lines to the printed subtotal (if given) and
        subtotal - discounts + tax + tip + fees to the printed total. Also reports
        anything that would make a split ambiguous: unassigned items, shared items
        without weights, fees without a policy, amounts finer than one cent, and so on.
        """
        return reconcile(receipt)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
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

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    def allocate_amount(
        amount: Decimal, weights: dict[str, Decimal], minor_unit_digits: int = 2
    ) -> AllocationResult:
        """Split one amount across people by weight, to the exact cent.

        Uses the same largest-remainder rounding as split_receipt. Useful for one-off
        amounts such as a shared ride or a deposit.
        """
        try:
            shares, extra = allocate(amount, weights, minor_unit_digits)
        except AllocationError as e:
            raise ToolError(str(e)) from e
        return AllocationResult(
            amount=amount, shares=shares, extra_minor_units=extra, rounding_method=ROUNDING_METHOD
        )

    if splitwise_token is None:
        return mcp

    def client() -> SplitwiseClient:
        token = splitwise_token()
        if not token:
            raise ToolError("Not signed in to Splitwise. Reconnect the Splitwise connector and try again.")
        return SplitwiseClient(token, http)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def list_splitwise_people() -> SplitwisePeople:
        """List the signed-in Splitwise user, their friends, and their groups with members.

        Use the ids to map people on a receipt to Splitwise users, and a group id to
        add the expense to a group.
        """
        try:
            return await list_people(client())
        except SplitwiseError as e:
            raise ToolError(str(e)) from e

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
        )
    )
    async def add_expense_to_splitwise(
        receipt: Receipt,
        paid_by: str,
        members: dict[str, int | str],
        description: str,
        group_id: int = 0,
        date: str | None = None,
    ) -> CreatedExpense:
        """Split the receipt and record it as one expense in Splitwise.

        paid_by: the person on the receipt who paid the whole bill.
        members: receipt person name -> Splitwise user id (from list_splitwise_people)
            or email address. Required for the payer and everyone who owes something.
        description: expense title shown in Splitwise, e.g. "Dinner at Luigi's".
        group_id: Splitwise group id, or 0 for an expense outside any group.
        date: optional ISO 8601 date of the purchase.

        Uses exactly the same split as split_receipt; each person's owed share is
        their total, and the per-person breakdown goes in the expense notes. Refuses
        if the receipt does not reconcile. Calling this twice creates two expenses.
        """
        try:
            result = split(receipt)
        except SplitError as e:
            raise ToolError("Cannot split receipt:\n- " + "\n- ".join(e.problems)) from e
        try:
            form = expense_form(
                result, paid_by=paid_by, members=members, description=description, group_id=group_id, date=date
            )
        except ValueError as e:
            raise ToolError(str(e)) from e
        try:
            expense = await client().create_expense(form)
        except SplitwiseError as e:
            raise ToolError(str(e)) from e
        return CreatedExpense(
            expense_id=expense["id"],
            description=description,
            cost=result.grand_total,
            currency=result.currency,
            group_id=group_id,
            paid_by=paid_by,
            owed={p.person: p.total for p in result.people if p.total},
        )

    return mcp


def oauth_config_from_env() -> OAuthConfig | None:
    names = ("PUBLIC_URL", "SPLITWISE_CLIENT_ID", "SPLITWISE_CLIENT_SECRET", "SERVER_SECRET")
    values = {n: os.environ.get(n, "").strip() for n in names}
    if not any(values.values()):
        return None
    missing = [n for n, v in values.items() if not v]
    if missing:
        raise SystemExit(f"Splitwise sign-in is partly configured; missing: {', '.join(missing)}")
    if len(values["SERVER_SECRET"]) < 32:
        raise SystemExit("SERVER_SECRET must be at least 32 characters")
    return OAuthConfig(
        public_url=values["PUBLIC_URL"].rstrip("/"),
        splitwise_client_id=values["SPLITWISE_CLIENT_ID"],
        splitwise_client_secret=values["SPLITWISE_CLIENT_SECRET"],
        server_secret=values["SERVER_SECRET"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="splitwise-mcp-server")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve streamable HTTP at /mcp instead of stdio. Implied when $PORT is set (e.g. on Railway).",
    )
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    if args.http or "PORT" in os.environ:
        oauth = oauth_config_from_env()
        if oauth is None:
            log.warning("Splitwise sign-in is not configured: serving calculation tools only, without auth.")
        mcp = create_server(oauth)
        # Stateless: every request stands alone, so any replica can serve it.
        mcp.run("streamable-http", host=args.host, port=args.port, stateless_http=True)
    else:
        # Local stdio: a personal API key (never used by the public HTTP server).
        api_key = os.environ.get("SPLITWISE_API_KEY", "").strip() or None
        mcp = create_server(splitwise_token=(lambda: api_key) if api_key else None)
        mcp.run()


if __name__ == "__main__":
    main()
