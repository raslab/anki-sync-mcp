# Anki MCP exploratory test report — 2026-07-25

## Summary

The latest connected server build was tested live through its MCP tools against the configured AnkiWeb-backed collection.

- Exposed Anki tools tested: **37 of 37**
- Happy-path tool calls: **37 passed**
- Cross-cutting and negative scenarios: idempotency, pagination, synchronization, validation, not-found behavior, atomicity, durable receipts, metrics, media consistency, and recovery after client-side throttling
- Final server state: `ready=true`, authenticated, no pending full sync, no pending mutations
- Final durable mutation state: 21 committed, 0 pending, 0 outcome-unknown
- Server defects found: 1
- Connected-client/integration defects found: 1
- Destructive, schema-changing, and full-sync maintenance tools were not exposed by this server configuration and therefore were outside the connected-tool scope.

The core collection, synchronization, deck, note, tag, card, note-type inspection, media, operation, and metric workflows all completed successfully. The server is operational, but the input-schema error format does not satisfy the documented stable error contract. The connected Hermes MCP client also temporarily opened its circuit after three expected tool-level errors even though the server remained healthy.

## Environment and safety controls

| Item | Value |
| --- | --- |
| Test window start | `2026-07-25T10:24:48Z` |
| Anki package | `26.5` |
| Remote | AnkiWeb, host number 32 |
| Initial status | Ready, authenticated, no pending full sync, no pending mutations |
| Safety backup | `/data/backups/backup-2026-07-25-10.24.49.colpkg` |
| Fixture namespace | `MCP Exploratory 20260725` and `explore-20260725-*` idempotency keys |
| Final collection sync | `2026-07-25T10:32:10.797194+00:00` |
| Final media sync | `2026-07-25T10:32:10.803920+00:00` |

The test changed only namespaced fixtures except for a temporary update to shared deck configuration ID 1. That configuration was changed from `20/200/60/0.90` to `21/201/61/0.91`, verified, and restored to its original values before completion.

Because destructive tools were not exposed, the following synced fixtures remain intentionally available for inspection:

- Decks: `MCP Exploratory 20260725::Source` (`1784975111765`) and `MCP Exploratory 20260725::Target` (`1784975137460`)
- Notes/cards: main note/card `1784975159918`, batch notes/cards `1784975167600` and `1784975167601`, compatibility card/note `1784975171403`
- Tags: `explore-added`, `explore-batch`, `explore-renamed`, and registry residue `explore::alpha`
- Media: `explore-unused-renamed-20260725.txt`
- Intentional missing-media reference: `explore-missing.png`

Cards were left unsuspended and flags were restored to 0. The two repositioned test cards retain new-card due positions 100 and 102.

## Exposed-tool coverage

### System, synchronization, and operation tracking

| Tool | Cases exercised | Result |
| --- | --- | --- |
| `anki_status` | Initial, post-error-recovery, and final readiness/auth/sync/pending-state checks | PASS |
| `anki_operations_list` | Empty baseline, first page, second page, ordering, totals, and `has_more` | PASS |
| `anki_operations_get` | Existing receipt and missing idempotency key | PASS |
| `anki_metrics` | Empty baseline, operation counts by type, sync timestamps, final 21/21 committed state | PASS |
| `anki_sync_login` | Reauthentication with configured AnkiWeb credentials | PASS |
| `anki_sync` | `sync_media=true` and `sync_media=false`; both returned `NO_CHANGES`; final media completion confirmed | PASS |
| `anki_backup_create` | Explicit `.colpkg` creation before mutations | PASS |

### Decks

| Tool | Cases exercised | Result |
| --- | --- | --- |
| `anki_decks_list` | Baseline, limit-1 pagination with `has_more=true`, invalid negative offset | PASS |
| `anki_decks_get` | Existing deck, missing deck, hierarchy/config fields, `sync_before=true` | PASS |
| `anki_decks_create` | New parent deck, new child/target deck, exact idempotent replay, conflicting replay | PASS |
| `anki_decks_update` | Rename to hierarchical source deck and verify by get | PASS |
| `anki_decks_update_config` | Update all supported values, verify, reject empty update, restore original shared config | PASS |

### Notes and tags

| Tool | Cases exercised | Result |
| --- | --- | --- |
| `anki_notes_search` | Deck and tag queries, first/second pages, empty result, invalid limit | PASS |
| `anki_notes_get` | Fields/tags/card IDs, post-update verification, missing note | PASS |
| `anki_notes_create` | Valid Basic note with tags/media reference; duplicate first field rejection | PASS |
| `anki_notes_create_batch` | Two-note atomic success; duplicate-within-batch rejection and zero partial notes | PASS |
| `anki_notes_update_fields` | Valid partial Back update and read-back | PASS |
| `anki_notes_add_tags` | Two-note bounded tag update and deduplication behavior | PASS |
| `anki_notes_remove_tags` | Case-normalized removal and read-back | PASS |
| `anki_tags_list` | First/second pages, sorted output, totals, and `has_more` | PASS |
| `anki_tags_rename` | Existing tag renamed across two notes; missing source tag rejected | PASS |

### Cards and scheduling

| Tool | Cases exercised | Result |
| --- | --- | --- |
| `anki_cards_search` | Deck query, first/second pages, stable IDs and totals | PASS |
| `anki_cards_get` | Rendered question/answer, fields, deck, flag, due/queue state, missing card | PASS |
| `anki_cards_create` | Basic compatibility card creation | PASS |
| `anki_cards_update` | Front, Back, and deck updated together; empty update rejected | PASS |
| `anki_cards_change_deck` | Two arbitrary cards moved to target deck and read back | PASS |
| `anki_cards_suspend` | Two cards suspended; queue verified as `-1` | PASS |
| `anki_cards_unsuspend` | Two cards restored; queue verified as active | PASS |
| `anki_cards_set_flag` | Flag set to 3, read back, invalid flag 8 rejected, flag cleared to 0 | PASS with BUG-01 error-format issue |
| `anki_cards_reposition` | Two new cards positioned at 100 and 102; starting position 0 rejected | PASS with BUG-01 error-format issue |

### Note types

| Tool | Cases exercised | Result |
| --- | --- | --- |
| `anki_note_types_list` | Full baseline plus offset/limit pagination | PASS |
| `anki_note_types_get` | Basic fields, template formats, CSS, kind, usage; missing ID | PASS |

Schema-changing note-type tools were correctly absent because schema changes are disabled.

### Media

| Tool | Cases exercised | Result |
| --- | --- | --- |
| `anki_media_list` | Empty baseline, stored-file page, beyond-end page | PASS |
| `anki_media_get` | Exact base64 round trip before/after rename; old/missing filename rejection | PASS |
| `anki_media_check` | Correctly reported one missing and one unused file with totals and trash state | PASS |
| `anki_media_store` | 18-byte file create, media-sync completion, path traversal rejection, invalid-base64 rejection | PASS |
| `anki_media_rename` | Rename without content change, old name absent, target readable, media sync complete | PASS |

### MCP prompts and resources

`list_prompts` and `list_resources` both returned empty lists. Therefore prompt/resource retrieval had no valid target and was not applicable. These are generic MCP connector helpers, not registered Anki domain tools.

## Cross-cutting test log

| ID | Scenario | Expected | Observed | Result |
| --- | --- | --- | --- | --- |
| X-01 | Create safety backup before live writes | Durable backup path returned | `.colpkg` created under `/data/backups` | PASS |
| X-02 | Explicit login and collection/media synchronization | Authenticated; normal sync; media completion | Authenticated; `NO_CHANGES`; media completed | PASS |
| X-03 | Exact idempotent create replay | Original receipt and stable ID, no replay | Same deck ID and original `created=true` receipt returned | PASS |
| X-04 | Reuse idempotency key with changed request | Stable conflict error; no mutation | `CONFLICT`; alternate deck not created | PASS |
| X-05 | List/search pagination | Correct slice, total, offset, limit, `has_more` | Verified for decks, notes, cards, tags, note types, media, operations | PASS |
| X-06 | Optional synchronization before read | Sync then return local resource | Deck returned; sync timestamp advanced | PASS |
| X-07 | Missing stable IDs/files/operations | `NOT_FOUND` with safe message and correlation ID | Correct for deck, note, card, note type, media, operation | PASS |
| X-08 | Invalid pagination | `INVALID_ARGUMENT` | Negative offset and zero limit rejected | PASS |
| X-09 | Duplicate single-note create | `DUPLICATE_NOTE`; no second note | Correct | PASS |
| X-10 | Duplicate within note batch | Whole batch rejected atomically | `DUPLICATE_NOTE`; follow-up search returned zero notes | PASS |
| X-11 | Empty mutation requests | Reject without durable receipt | Empty deck-config and card updates rejected | PASS |
| X-12 | Card scheduling validation | Reject out-of-range flag/start | Rejected before tool body; see BUG-01 | PARTIAL |
| X-13 | Media safety | Exact base64, plain filename only, valid base64 only | Round trip exact; traversal/base64 rejected | PASS |
| X-14 | Media consistency report | Missing and unused fixtures classified separately | 1 missing, 1 unused | PASS |
| X-15 | Durable operation observability | Successful writes listed and counted; failures absent; no pending state | 21 committed, 0 pending/unknown, per-operation counts correct | PASS |
| X-16 | Expected-error burst through connected client | Tool errors should not imply transport outage | Circuit opened after third expected error; see BUG-02 | FAIL (client integration) |
| X-17 | Final health and restoration | Ready, synced, no pending mutation/full sync, shared config restored | All conditions verified | PASS |

## Bugs found

### BUG-01 — Input-schema validation bypasses the stable JSON tool-error contract

- Classification: server/API contract defect
- Severity: Medium
- Status: Reproducible
- Affected tools: any argument rejected by the generated Pydantic/FastMCP argument model; directly reproduced with `anki_cards_set_flag` and `anki_cards_reposition`

Reproduction A:

- Call `anki_cards_set_flag(card_ids=[1784975159918], flag=8, ...)`.
- Observed error starts with `1 validation error for cards_set_flagArguments` and includes a Pydantic documentation URL.

Reproduction B:

- Call `anki_cards_reposition(card_ids=[1784975159918], starting_from=0, ...)`.
- Observed error starts with `1 validation error for cards_repositionArguments` and includes a Pydantic documentation URL.

Expected behavior:

All tool errors should use the documented compact JSON envelope with a stable code such as `INVALID_ARGUMENT`, a safe message, and a correlation ID. Runtime validation, such as negative pagination or invalid base64, already does this correctly.

Impact:

Clients cannot uniformly parse or correlate invalid-input failures. The response shape depends on whether validation occurs in the generated argument model or inside the tool/service body.

Root cause evidence:

- Constrained argument aliases are declared in `src/anki_mcp/app.py:33-53`.
- The stable error conversion is implemented inside `execute()` at `src/anki_mcp/app.py:141-173`.
- FastMCP validates generated argument models before entering the tool function, so those validation errors never reach `execute()`.
- The post-registration schema/model adjustment at `src/anki_mcp/app.py:900-919` tightens validation but does not install a validation-error adapter.

Recommendation:

Normalize FastMCP/Pydantic argument-validation failures at the MCP call boundary into the same safe `INVALID_ARGUMENT` JSON envelope, and add protocol-level tests for both constrained scalar failures and forbidden extra inputs.

### BUG-02 — Connected Hermes MCP client treats expected tool errors as transport failures

- Classification: connected-client/integration defect; not reproduced as an Anki server process failure
- Severity: Medium for agents that perform validation-heavy workflows
- Status: Reproducible in this session

Reproduction:

1. Call missing deck, note, and card IDs consecutively.
2. Each server response is a valid expected `NOT_FOUND` tool error.
3. The connector then rejects subsequent calls with: `MCP server 'anki_mcp_dev' is unreachable after 3 consecutive failures`, including an approximately 58-second retry delay.
4. After the delay, `anki_status` immediately returned `ready=true`, authenticated, with no pending full sync or mutations.

Expected behavior:

Application-level MCP tool errors (`isError` results such as `NOT_FOUND`, `INVALID_ARGUMENT`, or `DUPLICATE_NOTE`) should not count as transport/connectivity failures. Only connection, protocol, timeout, or server-unavailable failures should open the transport circuit.

Impact:

Three normal negative validations can make every tool temporarily unavailable, delaying autonomous workflows and obscuring the distinction between an unhealthy server and a healthy server rejecting bad input.

Root cause assessment:

The Anki service remained healthy and recovered without restart, and its valid structured errors were observed before the circuit opened. The likely fault is the connected Hermes MCP client's consecutive-failure classification rather than this repository's Anki server implementation. No source for that connector is present in this repository, so the external root cause was not patched here.

Recommendation:

Change the connector circuit breaker to count only transport/protocol availability failures. Preserve tool-level errors as successful transport exchanges and reset/leave unchanged the connectivity failure counter.

## Final assessment

All 37 Anki tools actually exposed to this session completed their intended happy paths. Collection synchronization, media synchronization, durable receipts, pagination, atomic batch behavior, duplicate prevention, card scheduling controls, and safe media handling worked with real Anki and AnkiWeb state.

The server is ready for continued use with one API-consistency issue: schema-level input failures need normalization into the documented stable error envelope. Separately, the connected Hermes MCP client should stop opening its transport circuit on expected tool errors. No data-loss, failed commit, pending operation, authentication failure, or full-sync safety issue was observed.
