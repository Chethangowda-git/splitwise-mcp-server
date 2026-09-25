"""Splitwise API client and conversion of a split into a Splitwise expense.

API reference: https://dev.splitwise.com/
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx2
from pydantic import BaseModel, Field

from .models import SplitResult

API_BASE = "https://secure.splitwise.com/api/v3.0"


class SplitwiseError(Exception):
    pass


class SplitwiseClient:
    def __init__(self, token: str, http: httpx2.AsyncClient | None = None):
        self._token = token
        self._http = http

    async def _request(self, method: str, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"}
        if self._http is not None:
            resp = await self._http.request(method, API_BASE + path, data=data, headers=headers)
        else:
            async with httpx2.AsyncClient(timeout=20) as http:
                resp = await http.request(method, API_BASE + path, data=data, headers=headers)
        if resp.status_code == 401:
            raise SplitwiseError("Splitwise rejected the sign-in. Reconnect the Splitwise connector and try again.")
        if resp.status_code == 429:
            raise SplitwiseError("Splitwise rate limit reached. Wait a moment and try again.")
        if resp.status_code >= 400:
            raise SplitwiseError(f"Splitwise returned HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def current_user(self) -> dict[str, Any]:
        return (await self._request("GET", "/get_current_user"))["user"]

    async def friends(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/get_friends"))["friends"]

    async def groups(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/get_groups"))["groups"]

    async def create_expense(self, form: dict[str, str]) -> dict[str, Any]:
        body = await self._request("POST", "/create_expense", data=form)
        # 200 OK is not success on its own: the call failed if `errors` is non-empty.
        errors = body.get("errors")
        if errors:
            raise SplitwiseError(f"Splitwise did not create the expense: {errors}")
        expenses = body.get("expenses") or []
        if not expenses:
            raise SplitwiseError("Splitwise returned no expense")
        return expenses[0]


# ---------------------------------------------------------------- people


class SplitwiseUser(BaseModel):
    id: int
    name: str
    email: str | None = None


class SplitwiseGroup(BaseModel):
    id: int
    name: str
    members: list[SplitwiseUser]


class SplitwisePeople(BaseModel):
    me: SplitwiseUser
    friends: list[SplitwiseUser]
    groups: list[SplitwiseGroup]


def _user(u: dict[str, Any]) -> SplitwiseUser:
    name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)
    return SplitwiseUser(id=u["id"], name=name or str(u["id"]), email=u.get("email"))


async def list_people(client: SplitwiseClient) -> SplitwisePeople:
    me = await client.current_user()
    friends = await client.friends()
    groups = await client.groups()
    return SplitwisePeople(
        me=_user(me),
        friends=[_user(f) for f in friends],
        groups=[
            SplitwiseGroup(id=g["id"], name=g.get("name") or "", members=[_user(m) for m in g.get("members", [])])
            for g in groups
            if g.get("id")  # id 0 is Splitwise's pseudo-group for non-group expenses
        ],
    )


# ---------------------------------------------------------------- expenses


class CreatedExpense(BaseModel):
    expense_id: int
    description: str
    cost: Decimal
    currency: str
    group_id: int
    paid_by: str
    owed: dict[str, Decimal] = Field(description="What each person owes, as recorded in Splitwise")


def expense_form(
    result: SplitResult,
    *,
    paid_by: str,
    members: dict[str, int | str],
    description: str,
    group_id: int,
    date: str | None,
) -> dict[str, str]:
    """Build the create_expense form. Raises ValueError for anything that can't be posted as-is."""
    people = [p.person for p in result.people]
    if paid_by not in people:
        raise ValueError(f"paid_by '{paid_by}' is not one of the people on the receipt: {people}")
    unknown = sorted(set(members) - set(people))
    if unknown:
        raise ValueError(f"members has names that are not on the receipt: {unknown}")

    owed = {p.person: p.total for p in result.people}
    involved = [p for p in people if owed[p] != 0 or p == paid_by]
    missing = [p for p in involved if p not in members]
    if missing:
        raise ValueError(
            f"No Splitwise user for {missing}. Map every person who owes something (and the payer) "
            "to a Splitwise user id or email; use list_splitwise_people to look up ids."
        )
    negative = [p for p in involved if owed[p] < 0]
    if negative:
        raise ValueError(f"Splitwise cannot record a negative share, but {negative} would be owed a credit")

    cost = result.grand_total
    form: dict[str, str] = {
        "cost": str(cost),
        "description": description,
        "currency_code": result.currency,
        "group_id": str(group_id),
        "details": breakdown(result),
    }
    if date:
        form["date"] = date
    for i, person in enumerate(involved):
        who = members[person]
        key = "email" if isinstance(who, str) and "@" in who else "user_id"
        form[f"users__{i}__{key}"] = str(who)
        form[f"users__{i}__paid_share"] = str(cost if person == paid_by else Decimal("0.00"))
        form[f"users__{i}__owed_share"] = str(owed[person])
    return form


def breakdown(result: SplitResult) -> str:
    lines = [f"Split by splitwise-mcp-server ({result.currency})"]
    for p in result.people:
        if p.total == 0:
            continue
        parts = [f"items {p.items}"]
        for label, value in (("discounts", p.discounts), ("tax", p.tax), ("tip", p.tip), ("fees", p.fees)):
            if value:
                parts.append(f"{label} {value}")
        lines.append(f"{p.person}: {p.total} ({', '.join(parts)})")
    return "\n".join(lines)
