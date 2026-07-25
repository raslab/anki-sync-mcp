# Anki MCP

A small, production-shaped sidecar based on
[`docs/anki_mcp_system_design.md`](docs/anki_mcp_system_design.md). It exposes one local Anki
collection through authenticated Streamable HTTP MCP and synchronizes it through Anki's official
sync client API.

The service exposes safe synchronized note, deck, tag, and card workflows. All collection and sync
calls—including automatic synchronization around operations—run on one dedicated owner thread.

## Included surface

Endpoint: `http://<host>:8000/mcp`

| Tools | Scope | Purpose |
| --- | --- | --- |
| `anki_status` | read | Report package, collection, authentication, last-sync, pending mutation, and pending full-sync state. |
| `anki_sync_login` | admin | Authenticate and persist the sync host key. |
| `anki_sync` | write | Synchronize collection changes and optionally media. |
| `anki_backup_create` | admin | Request an explicit persistent local backup. |
| `anki_sync_full_download`, `anki_sync_full_upload` | admin + flag | Perform a confirmed, server-requested full sync in the operator-selected direction. |
| `anki_decks_list`, `anki_decks_get` | read | Read bounded deck metadata by stable IDs. |
| `anki_decks_create`, `anki_decks_update` | write | Create or rename decks through durable mutation receipts. |
| `anki_decks_delete` | destructive + flag | Delete a deck and its cards with `confirm=true`. |
| `anki_notes_search`, `anki_notes_get` | read | Search and read arbitrary note types with bounded fields and stable IDs. |
| `anki_notes_create`, `anki_notes_create_batch` | write | Duplicate-checked, field-validated, idempotent note creation. Batches are bounded and atomic. |
| `anki_notes_update_fields` | write | Patch validated named fields on an arbitrary note type. |
| `anki_notes_add_tags`, `anki_notes_remove_tags` | write | Apply normalized tag changes to bounded note-ID sets. |
| `anki_cards_search`, `anki_cards_get` | read | Search and read rendered card and scheduling state. |
| `anki_cards_create`, `anki_cards_update` | write | Compatibility workflows for built-in single-card Basic notes. |
| `anki_cards_change_deck`, `anki_cards_suspend`, `anki_cards_unsuspend` | write | Control arbitrary supported cards by stable IDs. |
| `anki_cards_delete` | destructive + flag | Delete a card and its orphaned note with `confirm=true`. |

Public, content-free probes:

- `GET /health/live`
- `GET /health/ready`

Tools are omitted from discovery when their configured scope or feature flag is disabled. The
service never chooses a full-sync direction itself, never opens Anki Desktop, and never writes
directly to Anki's SQLite schema. Basic HTTP auth and multi-user MCP credentials are not included.

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

The tests create disposable collections through the official `anki` Python API and launch the
official self-hosted sync server shipped by the pinned package. They cover scoped discovery,
configuration and secret files, authentication, note/deck/card workflows, strict bounds,
sync-before/write/sync-after, full-sync gating, persisted restart recovery, and idempotent replay.

## Run locally

```sh
mkdir -p .local-data
export MCP_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export ANKI_COLLECTION_PATH="$PWD/.local-data/collection.anki2"
export ANKI_SYNC_USERNAME="your-sync-username"
export ANKI_SYNC_PASSWORD="your-sync-password"
export ANKI_SYNC_HOST="" # empty selects AnkiWeb; otherwise use an HTTPS custom sync base URL
export MCP_SCOPES="read,write,admin"
export ANKI_SYNC_ON_WRITE="true"
uv run anki-mcp
```

Configure the MCP client for Streamable HTTP at `http://127.0.0.1:8000/mcp` with this header:

Supply an `Authorization` header using the Bearer scheme and the configured token.

Do not use an AnkiWeb password as the MCP token. MCP authentication and remote Anki sync
authentication are separate. Call `anki_sync_login` once; the host key is persisted under
`state/sync-auth` with owner-only permissions and is reused after restart, so the plaintext sync
password can be removed when manual reauthentication is acceptable. Writes synchronize before and
after the local commit by default. Reads use the local snapshot unless `sync_before=true` or
`ANKI_SYNC_ON_READ=true`.

When status or a tool reports `FULL_SYNC_REQUIRED`, normal synchronized operations stop and
readiness reports `full_sync_required`. Create a backup, inspect which direction the server
requires, and enable `ANKI_ALLOW_FULL_SYNC=true` only for the operator-controlled maintenance
window. Call the compatible confirmed administrative tool, then disable the flag again. Both
directions request a local backup; an upload reconciles pending local mutation receipts as remotely
synchronized. The service never chooses a destructive direction itself.

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
| `ANKI_SYNC_USERNAME` | empty | Remote sync username used by login and controlled bootstrap. |
| `ANKI_SYNC_PASSWORD` | — | Optional remote sync password supplied directly; intended for local development only. |
| `ANKI_SYNC_PASSWORD_FILE` | — | Optional password file; preferred in containers. It is unnecessary at restart when persisted host-key authentication is sufficient. |
| `ANKI_SYNC_HOST` | empty | Empty selects AnkiWeb; otherwise an HTTPS self-hosted sync base URL. HTTP is accepted only for loopback development. User-info, query strings, and fragments are rejected. |
| `MCP_SCOPES` | `read,write,admin` | Enabled internal scopes from `read`, `write`, `admin`, and `destructive`. Tools outside these scopes are omitted. |
| `ANKI_SYNC_ON_READ` | `false` | Sync before every read; each read can also request `sync_before=true`. |
| `ANKI_SYNC_ON_WRITE` | `true` | Sync before and after every mutation through the durable coordinator. |
| `ANKI_ALLOW_DESTRUCTIVE` | `false` | Register provisional confirmed deck/card deletion tools when the destructive scope is also enabled. |
| `ANKI_ALLOW_FULL_SYNC` | `false` | Register confirmed full upload/download maintenance tools when the admin scope is enabled. |
| `ANKI_BOOTSTRAP_MODE` | `disabled` | `download_if_empty` explicitly permits startup login and download-only full sync, but refuses a nonempty local collection or server-required upload. |
| `ANKI_MAX_BATCH_SIZE` | `50` | Maximum note-create, note-tag, and card-control batch size; range 1–500. |
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

At most one of `ANKI_SYNC_PASSWORD` and `ANKI_SYNC_PASSWORD_FILE` may be set. One is needed for a
new login or `download_if_empty` bootstrap unless persisted host-key authentication already exists.
Routine health, authentication, and tool errors do not return credentials or the collection path.
Every HTTP method under the MCP path is authenticated with a constant-time token comparison. Sync
host keys, pending full-sync state, and mutation receipts persist under `/data/state`; `sync-auth`
is written with mode `0600`.
Server-directed migrations are accepted only within the configured custom origin, or to an HTTPS
`ankiweb.net` host when using AnkiWeb.

Missing IDs and invalid pagination return MCP tool errors whose text ends with a compact JSON
object containing a stable `code` (`NOT_FOUND` or `INVALID_ARGUMENT`), a safe message, and a
request correlation ID. FastMCP prefixes this JSON with the tool name when serializing the MCP
error response.

## Project layout

```text
src/anki_mcp/
  app.py          ASGI composition, scoped discovery, and FastMCP tool contracts
  auth.py         bearer authentication middleware
  collection.py   official Anki workflows, operation coordinator, and owner-thread executor
  config.py       strict environment and secret-file settings
  state.py        durable sync authentication, status, and idempotency receipts
  healthcheck.py  container readiness command
  __main__.py     one-worker Uvicorn entry point
tests/             config, collection, auth, health, and MCP integration tests
Dockerfile         multi-stage, non-root production image
compose.yaml       private MCP network plus controlled sync-egress sidecar deployment
pyproject.toml     package, test, lint, format, and type-check configuration
uv.lock            reproducible dependency lock
```

## Current safety boundary

The collection executor owns `anki.collection.Collection` on exactly one dedicated worker thread.
Reads optionally synchronize first. Mutations use `sync → validate/idempotency check → local commit
→ durable receipt → sync`; a retry after a post-commit network failure performs only the sync step.
Receipts report `local_committed`, `remote_synced`, `media_synced`, and `retryable` and retain stable
result IDs across restart.

Full-sync requirements persist, degrade readiness, and fail closed. Full-sync tools require the
admin scope, an explicit safety flag, a preceding compatible server requirement, strict
confirmation, and a local backup. `download_if_empty` is the only automatic full-sync direction and
must be selected in process configuration; it refuses nonempty local data and server-required
uploads. Delete tools independently require the destructive scope, safety flag, and strict
`confirm=true`.

General note workflows validate exact create fields against the note type, validate patched field
names, reject empty/duplicate first fields, bound batches, and use stable note/card IDs. Legacy card
create/update remains limited to built-in single-card Basic notes, while deck changes and
suspend/unsuspend support arbitrary cards. Back up the persistent volume before maintenance and
never mount one collection into multiple live Anki processes.
