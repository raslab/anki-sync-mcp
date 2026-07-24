# Anki MCP

A small, production-shaped MVP of the sidecar described in
[`docs/anki_mcp_system_design.md`](docs/anki_mcp_system_design.md). It exposes one local Anki
collection through authenticated Streamable HTTP MCP.

This iteration is intentionally read-only. It proves the package, container, authentication,
health checks, MCP transport, official Anki library integration, and single-owner collection
execution model before sync or mutation behavior is added.

## Included surface

Endpoint: `http://<host>:8000/mcp`

| Tool | Purpose |
| --- | --- |
| `anki_decks_list` | List deck IDs, names, and hierarchy with bounded pagination. |
| `anki_decks_get` | Read one deck by stable ID. |
| `anki_cards_search` | Search cards with Anki search syntax and bounded pagination. |
| `anki_cards_get` | Read rendered card content, deck identity, flags, and scheduling state. |

Public, content-free probes:

- `GET /health/live`
- `GET /health/ready`

Not included: sync, writes, media, notes, destructive operations, Basic auth, or multi-user
support. The server never opens Anki Desktop and never writes directly to SQLite.

## Requirements

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/), or Docker for container-only use
- A high-entropy MCP bearer token

## Develop and verify

```sh
uv sync --frozen --all-groups
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv build
```

The tests create disposable collections through the official `anki` Python API. They cover
configuration and secret-file behavior, bearer rejection without secret leakage, MCP
initialization and exact tool inventory, all four tool calls through JSON-RPC, pagination,
not-found behavior, and real deck/card reads.

## Run locally

```sh
mkdir -p .local-data
export MCP_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export ANKI_COLLECTION_PATH="$PWD/.local-data/collection.anki2"
uv run anki-mcp
```

Configure the MCP client for Streamable HTTP at `http://127.0.0.1:8000/mcp` with this header:

Supply an `Authorization` header using the Bearer scheme and the configured token.

Do not use an AnkiWeb password as this token. The MVP reads the current local collection
snapshot; it does not synchronize that snapshot with AnkiWeb.

## Run as the OpenClaw sidecar

Create the Compose secret and build the image:

```sh
mkdir -p secrets
python -c 'import secrets; print(secrets.token_urlsafe(32))' > secrets/anki_mcp_token
chmod 600 secrets/anki_mcp_token
docker compose build
docker compose up -d
docker compose exec anki-mcp anki-mcp-healthcheck
```

The checked-in Compose file keeps the service on the private `assistant` network and exposes no
host port. A container on that network connects to `http://anki-mcp:8000/mcp` and reads the bearer
token from the same secret source. Add a temporary `ports: ["127.0.0.1:8000:8000"]` mapping only
for host-side development.

The image runs as UID/GID `10001`, owns `/data`, uses one Uvicorn worker, and stores the collection
on the `anki_data` volume. Never mount the same collection read/write into two running sidecars,
Anki Desktop, or a sync server's storage directory.

## Configuration

Exactly one token source is required. Setting both is a startup error.

| Variable | Default | Description |
| --- | --- | --- |
| `MCP_AUTH_TOKEN` | — | Bearer token supplied directly. |
| `MCP_AUTH_TOKEN_FILE` | — | File containing the bearer token; preferred in containers. |
| `MCP_HOST` | `0.0.0.0` | Bind address. |
| `MCP_PORT` | `8000` | Bind port. |
| `MCP_PATH` | `/mcp` | Absolute, non-root MCP path. |
| `ANKI_COLLECTION_PATH` | `/data/collection.anki2` | Persistent collection file. |
| `MCP_MAX_PAGE_SIZE` | `100` | Server-side maximum, from 1 through 1000. |
| `MCP_MAX_SEARCH_SCAN` | `10000` | Refuse deck/card searches when the collection could require scanning more items. |
| `MCP_MAX_RENDERED_FIELD_BYTES` | `262144` | UTF-8 byte cap for each rendered card question/answer; responses include truncation flags. |
| `MCP_ALLOWED_HOSTS` | local and `anki-mcp` hosts | Comma-separated exact hosts or `host:*` patterns accepted by Streamable HTTP. |
| `MCP_ALLOWED_ORIGINS` | local HTTP origins | Comma-separated exact origins or `origin:*` patterns; browser requests with other origins are rejected. |
| `LOG_LEVEL` | `INFO` | Uvicorn log level. |

`MCP_AUTH_TOKEN_FILE` is read once during startup. Routine health and authentication errors do not
return the token or collection path. Every HTTP method under the MCP path is authenticated with a
constant-time token comparison.

Missing IDs and invalid pagination return MCP tool errors whose text ends with a compact JSON
object containing a stable `code` (`NOT_FOUND` or `INVALID_ARGUMENT`), a safe message, and a
request correlation ID. FastMCP prefixes this JSON with the tool name when serializing the MCP
error response.

## Project layout

```text
src/anki_mcp/
  app.py          ASGI composition and four FastMCP tools
  auth.py         bearer authentication middleware
  collection.py   official Anki adapter and dedicated single-thread executor
  config.py       strict environment and secret-file settings
  healthcheck.py  container readiness command
  __main__.py     one-worker Uvicorn entry point
tests/             config, collection, auth, health, and MCP integration tests
Dockerfile         multi-stage, non-root production image
compose.yaml       private-network sidecar deployment
pyproject.toml     package, test, lint, format, and type-check configuration
uv.lock            reproducible dependency lock
```

## Current safety boundary

The collection executor owns the `anki.collection.Collection` object on exactly one dedicated
worker thread. All tool calls are serialized through that thread. This is the required foundation
for a future `sync down -> mutate -> sync up` coordinator, but this MVP performs none of those
steps and must not be represented as remotely synchronized.
