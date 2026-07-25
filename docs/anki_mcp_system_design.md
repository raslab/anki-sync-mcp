# Anki MCP Sidecar — Lightweight System Design

Status: Draft

## 1. Purpose

Provide an always-on, headless MCP service that lets an AI assistant inspect and administer an Anki collection. The service runs as a container/sidecar, uses Anki's official Python core (`anki`) to manipulate a persistent local collection, and synchronizes that collection with AnkiWeb or an official self-hosted Anki sync server.

Typical flow:

1. A user asks an AI assistant to create or modify study material.
2. The assistant calls typed tools on the Anki MCP server.
3. The service synchronizes remote changes down, validates and applies the operation, then synchronizes changes up.
4. The user's Anki client receives the changes on its next normal sync.

## 2. Goals

- Run without Anki Desktop, Qt, Xvfb, or AnkiConnect.
- Expose a standards-compliant Streamable HTTP MCP endpoint at `/mcp`.
- Support broad collection administration: decks, notes, cards, tags, note types, templates, media, search, and sync.
- Work with AnkiWeb or an official self-hosted Anki sync server.
- Be deployable beside OpenClaw using Docker Compose.
- Keep the collection and sync state on persistent storage.
- Serialize collection access and protect destructive or full-sync operations.
- Return structured, bounded results suitable for AI tool use.

## 3. Non-goals

- Implement or reverse-engineer the Anki sync protocol independently.
- Modify a sync server's internal storage directly.
- Expose raw SQL or an unrestricted `anki_call(method, params)` tool.
- Run a GUI or provide an interactive Anki client.
- Push changes directly into mobile clients; devices still initiate their own sync.
- Provide multi-user or multi-collection tenancy in the first version.

## 4. Architecture

```text
┌──────────────────── GCP VM / Docker Compose ────────────────────┐
│                                                                 │
│  ┌──────────────┐       private Docker network                  │
│  │   OpenClaw   │ ───── Streamable HTTP MCP ─────┐              │
│  └──────────────┘                                 │              │
│                                                   ▼              │
│                                        ┌─────────────────────┐  │
│                                        │   anki-mcp sidecar  │  │
│                                        │                     │  │
│                                        │ auth middleware     │  │
│                                        │ MCP tool registry   │  │
│                                        │ operation lock      │  │
│                                        │ sync coordinator    │  │
│                                        │ official `anki` API │  │
│                                        └──────────┬──────────┘  │
│                                                   │              │
│                                             persistent volume    │
│                                                   │              │
│                                       collection + media + state │
└───────────────────────────────────────────────────┼──────────────┘
                                                    │ HTTPS
                                      ┌─────────────▼─────────────┐
                                      │ AnkiWeb or self-hosted    │
                                      │ Anki sync server          │
                                      └─────────────┬─────────────┘
                                                    │
                                            normal device sync
```

### Components

- **HTTP/MCP adapter:** Implements MCP initialization, tool discovery, tool calls, notifications, and session behavior over Streamable HTTP.
- **Authentication middleware:** Rejects unauthenticated requests before MCP processing.
- **Tool registry:** Publishes stable, typed domain tools rather than internal Anki methods.
- **Operation coordinator:** Allows only one collection/sync operation at a time and enforces timeouts and idempotency.
- **Anki adapter:** Uses public `anki.collection.Collection` and manager APIs for decks, notes, cards, models, tags, media, and sync.
- **Sync coordinator:** Handles authentication, pre-operation sync, post-write sync, media completion, and full-sync detection.
- **Persistent state:** Stores the client collection, media, sync state, idempotency records, and explicit backups.

### Technology stack

- **Runtime:** Python 3.12 with dependencies managed and locked by `uv`.
- **Anki:** Official `anki==26.5` package, accessed only through a dedicated adapter.
- **MCP:** `FastMCP` from the official Python `mcp` SDK for typed tools, schemas, lifecycle, and Streamable HTTP protocol handling.
- **HTTP:** Starlette and Uvicorn provide the minimal ASGI host needed to mount FastMCP, authentication middleware, and health routes.
- **Validation/configuration:** Pydantic v2 and `pydantic-settings`, including strict inputs and explicit environment-versus-`_FILE` secret handling.
- **State:** The standard-library `sqlite3` module stores idempotency records, while operation status uses a JSON state file; no ORM is required initially.
- **Testing:** `pytest`, AnyIO's pytest support, HTTPX, and `pytest-cov`, using disposable collections and a disposable instance of the official self-hosted Anki sync server.
- **Quality:** Ruff for formatting/linting and Pyright for static type checking.

The ASGI layer remains asynchronous, while a dedicated single worker thread owns the Anki collection and serializes all collection and sync operations. The deployment runs one Uvicorn worker and one container replica.

## 5. Deployment Model

The sidecar is a single-user, single-collection service. It should run as one replica because Anki collection access is not designed for concurrent writers.

Recommended image properties:

- Python 3.12 on a glibc-based Debian/Ubuntu image; do not use Alpine by default.
- Pin the official Anki package version, initially `anki==26.5`.
- Run as a non-root user.
- Use a read/write persistent volume mounted at `/data`.
- Expose the MCP port only to the private Compose network unless remote access is explicitly required.
- Use Docker secrets or a cloud secret manager for credentials.

### Example Compose shape

```yaml
services:
  openclaw:
    image: your-openclaw-image
    depends_on:
      anki-mcp:
        condition: service_healthy
    networks: [assistant]

  anki-mcp:
    image: your-registry/anki-mcp:0.1.0
    restart: unless-stopped
    environment:
      MCP_HOST: "0.0.0.0"
      MCP_PORT: "8000"
      MCP_AUTH_MODE: "bearer"
      MCP_AUTH_TOKEN_FILE: "/run/secrets/anki_mcp_token"
      ANKI_COLLECTION_PATH: "/data/collection.anki2"
      ANKI_SYNC_ENDPOINT: "" # empty means AnkiWeb
      ANKI_SYNC_USERNAME_FILE: "/run/secrets/anki_sync_username"
      ANKI_SYNC_PASSWORD_FILE: "/run/secrets/anki_sync_password"
      ANKI_SYNC_ON_READ: "false"
      ANKI_SYNC_ON_WRITE: "true"
      ANKI_FULL_SYNC_POLICY: "fail"
      ANKI_ALLOW_DESTRUCTIVE: "false"
    secrets:
      - anki_mcp_token
      - anki_sync_username
      - anki_sync_password
    volumes:
      - anki_data:/data
    networks: [assistant]
    healthcheck:
      test: ["CMD", "python", "-m", "anki_mcp.healthcheck"]
      interval: 30s
      timeout: 5s
      retries: 3

secrets:
  anki_mcp_token:
    file: ./secrets/anki_mcp_token
  anki_sync_username:
    file: ./secrets/anki_sync_username
  anki_sync_password:
    file: ./secrets/anki_sync_password

volumes:
  anki_data:

networks:
  assistant:
    internal: true
```

OpenClaw connects to `http://anki-mcp:8000/mcp` over the private network.

## 6. Configuration Contract

Secrets should support both direct environment variables and `_FILE` variants. If both are set, startup must fail rather than silently choosing one.

| Variable | Required | Default | Purpose |
|---|---:|---|---|
| `MCP_HOST` | No | `0.0.0.0` | HTTP bind address inside the container. |
| `MCP_PORT` | No | `8000` | HTTP port. |
| `MCP_PATH` | No | `/mcp` | Streamable HTTP MCP endpoint. |
| `MCP_AUTH_MODE` | Yes | `bearer` | `bearer` or `basic`. |
| `MCP_AUTH_TOKEN[_FILE]` | Bearer mode | — | Token accepted in the `Authorization` header using the Bearer scheme. |
| `MCP_BASIC_USERNAME[_FILE]` | Basic mode | — | HTTP Basic username. |
| `MCP_BASIC_PASSWORD[_FILE]` | Basic mode | — | HTTP Basic password; it may be a generated token. |
| `ANKI_COLLECTION_PATH` | No | `/data/collection.anki2` | Persistent client collection path. |
| `ANKI_SYNC_ENDPOINT` | No | empty | Empty for AnkiWeb; otherwise custom sync-server base URL. |
| `ANKI_SYNC_USERNAME[_FILE]` | Bootstrap/login | — | Sync account username. |
| `ANKI_SYNC_PASSWORD[_FILE]` | Bootstrap/login | — | Sync account password. |
| `ANKI_SYNC_HKEY[_FILE]` | No | persisted state | Existing sync host key; treated as a bearer secret. |
| `ANKI_BOOTSTRAP_MODE` | No | `disabled` | `disabled` or explicit `download_if_empty`. |
| `ANKI_SYNC_ON_READ` | No | `false` | Sync before read operations. Reads may otherwise reflect the last local sync. |
| `ANKI_SYNC_ON_WRITE` | No | `true` | Sync before and after mutation operations. |
| `ANKI_FULL_SYNC_POLICY` | No | `fail` | Must remain `fail` for normal unattended operation. |
| `ANKI_ALLOW_DESTRUCTIVE` | No | `false` | Enables registration of destructive tools. |
| `ANKI_ALLOW_SCHEMA_CHANGES` | No | `false` | Enables note-type and template mutation tools. |
| `ANKI_OPERATION_TIMEOUT_SECONDS` | No | `120` | Collection operation timeout. |
| `ANKI_SYNC_TIMEOUT_SECONDS` | No | `300` | Sync timeout. |
| `MCP_MAX_PAGE_SIZE` | No | `100` | Maximum list/search page size. |
| `ANKI_MAX_MEDIA_BYTES` | No | `1048576` | Maximum decoded media bytes accepted or returned per call. |
| `LOG_LEVEL` | No | `INFO` | Application log level. |

The service should authenticate once during bootstrap and persist the resulting sync host key under `/data/state`. Retaining the plaintext sync password after successful bootstrap should not be required when host-key authentication is sufficient.

## 7. MCP Transport and Authentication

- Endpoint: `POST`/`GET`/`DELETE /mcp` as required by the current MCP Streamable HTTP specification.
- Content: UTF-8 JSON-RPC messages using MCP lifecycle and tool methods.
- Authentication is checked on every request.
- Bearer authentication is the recommended default because OpenClaw can supply static headers.
- HTTP Basic is supported for deployments that explicitly require it; the password should be a generated high-entropy token, not the Anki sync password.
- Anki sync credentials and MCP credentials are independent trust boundaries.
- Health endpoints may be unauthenticated only when they expose no collection or credential details.

Recommended auxiliary endpoints:

- `GET /health/live`: process is alive.
- `GET /health/ready`: configuration is valid, persistent storage is writable, and the collection can be opened. It must not trigger remote sync on every probe.

## 8. Tool Surface

Tool names are stable API contracts. Inputs and outputs use Anki IDs where available; names are accepted only when ambiguity is handled explicitly. Every list/search tool is paginated and bounded.

### System and sync

| Tool | Scope | Purpose |
|---|---|---|
| `anki_status` | read | Return package version, collection state, last sync, media sync, and pending full-sync state. |
| `anki_sync` | write | Run an explicit normal collection sync and optionally wait for media sync. |
| `anki_backup_create` | admin | Create an explicit collection backup. |

### Decks

| Tool | Scope | Purpose |
|---|---|---|
| `anki_decks_list` | read | List decks with IDs and hierarchy. |
| `anki_decks_get` | read | Read deck metadata and configuration. |
| `anki_decks_create` | write | Create or return an existing deck by name. |
| `anki_decks_rename` | write | Rename a deck using its stable ID. |
| `anki_decks_update_config` | admin | Update supported deck options with strict validation. |
| `anki_decks_delete` | destructive | Delete a deck only with explicit confirmation. |

### Notes and tags

| Tool | Scope | Purpose |
|---|---|---|
| `anki_notes_search` | read | Search using Anki search syntax with pagination. |
| `anki_notes_get` | read | Return note fields, tags, note type, cards, and modification metadata. |
| `anki_notes_create` | write | Create one note with idempotency and duplicate checks. |
| `anki_notes_create_batch` | write | Create a bounded atomic batch and report per-item results. |
| `anki_notes_update_fields` | write | Patch named note fields. |
| `anki_notes_add_tags` | write | Add normalized tags to notes. |
| `anki_notes_remove_tags` | write | Remove tags from notes. |
| `anki_notes_replace_tags` | write | Replace one tag with another across a bounded selection. |
| `anki_notes_delete` | destructive | Delete notes and generated cards with confirmation. |

Collection-level tag resources use `anki_tags_list`, `anki_tags_rename`, and confirmed
`anki_tags_delete`. A standalone tag create operation is intentionally absent because the official
Anki API creates a meaningful tag by assigning it to a note; `anki_notes_add_tags` is that create
workflow.

### Cards and scheduling

| Tool | Scope | Purpose |
|---|---|---|
| `anki_cards_search` | read | Search cards using Anki search syntax. |
| `anki_cards_get` | read | Return card state, deck, template, flags, scheduling, and note ID. |
| `anki_cards_change_deck` | write | Move cards to another deck. |
| `anki_cards_suspend` | write | Suspend cards. |
| `anki_cards_unsuspend` | write | Unsuspend cards. |
| `anki_cards_set_flag` | write | Set or clear card flags. |
| `anki_cards_reposition` | admin | Change new-card positions with bounded inputs. |
| `anki_cards_answer` | admin | Record an actual review answer; disabled unless explicitly enabled. |

### Note types, fields, and templates

These tools can require a one-way full sync and are disabled unless `ANKI_ALLOW_SCHEMA_CHANGES=true`.

| Tool | Scope | Purpose |
|---|---|---|
| `anki_note_types_list` | read | List note types and stable IDs. |
| `anki_note_types_get` | read | Return fields, templates, CSS, and generation rules. |
| `anki_note_types_create` | admin | Create a standard note type with fields, templates, formats, and CSS. |
| `anki_note_types_update` | admin | Replace names, formats, and CSS while preserving field/template counts. |
| `anki_note_type_fields_update` | admin | Add, rename, reorder, or remove fields. |
| `anki_templates_update` | admin | Update template front, back, browser formats, or CSS. |
| `anki_note_types_delete` | destructive | Delete a note type only with impact report and confirmation. |

### Media

| Tool | Scope | Purpose |
|---|---|---|
| `anki_media_list` | read | List bounded filename and size metadata. |
| `anki_media_get` | read | Retrieve bounded base64 content for a safe filename. |
| `anki_media_store` | write | Create or replace media with filename, base64, and decoded-size validation. |
| `anki_media_rename` | write | Rename media without overwriting an existing target. |
| `anki_media_check` | read | Report missing and unused media. |
| `anki_media_delete` | destructive | Delete media with confirmation. |

## 9. Operation Semantics

### Read operations

By default, reads use the current local collection snapshot for low latency. Callers can request `sync_before=true`, or enable `ANKI_SYNC_ON_READ`, when freshness is more important than latency.

### Write operations

All mutations are serialized:

```text
acquire lock
→ sync remote changes down
→ validate authorization, IDs, limits, and confirmation
→ deduplicate/idempotency check
→ apply mutation through public Anki APIs
→ commit
→ sync changes up
→ wait for media sync when applicable
→ record audit result
→ release lock
```

The response distinguishes:

- `local_committed`: whether the local collection changed successfully.
- `remote_synced`: whether the change reached the configured sync server.
- `media_synced`: whether associated media transfer completed.
- `retryable`: whether a failed remote step may safely be retried.

If local commit succeeds but remote sync fails, the service must not repeat the mutation. A retry resumes synchronization using the operation's idempotency record.

### Idempotency

Create and batch-create tools require or generate an `idempotency_key`. The service persists an
intent before mutation and then replaces it with the result and stable IDs. Repeating the same key
with the same normalized request returns the original result; repeating it with different content
is rejected. If a crash occurs between the collection mutation and durable result receipt, the
intent remains `outcome_unknown` and fails closed instead of replaying. The operator must inspect
the collection before deciding whether to retry with a new key.

### Full-sync safety

A full sync is one-way and potentially destructive. The unattended policy is always:

- Detect `FULL_SYNC`, `FULL_UPLOAD`, or `FULL_DOWNLOAD` requirements.
- Stop the operation and mark readiness as degraded.
- Return an actionable `FULL_SYNC_REQUIRED` error.
- Require an operator to create a backup, enable the full-sync maintenance flag, and explicitly
  choose direction through the administrative tools. These tools are omitted during normal
  operation.

The AI must never choose a full-sync direction by itself.

## 10. Authorization Model

A single shared token is sufficient for the first private-sidecar deployment, but tools still carry internal scopes so the design can evolve to multiple credentials.

Scopes:

- `anki:read`: status, list, get, search, media checks.
- `anki:write`: note/deck/tag/card mutations and normal sync.
- `anki:admin`: deck configuration, backups, advanced scheduling such as reposition/review answers,
  note types, and templates. Core suspend/unsuspend controls remain ordinary `anki:write` tools as
  specified in the card tool table.
- `anki:destructive`: deletions and other irreversible operations.

With a single token, enabled scopes are configured at startup. Destructive and schema-changing tools are omitted from `tools/list` unless their feature flags are enabled.

## 11. Validation and Limits

- Reject unknown input fields.
- Require stable IDs for updates and deletes.
- Cap page sizes, batch sizes, media sizes, and search result counts.
- Validate note fields against the selected note type.
- Return an impact preview before note-type, template, deck, or bulk destructive changes.
- Require `confirm=true` plus a short-lived confirmation token derived from the impact preview for destructive tools.
- Sanitize filenames and prevent path traversal in media operations.
- Do not place collection content, sync credentials, MCP tokens, or media bytes in routine logs.

## 12. Error Model

Tool errors use stable machine-readable codes and a safe human-readable message:

- `AUTHENTICATION_FAILED`
- `AUTHORIZATION_FAILED`
- `INVALID_ARGUMENT`
- `NOT_FOUND`
- `AMBIGUOUS_NAME`
- `DUPLICATE_NOTE`
- `CONFLICT`
- `COLLECTION_BUSY`
- `SYNC_UNAVAILABLE`
- `FULL_SYNC_REQUIRED`
- `MEDIA_SYNC_FAILED`
- `DESTRUCTIVE_CONFIRMATION_REQUIRED`
- `INTERNAL_ERROR`

Errors include a request correlation ID but never echo credentials or raw authorization headers.

## 13. Persistence, Backups, and Recovery

The `/data` volume contains:

```text
/data/
├── collection.anki2
├── collection.media/
├── state/
│   ├── sync-auth
│   ├── idempotency.sqlite
│   └── operation-status.json
└── backups/
```

Requirements:

- Never use the same directory as a self-hosted sync server's `SYNC_BASE`.
- Back up before package upgrades, schema changes, destructive operations, and any explicit full sync.
- Keep scheduled external volume snapshots.
- Test restoring the collection and media into a disposable environment.
- Ensure only one live sidecar instance mounts the collection read/write.

## 14. Observability

Structured logs should include:

- correlation ID
- tool name
- operation class and scope
- target IDs/counts, but not full field contents by default
- local commit outcome
- remote and media sync outcomes
- duration and normalized error code

Recommended metrics:

- calls and failures by tool
- authentication failures
- collection-lock wait time
- sync duration and failures
- last successful collection/media sync timestamp
- pending unsynchronized local changes
- full-sync-required state
- backup age

## 15. Testing Strategy

Minimum automated coverage:

1. Exact MCP tool inventory, schemas, initialization, notifications, unknown methods, and malformed requests.
2. Bearer and Basic authentication success/failure without secret leakage.
3. Read/write/admin/destructive gating and feature-flagged tool omission.
4. Happy-path integration test for every exposed tool against a disposable collection.
5. Search pagination and bounded result behavior.
6. Idempotent creates and retry after local commit plus remote sync failure.
7. Concurrent tool calls proving single-writer serialization.
8. Sync-before-write and sync-after-write behavior against a test sync server.
9. Full-sync detection proving no automatic upload/download occurs.
10. Media upload plus asynchronous media-sync completion.
11. Destructive preview/confirmation and unchanged state after rejection.
12. Backup and restore smoke test.
13. Container startup, persistent restart, health checks, and non-root execution.
14. Upgrade test against the next pinned `anki` package version.

Before production use, perform an end-to-end test with a disposable sync account and real Anki client: device edit → sidecar sync/read → sidecar write/sync → device sync.

## 16. Delivery Phases

The implementation now includes the authenticated baseline and the Phase 1 safe synchronized note
core. The phases below record delivered behavior and order remaining work by operational
dependency. A phase is complete only when its tools, safety semantics, and integration tests are
all delivered.

### Delivered baseline: authenticated deck and Basic-card service (`0.2.0`)

- Non-root container and Compose sidecar with a persistent `/data` volume, private MCP network,
  separate sync egress, health checks, and one Uvicorn worker.
- Authenticated Streamable HTTP MCP using bearer tokens, direct or `_FILE` secrets, strict tool
  inputs, bounded requests/responses, and host/origin checks.
- One dedicated collection-owner thread serializes all local collection and sync calls.
- Bounded deck list/get/create/rename/delete and card search/get/create/update/move/delete tools.
  Card creation and card update/move are intentionally limited to Anki's built-in single-card
  Basic note type.
- Explicit sync login and collection sync against AnkiWeb or a custom endpoint, including an
  optional media-sync request and trusted endpoint migration handling. Media-sync completion is
  not yet tracked.
- Full-sync requirements are reported without automatically choosing a direction. Confirmed
  upload/download tools require a preceding compatible server requirement and request a local
  backup first.
- Automated coverage for configuration, authentication, MCP schemas and calls, collection CRUD,
  serialization, response bounds, rollback behavior, sync safety, and Compose isolation.

The baseline did **not** provide automatic sync around mutations, persisted sync authentication,
operation idempotency, general note/tag workflows, scheduling controls, media tools, scoped
authorization, audit state, or preview-token protection. Phase 1 supplies the synchronization,
note/tag, core card-control, scoped authorization, and durable operation pieces. Delete and
full-sync tools still use strict `confirm=true`; treat them as a provisional private-deployment
interface until the guarded administration phase is complete.

### Delivered Phase 1: safe synchronized note workflows

- Enforce internal read/write/admin/destructive scopes for the current shared credential and omit
  feature-gated tools from discovery when their scope or safety flag is disabled.
- Add `anki_status` and explicit backup creation, persisted sync host-key state, controlled
  bootstrap, and actionable readiness/status for authentication and pending full-sync conditions.
- Add the operation coordinator: optional sync-before-read, sync-before-and-after-write, durable
  mutation receipts, idempotency keys, and sync-only retry after a local commit.
- Fail closed when normal operations encounter a full-sync requirement. Keep direction selection
  operator-controlled and reconcile the baseline full-sync tools with the administrative boundary
  defined in Section 9.
- Implement general note search/get/create/batch-create/field-update and tag add/remove workflows,
  with duplicate checks, stable IDs, bounded atomic batches, and field validation.
- Complete core card controls for arbitrary supported cards: change deck, suspend, and unsuspend.
- Prove the complete write lifecycle, restart recovery, idempotency, and full-sync behavior against
  a disposable official self-hosted sync server.

Phase 1 is covered by unit and MCP protocol tests plus an end-to-end test that launches the official
self-hosted sync server from the pinned `anki` package. The test proves initial full-upload gating,
operator-selected resolution, persisted host-key restart, receipt reconciliation and replay, a
second client's full download, and a subsequent normal synchronized write/read lifecycle.

### Delivered Phase 2A: critical-resource CRUD

- Complete confirmed note deletion and collection-level tag list/rename/delete workflows.
- Add read-only note-type inspection and schema-gated standard note-type create/update/delete.
  Updates preserve field/template counts to avoid an ambiguous field-mapping API.
- Add bounded media list/get/store/rename/delete with strict filename validation, base64 transport,
  decoded-size limits, rollback-aware replacement, and Anki media trash semantics. Media reads that
  request synchronization and all automatically synchronized media mutations enable Anki media
  transfer; durable receipts track and retry media completion without replaying local writes.
- Keep all new mutations inside the durable synchronized/idempotent coordinator and cover every
  exposed tool through real MCP JSON-RPC calls against disposable official Anki collections.

### Remaining Phase 2: complete non-destructive administration

- Add deck configuration updates and advanced field/template add, remove, and reorder mappings.
- Add card flags, repositioning, and bounded bulk operations.
- Add media consistency checking and media-sync completion tracking.
- Add operation-status APIs and reporting over the durable coordinator state delivered in Phase 1,
  structured audit events, metrics, backup/restore smoke tests, and container persistent-restart
  tests.

### Phase 3: guarded advanced administration

- Add note-type, field, template, and CSS mutations behind `ANKI_ALLOW_SCHEMA_CHANGES`.
- Replace provisional confirmation-only deletion with impact previews, short-lived confirmation
  tokens, backups where required, feature-gated registration, and tests proving rejected operations
  leave the collection unchanged.
- Add guarded note, note-type, and media deletion; retain deck/card deletion under the same model.
- Add optional review answering only behind an explicit feature flag and administrative scope.
- Add multiple MCP credentials mapped to the established read/write/admin/destructive scopes if
  deployment requirements justify them; add HTTP Basic only if a concrete client requires it.

## 17. Key Decisions

- Use the official `anki` Python package, not AnkiConnect or direct SQLite writes.
- Use the official Python MCP SDK's `FastMCP`, mounted in Starlette and served by Uvicorn.
- Use Streamable HTTP MCP at `/mcp`.
- Prefer bearer authentication; optionally support HTTP Basic with a generated token as password.
- Keep MCP authentication separate from Anki sync authentication.
- Run one replica with one persistent client collection.
- Use AnkiWeb by default; a custom endpoint enables an official self-hosted sync server.
- Serialize `sync down → mutate → sync up` for writes.
- Fail closed on full-sync requirements.
- Expose typed tools with scoped safety controls rather than raw backend access.

## 18. References

- Anki Python package: https://pypi.org/project/anki/
- Anki collection API: https://github.com/ankitects/anki/blob/main/pylib/anki/collection.py
- Anki Python module guide: https://addon-docs.ankiweb.net/the-anki-module.html
- Anki synchronization behavior: https://docs.ankiweb.net/syncing.html
- Official self-hosted sync server: https://docs.ankiweb.net/sync-server.html
- MCP Streamable HTTP transport: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- OpenClaw MCP configuration: https://docs.openclaw.ai/cli/mcp
