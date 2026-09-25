# splitwise-mcp-server

An MCP server that reconciles a receipt and splits it between people to the exact cent.

## Tools

| Tool | Purpose |
|---|---|
| `reconcile_receipt` | Checks that item lines, discounts, tax, tip, and fees add up to the printed subtotal and total. Lists anything ambiguous. Assigns nothing to anyone. |
| `split_receipt` | Reconciles, then allocates every component to people. Refuses if the receipt doesn't balance or anything is ambiguous. |
| `allocate_amount` | Splits a single amount by weights using the same rounding. |

## Allocation rules

- **Money** is `Decimal` at the edges and exact `Fraction` internally. Rounding happens only at defined boundaries: each item, each discount, the tax, the tip, and each fee. Every component's shares add up exactly to that component's amount, so everyone's totals add up exactly to the printed total.
- **Items** are split by explicit weights. A multi-person item with no weights is rejected unless `split: "equal"` is set, which should only be done when the user said it's shared equally.
- **Discounts** are split by the subtotals of the items they cover (`applies_to`, or the whole receipt) by default. `equal` and `custom` allocations are also available.
- **Tax** is split by each person's taxable amount after discounts. Items with `taxable: false` (e.g. exempt groceries) are left out of the tax base.
- **Tip** is `proportional` by default: split by each person's tip-eligible pre-tax items (`basis: "net"` after discounts, or `"gross"` before them). `equal` and `custom` are also available.
- **Fees** have no default policy. A fee without `policy` is reported as a problem.
- **Leftover cents** use the largest-remainder method. Ties go by person name, ascending by Unicode code point, so the result doesn't depend on input order.
- **Reconciliation** is separate from allocation. An unexplained difference is reported and blocks the split. It is never assigned to a person. If the user confirms a real extra charge, add it as a fee with a policy.

## Example

```json
{
  "receipt": {
    "people": ["ann", "bob"],
    "items": [
      {"id": "pizza", "amount": "24.00", "split": "equal",
       "shares": [{"person": "ann"}, {"person": "bob"}]},
      {"id": "salad", "amount": "9.00", "shares": [{"person": "ann"}]},
      {"id": "milk",  "amount": "3.50", "taxable": false, "shares": [{"person": "bob"}]}
    ],
    "discounts": [{"id": "coupon", "amount": "4.00", "applies_to": ["pizza"]}],
    "tax":  {"amount": "2.32"},
    "tip":  {"amount": "6.00"},
    "fees": [{"id": "delivery", "amount": "3.99", "policy": "equal"}],
    "printed_subtotal": "36.50",
    "printed_total": "44.81"
  }
}
```

Send money as strings so no float ever touches an amount. `minor_unit_digits` (default 2) supports currencies like JPY (`0`).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install ".[dev]"
.venv/bin/pytest
```

Re-run the `pip install` after changing source to update the installed server (tests run from `src/` directly). An editable install (`-e`) can break on macOS with Python 3.13, because the `.pth` file it writes may get the `hidden` flag, and Python then skips it.

Claude Code:

```bash
claude mcp add splitwise -- /path/to/splitwise-mcp-server/.venv/bin/splitwise-mcp-server
```

## Remote (HTTP) mode

```bash
splitwise-mcp-server --http --port 8000   # MCP endpoint: http://localhost:8000/mcp
```

HTTP mode is used automatically whenever `$PORT` is set. It binds `0.0.0.0`, runs stateless, and serves `GET /health` for health checks.

**Railway:** `railway.json` sets the start command and health check. `requirements.txt` installs this package, and `.python-version` pins Python. After a deploy, generate a public domain under the service's **Settings → Networking**. The connector URL is `https://<your-domain>/mcp`. Use that URL for Claude custom connectors and ChatGPT developer-mode connectors (authentication: none).

The HTTP endpoint has no authentication. Anyone with the URL can call the tools, which only do arithmetic on the input and store nothing.

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "splitwise": {
      "command": "/path/to/splitwise-mcp-server/.venv/bin/splitwise-mcp-server"
    }
  }
}
```
