import pytest
from mcp.client import Client

from splitwise_mcp_server.server import create_server

mcp = create_server()

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_tools_listed():
    async with Client(mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == {"reconcile_receipt", "split_receipt", "allocate_amount"}


async def test_split_tool_reports_unreconciled_receipt():
    async with Client(mcp) as client:
        result = await client.call_tool("split_receipt", {"receipt": {
            "people": ["ann"],
            "items": [{"id": "x", "amount": "1.00", "shares": [{"person": "ann"}]}],
            "printed_total": "2.00",
        }})
    assert result.is_error
    assert "difference 1.00" in result.content[0].text


async def test_split_tool_success():
    async with Client(mcp) as client:
        result = await client.call_tool("split_receipt", {"receipt": {
            "people": ["ann", "bob"],
            "items": [{"id": "x", "amount": "1.01", "split": "equal",
                       "shares": [{"person": "ann"}, {"person": "bob"}]}],
            "printed_total": "1.01",
        }})
    assert not result.is_error
    totals = {p["person"]: p["total"] for p in result.structured_content["people"]}
    assert totals == {"ann": "0.51", "bob": "0.50"}


async def test_allocate_tool():
    async with Client(mcp) as client:
        result = await client.call_tool(
            "allocate_amount", {"amount": "10.00", "weights": {"a": "1", "b": "1", "c": "1"}}
        )
    assert not result.is_error
    assert result.structured_content["shares"] == {"a": "3.34", "b": "3.33", "c": "3.33"}
