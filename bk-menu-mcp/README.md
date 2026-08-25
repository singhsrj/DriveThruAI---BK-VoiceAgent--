# bk-menu-mcp

Minimal read-only [FastMCP](https://gofastmcp.com) server over a Burger King
menu Postgres database (e.g. Neon).

## Setup

```bash
uv sync
cp .env.example .env
# edit .env and set DATABASE_URL to your Postgres connection string
```

`DATABASE_URL` accepts a standard `postgres://` or `postgresql://` URL
(e.g. straight from Neon's dashboard, `sslmode=require` and all -- it's
normalized internally for the async `asyncpg` driver).

## Run

```bash
uv run server.py
```

By default this starts the server over **HTTP** at `http://127.0.0.1:8000/mcp`.
Override with env vars if needed:

```bash
MCP_HOST=0.0.0.0 MCP_PORT=9000 uv run server.py
```

Connect an MCP client to `http://<host>:<port>/mcp`. For remote/hosted use
(e.g. behind a reverse proxy), set `MCP_HOST=0.0.0.0` and put TLS/auth in
front of it -- FastMCP's HTTP transport itself doesn't add auth.

### Stdio instead

To run over stdio instead (e.g. for Claude Desktop, which spawns the
server as a subprocess rather than connecting to a URL), change the last
lines of `server.py`:

```python
if __name__ == "__main__":
    mcp.run()  # defaults to stdio
```

Then wire it into Claude Desktop's config:

```json
{
  "mcpServers": {
    "bk-menu": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/bk-menu-mcp", "run", "server.py"],
      "env": {
        "DATABASE_URL": "postgresql://user:password@host:5432/dbname?sslmode=require"
      }
    }
  }
}
```

## Testing

A minimal MCP client (`test_client.py`) is included to sanity-check the
server without needing a real MCP host like Claude Desktop.

```bash
# Full smoke test against a running HTTP server (start `uv run server.py` first):
uv run test_client.py --url http://127.0.0.1:8000/mcp

# Full smoke test, spawns `server.py` as a real subprocess over stdio
# (only works if server.py's __main__ block uses stdio, see above):
uv run test_client.py

# Same smoke test but connects in-process (no subprocess/network), fastest to iterate:
uv run test_client.py --in-process

# Call one tool directly:
uv run test_client.py --url http://127.0.0.1:8000/mcp --tool search_items --args '{"query": "whopper"}'
```

The smoke test pings the server, lists all registered tools, then calls
every tool once (chaining real ids/categories/combo keys returned from
earlier calls into later ones) and reports pass/fail per step. Exits
non-zero if anything fails. Requires `DATABASE_URL` to be set, since every
tool call hits the real database.

## Tools

All tools are read-only (`SELECT`-only queries).

| Tool | Description |
|---|---|
| `list_categories` | List all menu categories. |
| `list_items` | List items, filterable by category name, veg status, availability. |
| `search_items` | Case-insensitive partial name search. |
| `get_item` | Full item detail by id, including size/price variants. |
| `filter_items_by_price` | Items within a min/max price range. |
| `filter_items_by_calories` | Items within a min/max calorie range. |
| `get_high_protein_items` | Items at or above a protein (g) threshold, sorted descending. |
| `list_combos` | List combo meals, filterable by veg status/availability. |
| `get_combo` | Full combo detail by `(id, is_veg)`, including line items. |
| `get_menu_stats` | Aggregate stats: item/combo counts, price range, items per category. |

## Schema assumptions

Built against this shape (see `models.py` for the full SQLAlchemy models):

- `categories(id, name)`
- `items(id, name, category_id, price, calories, protein_g, piece_count, is_veg, is_available)`
- `item_sizes(id, item_id, size, price)`
- `combos(id, is_veg, num_people, name, price, is_available)` -- composite PK `(id, is_veg)`, since each combo "family" has a veg and non-veg row sharing the same `id`.
- `combo_items(combo_id, combo_is_veg, item_id, quantity)` -- composite PK, FK to `combos(id, is_veg)`.

## Project layout

- `models.py` -- SQLAlchemy ORM models matching the schema above.
- `db.py` -- async engine/session setup; normalizes `DATABASE_URL` to the `asyncpg` driver and strips libpq-only query params (`sslmode`, `channel_binding`) that asyncpg doesn't accept, moving SSL into `connect_args` instead.
- `server.py` -- the FastMCP server and all tool definitions.
