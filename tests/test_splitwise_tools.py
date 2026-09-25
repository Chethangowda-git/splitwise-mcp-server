import json
from urllib.parse import parse_qs

import httpx2
import pytest
from mcp.client import Client

from splitwise_mcp_server.server import create_server

pytestmark = pytest.mark.anyio

RECEIPT = {
    "people": ["Ann", "Bob"],
    "items": [
        {"id": "pizza", "amount": "24.00", "split": "equal", "shares": [{"person": "Ann"}, {"person": "Bob"}]},
        {"id": "salad", "amount": "9.00", "shares": [{"person": "Ann"}]},
    ],
    "tax": {"amount": "2.32"},
    "tip": {"amount": "6.00"},
    "printed_total": "41.32",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def fake_api(calls, errors=None):
    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        assert request.headers["authorization"] == "Bearer tok"
        path = request.url.path.removeprefix("/api/v3.0")
        if path == "/get_current_user":
            return httpx2.Response(200, json={"user": {"id": 1, "first_name": "Ann", "last_name": "A", "email": "ann@x.test"}})
        if path == "/get_friends":
            return httpx2.Response(200, json={"friends": [{"id": 2, "first_name": "Bob", "email": "bob@x.test"}]})
        if path == "/get_groups":
            return httpx2.Response(200, json={"groups": [
                {"id": 0, "name": "Non-group expenses", "members": []},
                {"id": 77, "name": "Roommates", "members": [{"id": 1, "first_name": "Ann"}, {"id": 2, "first_name": "Bob"}]},
            ]})
        if path == "/create_expense":
            if errors:
                return httpx2.Response(200, json={"expenses": [], "errors": errors})
            return httpx2.Response(200, json={"expenses": [{"id": 999}], "errors": {}})
        return httpx2.Response(404)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def server(calls, **kw):
    return create_server(splitwise_token=lambda: "tok", http=fake_api(calls, **kw))


async def test_tools_hidden_without_splitwise():
    async with Client(create_server()) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert "add_expense_to_splitwise" not in names


async def test_list_people():
    calls = []
    async with Client(server(calls)) as c:
        r = await c.call_tool("list_splitwise_people", {})
    assert not r.is_error, r.content
    assert r.structured_content["me"]["name"] == "Ann A"
    assert [g["id"] for g in r.structured_content["groups"]] == [77]


async def test_add_expense_posts_exact_shares():
    calls = []
    async with Client(server(calls)) as c:
        r = await c.call_tool("add_expense_to_splitwise", {
            "receipt": RECEIPT, "paid_by": "Ann", "members": {"Ann": 1, "Bob": "bob@x.test"},
            "description": "Pizza night", "group_id": 77,
        })
    assert not r.is_error, r.content
    assert r.structured_content["expense_id"] == 999
    form = {k: v[0] for k, v in parse_qs(calls[-1].content.decode()).items()}
    assert form["cost"] == "41.32"
    assert form["group_id"] == "77"
    assert form["users__0__user_id"] == "1"
    assert form["users__0__paid_share"] == "41.32"
    assert form["users__0__owed_share"] == "26.30"
    assert form["users__1__email"] == "bob@x.test"
    assert form["users__1__paid_share"] == "0.00"
    assert form["users__1__owed_share"] == "15.02"
    assert "Ann: 26.30" in form["details"]


async def test_add_expense_refuses_unreconciled_or_unmapped():
    calls = []
    async with Client(server(calls)) as c:
        bad_total = await c.call_tool("add_expense_to_splitwise", {
            "receipt": {**RECEIPT, "printed_total": "42.00"}, "paid_by": "Ann",
            "members": {"Ann": 1, "Bob": 2}, "description": "x",
        })
        unmapped = await c.call_tool("add_expense_to_splitwise", {
            "receipt": RECEIPT, "paid_by": "Ann", "members": {"Ann": 1}, "description": "x",
        })
    assert bad_total.is_error and "difference 0.68" in bad_total.content[0].text
    assert unmapped.is_error and "No Splitwise user for ['Bob']" in unmapped.content[0].text
    assert not any(r.url.path.endswith("/create_expense") for r in calls)


async def test_splitwise_errors_surface():
    calls = []
    async with Client(server(calls, errors={"base": ["Invalid user"]})) as c:
        r = await c.call_tool("add_expense_to_splitwise", {
            "receipt": RECEIPT, "paid_by": "Ann", "members": {"Ann": 1, "Bob": 2}, "description": "x",
        })
    assert r.is_error and "Invalid user" in r.content[0].text
