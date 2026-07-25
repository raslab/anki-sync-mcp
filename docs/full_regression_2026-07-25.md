# Anki MCP full regression and exploratory test report — 2026-07-25

## Executive summary

Recommendation: **conditionally ready for a limited/canary production rollout of the 37 tools exposed by the tested deployment, after deployment provenance and basic operational smoke checks are established; not yet an unconditional broad-production approval.**

The connected `anki_mcp_dev` server was tested live against its authenticated AnkiWeb-backed collection. Every exposed Anki tool completed a positive path, mutations were read back, shared deck configuration was restored, and the final collection/media sync completed with no pending or outcome-unknown work. The repository regression suite, Ruff, and Pyright also passed.

No new server-side data-integrity or synchronization defect was found. The previously reported schema-validation envelope defect is fixed and was verified live. One medium-severity integration defect remains outside this repository: the connected Hermes MCP client opens its circuit after expected application-level tool errors and temporarily reports a healthy server as unreachable.

The latest source also contains guarded destructive/schema/review administration tools, but those feature-gated tools were not present in this connected deployment's tool inventory. They passed repository tests but were not live-tested here. Production enablement of those additional tools should therefore have a separate staging/live acceptance pass.

## Scope and evidence

| Item | Result |
| --- | --- |
| Repository source baseline | `e48026ecf2bf1a185e4ab5dee4c3e96ac13e7b10` (`master` at test start) |
| Connected server provenance | Not independently attested: no build/version endpoint or deployed image digest was exposed |
| Anki version | `26.5` |
| Connected Anki tools | 37 |
| Live happy-path coverage | 37/37 |
| Prompt registry | Empty; retrieval not applicable |
| Resource registry | Empty; retrieval not applicable |
| Safety backup | `/data/backups/backup-2026-07-25-19.32.27.colpkg` |
| Fresh fixture namespace | `MCP Regression 20260725 1932` / `regression-20260725-1932-*` |
| Automated tests | 154 passed (`uv run pytest -q`) |
| Statement coverage | 90% overall |
| Static checks | `uv run ruff check .` passed; `uv run pyright` passed with 0 errors/warnings |
| Final collection sync | `2026-07-25T19:38:08.541055+00:00` |
| Final media sync | `2026-07-25T19:38:08.544210+00:00` |
| Durable operation baseline | 21 committed before this run |
| Fresh run delta | +21 committed |
| Final operation state | 42 committed, 0 pending, 0 outcome-unknown |
| Final readiness | `ready=true`, authenticated, no pending full sync, no pending mutations |

Testing used the actual connected MCP functions rather than direct database access. Positive calls covered defaults and explicit options; negative calls covered representative schema, domain, not-found, duplicate, atomicity, path-safety, and idempotency failures. This is broad regression/exploratory coverage, not a proof of every combinatorial input value.

The checked-out repository parent of this report is the stated source baseline, but the connected process did not expose a build SHA, image digest, or equivalent attestation. The live tool inventory and behavior are consistent with the default 37-tool configuration, but this report cannot cryptographically prove that the deployed binary was built from that commit. Before production rollout, record the immutable image digest and effective configuration/scopes and verify them against the tested artifact.

## Operational coverage not demonstrated live

The following production concerns were not exercised end to end against the connected deployment:

- process/container restart and crash recovery with pending or outcome-unknown receipts;
- restoration from the generated `.colpkg` backup;
- HTTP bearer-authentication rejection, Host/Origin allowlists, request-size limits, and external readiness routing;
- sustained load, rate limits, and multi-client concurrency beyond a small burst of independently submitted mutations;
- production image build, Compose startup, persistent-volume ownership/permissions, and deployment rollback;
- monitoring/alert delivery for readiness, pending mutations, sync failures, disk exhaustion, or backup failure.

Repository tests cover several related code paths, but they are not a substitute for deployment acceptance. These items are rollout gates or canary checks, not evidence of a defect in the live tool behavior tested here.

## Safety and restoration

A real `.colpkg` backup was created before fresh mutations. All new data used a unique namespace and explicit idempotency keys. The shared deck configuration was snapshotted as `20/200/60/0.90`, changed to `21/201/61/0.91`, read back, restored to `20/200/60/0.90`, and read back again. Test cards were unsuspended and flags were cleared to 0 before completion. A final collection and media sync returned `NO_CHANGES` with media completion.

Destructive tools were not exposed, so namespaced test fixtures could not be deleted through the connected API. Residual fixtures are documented below.

## Live per-tool coverage

### Status, synchronization, backup, operations, and metrics

| Tool | Positive/options tested | Negative/boundary tested | Result |
| --- | --- | --- | --- |
| `anki_status` | Initial, recovery, and final readiness; auth/full-sync/pending state | Health checked after expected tool failures | PASS |
| `anki_sync_login` | Reauthentication using configured AnkiWeb credentials | Missing credential configuration covered by automated suite | PASS |
| `anki_sync` | `sync_media=false` and `true`; endpoint change and unchanged endpoint; collection/media completion | Full-sync/error branches covered by automated tests | PASS |
| `anki_backup_create` | Explicit backup with returned durable path | Failure behavior covered by automated tests | PASS |
| `anki_operations_list` | Defaults and explicit offset/limit; later page; total/`has_more`; ordering | Rejected mutations verified absent | PASS |
| `anki_operations_get` | Existing committed receipt | Missing/rejected idempotency key returned `NOT_FOUND` | PASS |
| `anki_metrics` | Per-operation counts, sync timestamps, pending/unknown totals; baseline 21 and final 42 | Rejected calls did not increment durable writes; fresh run delta was exactly +21 | PASS |

### Decks

| Tool | Positive/options tested | Negative/boundary tested | Result |
| --- | --- | --- | --- |
| `anki_decks_list` | `sync_before=true/false`; full page and bounded pagination | Negative offset returned stable `INVALID_ARGUMENT`; conflicting replay did not create a deck | PASS |
| `anki_decks_get` | Existing deck, hierarchy, config, both `sync_before` values | Missing positive ID returned `NOT_FOUND` | PASS |
| `anki_decks_create` | Source/target hierarchy; explicit idempotency; exact replay returned original stable ID | Same key/different name returned `CONFLICT` and no alternate deck appeared | PASS |
| `anki_decks_update` | Rename with hierarchy and read-back | Missing IDs/schema limits covered by automated tests | PASS |
| `anki_decks_update_config` | All four optional settings supplied; changed-state read-back; exact restoration | Empty/no-op and range/type validation covered live previously and by automated tests | PASS |

### Notes and tags

| Tool | Positive/options tested | Negative/boundary tested | Result |
| --- | --- | --- | --- |
| `anki_notes_search` | Anki query, `sync_before=true/false`, first/later/beyond-result pages | Atomic rollback search proved zero partial note creation | PASS |
| `anki_notes_get` | Fields, note type, tags, card IDs, update read-back | Missing note returned `NOT_FOUND` | PASS |
| `anki_notes_create` | Basic fields, tags, missing-media reference, explicit idempotency | Duplicate first-field behavior covered in batch/live and automated single-create tests | PASS |
| `anki_notes_create_batch` | Two-note atomic success with per-note fields/tags | Mixed new+duplicate batch returned `DUPLICATE_NOTE`; follow-up search proved no partial commit | PASS |
| `anki_notes_update_fields` | Partial Back-field update and read-back | Unknown/empty fields covered by automated tests | PASS |
| `anki_notes_add_tags` | Multi-note update and duplicate input tag normalization | Limits/missing IDs covered by automated tests | PASS |
| `anki_notes_remove_tags` | Case-normalized removal and read-back | Missing IDs/limits covered by automated tests | PASS |
| `anki_tags_list` | Full and later-page offset/limit, sorted names, total/`has_more` | Bounds covered by shared pagination tests | PASS |
| `anki_tags_rename` | Existing tag renamed and verified on note | Missing source covered in prior live regression and automated tests | PASS |

### Cards and scheduling

| Tool | Positive/options tested | Negative/boundary tested | Result |
| --- | --- | --- | --- |
| `anki_cards_search` | Query, `sync_before=true/false`, first/later/beyond-end pages | Invalid pagination covered by shared tests | PASS |
| `anki_cards_get` | Rendered question/answer, fields, deck, flag, full scheduling state | Missing card returned `NOT_FOUND` | PASS |
| `anki_cards_create` | Basic compatibility create with explicit idempotency | Duplicate/field limits covered by automated tests | PASS |
| `anki_cards_update` | Front, Back, and deck together; read-back | Empty update and invalid IDs covered by automated tests/prior live run | PASS |
| `anki_cards_change_deck` | Two arbitrary cards moved and read back | Invalid/missing IDs covered by automated tests | PASS |
| `anki_cards_suspend` | Two cards; queue read back as `-1` | Invalid/missing IDs covered by automated tests | PASS |
| `anki_cards_unsuspend` | Two cards; active queue restored and read back | Invalid/missing IDs covered by automated tests | PASS |
| `anki_cards_set_flag` | Flag 4 set/read back; flag 0 restoration | Flag 8 returned normalized `INVALID_ARGUMENT` without Pydantic details | PASS |
| `anki_cards_reposition` | Two new cards; start 200, step 3, `randomize=false`, `shift_existing=true`; due read-back | Start/range/type validation covered live previously and by automated tests | PASS |

### Note types

| Tool | Positive/options tested | Negative/boundary tested | Result |
| --- | --- | --- | --- |
| `anki_note_types_list` | Full list, offset/limit, beyond-end page, both sync modes | Pagination bounds covered by shared tests | PASS |
| `anki_note_types_get` | Basic kind, CSS, fields, templates, usage, `sync_before=true` | Missing ID covered in prior live regression and automated tests | PASS |

### Media

| Tool | Positive/options tested | Negative/boundary tested | Result |
| --- | --- | --- | --- |
| `anki_media_list` | Full/offset pages, sizes, both sync modes | Beyond-end behavior covered | PASS |
| `anki_media_get` | Exact base64 and byte-size round trip after rename; both sync modes | Missing file participated in client-circuit reproduction; server not independently implicated | PASS |
| `anki_media_check` | Missing vs unused classification, pagination metadata, trash state, sync option | Correctly found two intentional missing and two unused fixtures across both test runs | PASS |
| `anki_media_store` | Valid base64 create, 17-byte size, media sync | Traversal filename and invalid base64 returned stable `INVALID_ARGUMENT`; no receipt | PASS |
| `anki_media_rename` | Rename, content preservation, old/new identity, media sync | Overwrite/missing/path cases covered by automated tests | PASS |

## Cross-cutting cases

| ID | Scenario | Expected | Observed | Result |
| --- | --- | --- | --- | --- |
| X-01 | Safety backup before writes | Durable archive path | `.colpkg` path returned | PASS |
| X-02 | Concurrently submitted independent mutations | Serialized, durable commits without corruption | All receipts committed and remotely synced; final state consistent | PASS |
| X-03 | Exact idempotent replay | Original stable result, no second mutation | Original deck ID/receipt returned; total remained consistent | PASS |
| X-04 | Same idempotency key, changed request | `CONFLICT`, no mutation | Stable error; `SHOULD-NOT-EXIST` deck absent | PASS |
| X-05 | Atomic batch rejection | No partial write | Duplicate rejected; unique companion note absent | PASS |
| X-06 | Generated-schema failure | Stable safe envelope | Flag 8 returned `INVALID_ARGUMENT`, generic safe message, correlation ID; no Pydantic URL/input leak | PASS |
| X-07 | Runtime/domain validation | Stable safe envelope | Pagination, media path, and base64 errors returned `INVALID_ARGUMENT` with correlation IDs | PASS |
| X-08 | Not-found handling | Safe `NOT_FOUND` | Deck/note/card/operation cases behaved as expected | PASS |
| X-09 | Durable observability | Successful writes tracked; failures absent | Baseline 21, run delta +21, final 42/42 committed, 0 pending/unknown; rejected batch operation absent | PASS |
| X-10 | Shared configuration restoration | Exact original values restored | `20/200/60/0.90` verified after restore | PASS |
| X-11 | Card state restoration | Unsuspended and unflagged | Queue 0 and flag 0 verified; due remains intentionally repositioned | PASS |
| X-12 | Final sync/readiness | Fully synced and ready | `NO_CHANGES`; media completed; no pending mutation/full sync | PASS |
| X-13 | Burst of expected application errors | Client remains connected | Connector opened circuit and blocked the next call for about 57 seconds; server later returned healthy without restart | FAIL — external integration |

## Findings

### FINDING-01 — Hermes connector counts normal tool errors as connectivity failures

- Classification: connected-client/integration defect, outside this repository
- Severity: Medium
- Status: Reproduced
- Server impact: none observed; server remained ready and required no restart

Reproduction:

1. Invoke valid tools with nonexistent stable IDs so the server returns normal MCP tool errors (`NOT_FOUND`).
2. After the expected error sequence, invoke another tool.
3. The connector returns `MCP server 'anki_mcp_dev' is unreachable after 3 consecutive failures` and applies an approximately 57-second retry delay.
4. After the cooldown, normal calls and final sync immediately succeed; `anki_status` reports `ready=true`.

Impact: validation-heavy or self-correcting agents can be delayed and may incorrectly diagnose a healthy service as down. Application-level `isError` responses should be treated as successful transport exchanges, not circuit-breaker failures.

Recommendation: fix the Hermes MCP connector to increment its circuit breaker only for transport/protocol/timeout/server-unavailable failures. Until then, production agents should validate locally where possible, avoid bursts of speculative calls, and use backoff when receiving the connector-level unreachable message.

### FINDING-02 — Guarded Phase 3 tools were not exposed by the tested deployment

- Classification: coverage/deployment limitation, not a demonstrated bug
- Severity: Medium if these tools will be enabled in production; Informational otherwise

The current source contains feature-gated guarded administration capabilities, but the connected inventory contained only the 37 tools listed above. Repository tests cover guarded previews, confirmation tokens, stale impact, replay, backups, schema changes, destructive operations, and review administration, but live Anki/AnkiWeb behavior was not exercised through this deployment.

Recommendation: if production will enable destructive/schema/review feature flags, deploy the exact production configuration to staging and run a separate guarded-tool acceptance test, including preview/token expiry, payload mismatch, stale state, replay rejection, backup verification, final sync, and recovery.

## Previously reported server defect status

The earlier schema-validation inconsistency is **fixed**. A live out-of-range `flag=8` call now returned:

- code: `INVALID_ARGUMENT`
- safe message: `tool arguments failed validation`
- correlation ID: present
- leaked Pydantic URL/input details: absent

This aligns generated argument-model failures with the stable tool error contract.

## Residual synchronized fixtures

No destructive tool was exposed, so the fresh test left these deliberate fixtures:

- Parent deck: `MCP Regression 20260725 1932` (`1785008020638`, implicitly created by hierarchy)
- Source deck: `MCP Regression 20260725 1932::Source Renamed` (`1785008020639`)
- Target deck: `MCP Regression 20260725 1932::Target` (`1785008021825`)
- Main note/card: `1785008038281`
- Batch notes/cards: `1785008039531`, `1785008039532`
- Compatibility note/card: `1785008040767`
- Tags: `regression-live`, `regression-renamed`, `regression-added`, `regression-batch`
- Media: `regression-unused-renamed-20260725-1932.txt`
- Intentional missing-media reference: `regression-missing-20260725.png`

The main and first batch cards remain repositioned at due positions 200 and 203. They are unsuspended and flags are 0. Shared deck configuration is restored.

## Production decision

For the observed 37-tool configuration, the server showed strong functional reliability: every tool's happy path worked, durable mutation/idempotency behavior held, invalid requests did not produce partial writes or durable receipts, media round trips were exact, shared state was restored, and synchronization ended cleanly. A canary production rollout is reasonable only after pinning and recording the tested image digest/configuration and completing restart, restore, HTTP security/readiness, container startup, and basic load smoke checks. Operate the canary with backups, metrics/readiness monitoring, and explicit awareness of the Hermes client circuit-breaker defect.

Do not yet claim equivalent live reliability for feature-gated guarded administration tools. If those tools are part of the intended production configuration, complete their staging/live acceptance test before enabling them for production traffic.
