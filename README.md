# Anki MCP

A small, production-shaped sidecar based on
[`docs/anki_mcp_system_design.md`](docs/anki_mcp_system_design.md). It exposes one local Anki
collection through authenticated Streamable HTTP MCP and synchronizes it through Anki's official
sync client API.

The service exposes explicit remote login/sync operations and CRUD tools for decks and Basic
cards. All collection and sync calls run on one dedicated owner thread.

## Included surface

Endpoint: `http://<host>:8000/mcp`

| Tool | Purpose |
| --- | --- |
| `anki_sync_login` | Authenticate with configured sync credentials and retain the host key in memory. |
| `anki_sync` | Synchronize collection changes and optionally media. |
| `anki_sync_full_download` | After `anki_sync` reports `FULL_SYNC`/`FULL_DOWNLOAD`, back up and replace local data with `confirm=true`. |
| `anki_sync_full_upload` | After `anki_sync` reports `FULL_SYNC`/`FULL_UPLOAD`, back up and replace remote data with `confirm=true`. |
| `anki_decks_list` | List deck IDs, names, and hierarchy with bounded pagination. |
| `anki_decks_get` | Read one deck by stable ID. |
| `anki_decks_create` | Create a deck by name. |
| `anki_decks_update` | Rename a deck by stable ID. |
| `anki_decks_delete` | Delete a deck and its contained cards with `confirm=true`. |
| `anki_cards_search` | Search cards with Anki search syntax and bounded offset pagination. |
| `anki_cards_get` | Read rendered card content, deck identity, flags, and scheduling state. |
| `anki_cards_create` | Create a Basic Front/Back note and its card in a deck. |
| `anki_cards_update` | Update Front/Back and/or move a Basic card to another deck. |
| `anki_cards_delete` | Delete a card and its orphaned note with `confirm=true`. |

Public, content-free probes:

- `GET /health/live`
- `GET /health/ready`

Not included: arbitrary note types/fields, automatic sync around each mutation, persisted sync host
keys, automatic full-sync direction selection, Basic HTTP auth, or multi-user support. The server never opens
Anki Desktop and never writes directly to SQLite.

## Requirements

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/), or Docker for container-only use
- A high-entropy MCP bearer token
- An AnkiWeb or self-hosted sync account

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
initialization and exact tool inventory, deck/card CRUD through JSON-RPC, mocked remote login/sync
without credential leakage, pagination, not-found behavior, and real collection mutations.

## Run locally

```sh
mkdir -p .local-data
export MCP_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export ANKI_COLLECTION_PATH="$PWD/.local-data/collection.anki2"
export ANKI_SYNC_USERNAME="your-sync-username"
export ANKI_SYNC_PASSWORD="your-sync-password"
export ANKI_SYNC_HOST="" # empty selects AnkiWeb; otherwise use an HTTPS custom sync base URL
uv run anki-mcp
```

Configure the MCP client for Streamable HTTP at `http://127.0.0.1:8000/mcp` with this header:

Supply an `Authorization` header using the Bearer scheme and the configured token.

Do not use an AnkiWeb password as the MCP token. MCP authentication and remote Anki sync
authentication are separate. Call `anki_sync_login` after each service restart, then call
`anki_sync` explicitly before reading remote changes or after local mutations. The returned sync
payload never contains the configured password, Anki host key, or endpoint. When it reports
`FULL_SYNC`, `FULL_DOWNLOAD`, or `FULL_UPLOAD`, call the matching confirmed full-sync tool. Both
directions create a local backup first; the service never chooses a destructive direction itself.

## Run as the OpenClaw sidecar

Create the Compose secret and build the image:

```sh
mkdir -p secrets
python -c 'import secrets; print(secrets.token_urlsafe(32))' > secrets/anki_mcp_token
read -rsp "Anki sync password: " ANKI_PASSWORD
printf '%s' "$ANKI_PASSWORD" > secrets/anki_sync_password
unset ANKI_PASSWORD
printf '\n'
chmod 600 secrets/anki_mcp_token
chmod 600 secrets/anki_sync_password
export ANKI_SYNC_USERNAME="your-sync-username"
export ANKI_SYNC_HOST="" # AnkiWeb
docker compose build
docker compose up -d
docker compose exec anki-mcp anki-mcp-healthcheck
```

The checked-in Compose file keeps MCP traffic on the private `assistant` network, adds a separate
egress-capable network for AnkiWeb/self-hosted sync, and exposes no host port. A container on the
private network connects to `http://anki-mcp:8000/mcp` and reads the bearer token from the same
secret source. Add a temporary `ports: ["127.0.0.1:8000:8000"]` mapping only for host-side development.

The image runs as UID/GID `10001`, owns `/data`, uses one Uvicorn worker, and stores the collection
on the `anki_data` volume. Never mount the same collection read/write into two running sidecars,
Anki Desktop, or a sync server's storage directory.

## Configuration

Exactly one token source is required. Setting both is a startup error.

| Variable | Default | Description |
| --- | --- | --- |
| `MCP_AUTH_TOKEN` | — | Bearer token supplied directly. |
| `MCP_AUTH_TOKEN_FILE` | — | File containing the bearer token; preferred in containers. |
| `ANKI_SYNC_USERNAME` | — | Required remote sync account username. |
| `ANKI_SYNC_PASSWORD` | — | Remote sync password supplied directly; intended for local development only. |
| `ANKI_SYNC_PASSWORD_FILE` | — | File containing the remote sync password; used by Compose and preferred in containers. |
| `ANKI_SYNC_HOST` | empty | Empty selects AnkiWeb; otherwise an HTTPS self-hosted sync base URL. HTTP is accepted only for loopback development. User-info, query strings, and fragments are rejected. |
| `MCP_HOST` | `0.0.0.0` | Bind address. |
| `MCP_PORT` | `8000` | Bind port. |
| `MCP_PATH` | `/mcp` | Absolute, non-root MCP path. |
| `ANKI_COLLECTION_PATH` | `/data/collection.anki2` | Persistent collection file. |
| `MCP_MAX_PAGE_SIZE` | `100` | Server-side maximum, from 1 through 1000. |
| `MCP_MAX_SEARCH_SCAN` | `10000` | Refuse deck/card searches when the collection could require scanning more items. |
| `MCP_MAX_RENDERED_FIELD_BYTES` | `262144` | UTF-8 byte cap for each rendered card question/answer; responses include truncation flags. |
| `MCP_MAX_CARD_FIELDS` | `100` | Maximum number of note fields returned with one card. |
| `MCP_MAX_RESPONSE_BYTES` | `1048576` | Aggregate UTF-8 JSON budget for one tool result; oversized responses fail with `RESPONSE_TOO_LARGE`. |
| `MCP_MAX_REQUEST_BYTES` | `1048576` | Aggregate byte budget for one MCP HTTP request body; oversized requests receive HTTP 413. |
| `MCP_ALLOWED_HOSTS` | local and `anki-mcp` hosts | Comma-separated exact hosts or `host:*` patterns accepted by Streamable HTTP. |
| `MCP_ALLOWED_ORIGINS` | local HTTP origins | Comma-separated exact origins or `origin:*` patterns; browser requests with other origins are rejected. |
| `LOG_LEVEL` | `INFO` | Uvicorn log level. |

Exactly one of `ANKI_SYNC_PASSWORD` and `ANKI_SYNC_PASSWORD_FILE` is also required. Secret files and
sync credentials are read at startup. Routine health, authentication, and
tool errors do not return credentials or the collection path. Every HTTP method under the MCP path
is authenticated with a constant-time token comparison. The sync host key is retained only in
process memory and is cleared before re-login or after a failed sync, so `anki_sync_login` must be
called after a restart or sync authentication/network failure before `anki_sync`.

Missing IDs and invalid pagination return MCP tool errors whose text ends with a compact JSON
object containing a stable `code` (`NOT_FOUND` or `INVALID_ARGUMENT`), a safe message, and a
request correlation ID. FastMCP prefixes this JSON with the tool name when serializing the MCP
error response.

## Project layout

```text
src/anki_mcp/
  app.py          ASGI composition and fourteen FastMCP tools
  auth.py         bearer authentication middleware
  collection.py   official Anki CRUD/sync adapter and dedicated single-thread executor
  config.py       strict environment and secret-file settings
  healthcheck.py  container readiness command
  __main__.py     one-worker Uvicorn entry point
tests/             config, collection, auth, health, and MCP integration tests
Dockerfile         multi-stage, non-root production image
compose.yaml       private MCP network plus controlled sync-egress sidecar deployment
pyproject.toml     package, test, lint, format, and type-check configuration
uv.lock            reproducible dependency lock
```

## Current safety boundary

The collection executor owns the `anki.collection.Collection` object on exactly one dedicated
worker thread. All tool calls, including remote login and sync, are serialized through that thread.
Sync is explicit rather than automatic: callers should use `sync -> mutate -> sync`. Full sync
requires a preceding server requirement, a direction-compatible tool, strict confirmation, and a
local backup under the collection's `backups/` directory. Delete tools
require a strict JSON boolean `confirm=true`; the Default deck cannot be deleted, and deleting any
other deck removes its contained cards. Card creation, field updates, and moves support only Anki's
built-in single-card Basic note type (`Front` and `Back`). Back up the persistent volume before
destructive operations, and do not mount one collection into multiple live Anki processes.

Mutation tools return concise receipts so a successful side effect cannot be hidden by a large
rendered response. Card reads represent note fields as bounded `{name, value, ...truncated}` items
instead of using unbounded field names as JSON object keys.
Deck creation is idempotent and reports `created=false` when the exact deck name already exists.
