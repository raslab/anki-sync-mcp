# FSRS Independent Review Backlog

Status: deferred for later implementation  
Reviewed commit: `32a890fc49b26a76e5b2f9e20281a269d9641468`  
Review date: 2026-07-27

## Purpose

This document preserves the findings from three independent reviews of the native Anki FSRS tools:

1. MCP tool concision and usability for autonomous agent systems.
2. Security, reliability, and operational correctness.
3. General code quality and readiness for public OSS publication.

It is a prioritized backlog, not a claim that the findings have already been implemented. Duplicate findings from different reviewers are consolidated, while each review lens remains identified.

## Current release recommendation

Do not publish the FSRS feature as OSS yet. No Critical or High-severity security vulnerability was identified, but the P0 safety work and P1 contract/validation work below should be completed before release. Public documentation and release-level test coverage are also publication blockers.

## Shared completion gates

Every implementation item must satisfy these gates unless its own Definition of Done (DoD) says otherwise:

- Add a regression test that fails before the fix and passes afterward for every non-trivial behavior change.
- Preserve native Anki 26.5 FSRS behavior; do not replace optimizer, simulator, or rescheduling mathematics with Python approximations.
- Preserve destructive rescheduling safeguards: destructive scope and feature flag, preview token, state revalidation, fresh backup, idempotency, and serialized execution.
- Keep request and response work bounded.
- Run:
  - `uv run pytest -q`
  - `uv run ruff check .`
  - `uv run pyright`
  - `git diff --check`
- Update public documentation when a public tool contract, configuration variable, compatibility promise, or response shape changes.

## Priority summary

| ID | Priority | Finding | Review lenses | Release blocker |
| --- | --- | --- | --- | --- |
| FSRS-R1 | P0 | Bound rescheduling candidate discovery before materialization | Agent usability, technical, OSS | Yes |
| FSRS-R2 | P0 | Fingerprint ordered FSRS-relevant history and scheduler context | Technical | Yes |
| FSRS-R3 | P0 | Require a newly created backup before destructive execution | Technical | Yes |
| FSRS-R4 | P1 | Reject failed optimizer health checks before mutation | Technical | Yes |
| FSRS-R5 | P1 | Reject non-finite and invalid FSRS parameters before preview | Technical, OSS | Yes |
| FSRS-R6 | P1 | Publish explicit, stable FSRS output contracts | Agent usability | Yes |
| FSRS-R7 | P1 | Make tool descriptions and input semantics self-contained | Agent usability, OSS | Yes |
| FSRS-R8 | P1 | Add an optimization preview/apply workflow | Agent usability | Recommended before release |
| FSRS-R9 | P1 | Make scan-limit and confirmation failures actionable | Agent usability | Recommended before release |
| FSRS-R10 | P1 | Clarify and strengthen idempotency ergonomics | Agent usability | Recommended before release |
| FSRS-R11 | P1 | Complete release-level FSRS test coverage | Technical, OSS | Yes |
| FSRS-R12 | P1 | Correct and expand public FSRS/compatibility documentation | OSS | Yes |
| FSRS-R13 | P2 | Add preview synchronization ergonomics | Agent usability | No |
| FSRS-R14 | P2 | Isolate and type the FSRS compatibility layer | OSS | No |
| FSRS-R15 | P2 | Harden simulation response compatibility checks | OSS | No |
| FSRS-R16 | P2 | Complete OSS package, CI, and contributor metadata | OSS | Yes for public release |

---

## P0: Safety and bounded execution

### FSRS-R1: Bound rescheduling candidate discovery before materialization

**Finding**

`src/anki_mcp/collection.py` currently materializes and sorts every candidate ID across all decks using a preset, then loads cards and queries review history. `MCP_MAX_SEARCH_SCAN` is checked only after an eligible card is appended. A very large set of ineligible candidates can therefore consume unbounded memory and monopolize the collection's single worker without tripping the limit.

**Affected areas**

- `src/anki_mcp/collection.py`: `_fsrs_reschedule_impact()`
- `tests/test_fsrs_operations.py`

**Required direction**

Use bounded candidate discovery, ideally as a batched SQL/backend query that returns at most `max_search_scan + 1` eligible candidates and the state needed for the impact calculation. Avoid an unbounded ID list, global sort, and N+1 review-log queries. Account for deck enumeration in the work budget or bound it separately.

**DoD**

- Candidate discovery stops after proving that the configured maximum was exceeded.
- No unbounded candidate list is constructed before the limit check.
- Review-state/revlog lookup is batched or otherwise demonstrably bounded.
- The error includes the configured maximum and enough scope information to diagnose the preset/decks involved.
- Existing small-collection preview and execution behavior remains unchanged.

**Verification**

- Test with `max_search_scan=1` and multiple review-state candidates without valid review logs; the preview must reject after examining at most `limit + 1` candidates.
- Test exactly-at-limit and one-over-limit cases.
- Test multiple decks sharing one preset.
- Run a bounded performance test/probe showing that work does not scale beyond the configured limit.

### FSRS-R2: Fingerprint ordered FSRS-relevant history and scheduler context

**Finding**

The confirmation fingerprint uses aggregate review-log values. Different ordered review histories can preserve those aggregates while producing different FSRS results. Scheduler day/rollover context is also absent, so a token may remain valid across a day boundary even though native rescheduling can produce different due dates.

**Affected areas**

- `src/anki_mcp/collection.py`: `_fsrs_reschedule_impact()` and fingerprint construction
- `src/anki_mcp/app.py`: guarded mutation revalidation
- `tests/test_fsrs_operations.py`

**Required direction**

Stream-hash the actual ordered review tuples used by native FSRS, including card identity and relevant review fields such as `(cid, id, ease, ivl, lastIvl, factor, time, type)`. Respect `ignore_revlogs_before_date`. Include scheduler day/rollover context and all preset/deck/card settings that influence native rescheduling. Keep hashing bounded under FSRS-R1.

**DoD**

- Aggregate-preserving changes to review order or timestamps change the fingerprint.
- A scheduler day/rollover change invalidates the token when it can affect scheduling.
- Changes before the configured ignore date do not invalidate the token unless native FSRS considers them.
- Deck desired-retention overrides, card eligibility/state, preset parameters, and scheduler options remain covered.
- Fingerprint data is not exposed in a way that leaks card content or review details.

**Verification**

- Regression test that swaps two relevant review timestamps while preserving previous aggregates; fingerprints must differ.
- Test day rollover between preview and execution.
- Tests for review-log changes before and after `ignore_revlogs_before_date`.
- Existing suspension/state-change stale-token tests remain green.

### FSRS-R3: Require a newly created backup before destructive execution

**Finding**

`create_backup()` can fall back to any existing backup when no new file appears, and `backup_before()` checks only that the returned path exists. A failed native backup request can therefore be satisfied by an old backup.

This behavior predates the FSRS feature, but FSRS rescheduling directly relies on it as a destructive-operation prerequisite.

**Affected areas**

- `src/anki_mcp/collection.py`: `create_backup()` and `backup_before()`
- Existing guarded mutation tests plus FSRS integration tests

**Required direction**

For destructive prerequisites, require native success and verify a newly created backup identity after the request starts. Do not let an old backup satisfy `backup_before()`. Explicit backup/status APIs may still report the most recent historical backup separately.

**DoD**

- Destructive mutation proceeds only when native backup creation reports success.
- The verified backup was newly observed after the backup request began.
- The backup path exists and is a non-empty regular file; use native completion confirmation and stronger durability checks where available.
- Backup failure leaves the confirmation/mutation outcome recoverable according to the documented token and idempotency semantics.
- No mutation callback executes on backup failure.

**Verification**

- Tests for native backup returning false, raising, and returning success without creating a new file.
- Test with an old backup present; it must not satisfy the prerequisite.
- Test successful guarded FSRS rescheduling still returns the new backup receipt.

---

## P1: Validation and operation correctness

### FSRS-R4: Reject failed optimizer health checks before mutation

**Finding**

When `health_check=true`, native parameters are currently applied even if `health_check_passed` is false.

**DoD**

- If health checking was requested and fails, optimization returns a safe domain error and does not update the shared preset.
- `health_check=false` behavior is explicitly documented and tested if bypass remains supported.
- No completed mutation receipt claims parameters were applied after a failed health check.

**Verification**

- Mock the native backend to return 21 parameters with `health_check_passed=false`.
- Assert preset parameters remain unchanged and the idempotency record is retry-safe.

### FSRS-R5: Reject non-finite and backend-invalid FSRS parameters before preview

**Finding**

The 21-value parameter schema currently permits `NaN`, positive infinity, and negative infinity. Native rejection occurs only during update, after preview/token consumption and potentially after backup creation.

**DoD**

- Public schema rejects non-finite values.
- Adapter boundary independently rejects non-finite values for defense in depth.
- Apply any stable native FSRS parameter validation available in pinned Anki 26.5 before issuing a confirmation token.
- Invalid values do not produce a confirmation token, consume a token, create a backup, or mutate state.
- Errors identify the invalid parameter position without exposing internal traces.

**Verification**

- Tests for `NaN`, `Infinity`, `-Infinity`, wrong vector length, and representative native-invalid finite values.
- JSON-RPC tests confirm invalid-argument error classification.

### FSRS-R11: Complete release-level FSRS test coverage

**Required coverage**

- All simulation modes: `review`, `workload`, and `optimal_retention`.
- `include_daily=true`, including array lengths and output units.
- Successful rescheduling with a changed 21-value parameter vector.
- Non-default preset behavior and multiple decks sharing a preset.
- Per-deck desired-retention overrides.
- Candidate-scan rejection from FSRS-R1.
- Fingerprint collision/day-rollover cases from FSRS-R2.
- Fresh-backup failures from FSRS-R3.
- Optimizer health-check failure from FSRS-R4.
- Non-finite/native-invalid parameters from FSRS-R5.
- Malformed ignore-date behavior.
- Native interruption, database/backend error classification, partial-failure assumptions, and `outcome_unknown` recovery.
- Sync-induced state change between preview and execute.
- A pinned Anki 26.5 compatibility smoke test for every native backend call used by the public tools.

**DoD**

- Every public FSRS mode and destructive mutation variant has at least one successful protocol-level test and relevant failure tests.
- Tests distinguish native compatibility smoke tests from mocked unit tests.
- CI executes the compatibility tests on the supported Python and Anki versions.
- Full shared completion gates pass.

---

## P1: Agent-facing MCP contracts

### FSRS-R6: Publish explicit, stable result contracts

**Finding**

FSRS tools return `dict[str, Any]`; simulation has three unrelated shapes, and mutation payloads are nested under `result` while reads return direct payloads. Autonomous clients cannot reliably infer branches, fields, or units.

**Required direction**

Introduce explicit result models for optimization, preview, execution, and mutation receipts. Represent simulation as a discriminated union keyed by `mode`. Decide and document whether mutation envelopes are universal or operation-specific.

**DoD**

- MCP output schemas expose stable fields and required/optional branches.
- Simulation uses a `mode` discriminator with mode-specific result schemas.
- Units are encoded in names or descriptions.
- Input/output naming is consistent, including `days_to_simulate` rather than an unexplained `days` alias.
- Workload retention map keys and values are documented.
- JSON-RPC schema snapshot/tests cover every result variant.

### FSRS-R7: Make FSRS tools self-contained and discoverable

**Finding**

Current one-line descriptions omit information agents need for a correct first call.

**DoD**

Each tool and relevant field concisely explains:

- `config_id` is a deck preset ID and can be obtained from `anki_deck_presets_list` or deck options.
- Whether FSRS must be enabled first and how to enable it.
- Default search behavior: explicit search, preset `param_search`, then preset fallback.
- What each simulation mode answers and the units of its inputs/outputs.
- `deck_size` is hypothetical simulation input, not inferred collection size.
- Parameters are a finite 21-value FSRS-6 vector.
- Optimization mutates a shared preset and affects all decks using it.
- Rescheduling requires preview with identical change arguments and an unexpired token.
- Scope and destructive-feature prerequisites.

**Verification**

- Tool-schema tests assert the critical descriptions and constraints.
- A reviewer unfamiliar with the repository can discover IDs and complete each workflow using MCP schemas alone.

### FSRS-R8: Add optimization preview/apply separation

**Finding**

`anki_fsrs_optimize` computes and immediately applies parameters to a shared preset. An agent cannot inspect training count, health result, candidate parameters, search scope, or affected decks before mutation.

**Preferred contract**

- `anki_fsrs_optimize_preview`
- `anki_fsrs_optimize_apply`

An alternative is an explicit `apply` flag that defaults to false, but a separate preview/apply workflow is clearer and aligns with destructive rescheduling.

**DoD**

- Preview is read-only and reports selected search, training count, health result, candidate parameters, current parameters, and affected decks.
- Apply requires an explicit token or equivalent binding to the previewed optimizer inputs/result.
- Apply is idempotent and revalidates relevant state.
- Existing direct mutation behavior is removed, renamed, or retained only with an explicit compatibility/deprecation decision.

### FSRS-R9: Make limit and confirmation errors actionable

**DoD**

- Limit errors use a stable machine-readable code.
- Errors report operation, configured maximum, observed lower bound/count, and remedy.
- Rescheduling errors identify preset/deck scope because callers cannot narrow it with a search parameter.
- Stale-confirmation errors explicitly direct the agent to run preview again.
- Internal environment-variable names may be included, but are not the only remediation guidance.

### FSRS-R10: Clarify idempotency ergonomics

**DoD**

- Every mutating FSRS tool describes `idempotency_key` as stable across retries of the same logical request.
- Decide whether the key is required for FSRS mutations.
- If automatic generation remains, return the generated key prominently and document that a retry after losing the response cannot be deduplicated unless the caller supplied a stable key.
- Tests cover replay, conflict, invalid-input cleanup, and `outcome_unknown` behavior.

### FSRS-R13: Add preview synchronization ergonomics

**Finding**

Reschedule preview does not expose `sync_before`, while execution can synchronize and invalidate an otherwise valid preview.

**DoD**

- Either expose `sync_before` on preview with a documented default, or clearly document the expected re-preview behavior after synchronization.
- Confirmation errors caused by changed synchronized state tell the agent to preview again.
- Add a sync-induced stale-preview test.

---

## P1/P2: OSS publication readiness

### FSRS-R12: Correct and expand public documentation

**Required updates**

- Replace the README clone placeholder `YOUR-USERNAME` with the real publication URL before release.
- Document that `ANKI_ALLOW_DESTRUCTIVE` also controls FSRS preview/rescheduling, not only delete tools.
- Correct the blanket “official APIs” claim: optimization and simulation currently use Anki 26.5 private/generated backend APIs.
- Document FSRS enablement, optimization mutation semantics, simulation modes/units, search defaults, rescheduling preview/execute, backup and confirmation behavior, idempotency, and resource limits.
- Publish an explicit compatibility policy: pinned Anki 26.5 is supported; other versions require validation.
- Document limitations and recovery behavior for backend/sync failures.

**DoD**

- README quick start contains no publication placeholder.
- Public docs describe every FSRS tool sufficiently for first-time MCP use.
- Private API/schema dependencies are disclosed without overstating compatibility.
- Configuration table accurately describes destructive FSRS registration.
- Local Markdown links validate.

### FSRS-R14: Isolate and type the FSRS compatibility layer

**Finding**

`collection.py` exceeds 3,300 lines and now combines collection ownership, sync, persistence, CRUD, analytics, deck configuration, direct SQL, and FSRS orchestration. Compatibility-sensitive FSRS structures use substantial `Any` typing.

**DoD**

- Move native FSRS backend/protobuf/direct-schema compatibility code into a focused module or component.
- Keep collection-thread ownership and mutation coordination explicit.
- Introduce typed result/impact structures and aliases/protocols for preset/config state.
- Centralize Anki 26.5 private API and legacy deck/revlog schema assumptions.
- Preserve public behavior with regression tests; avoid a broad unrelated refactor.

### FSRS-R15: Harden simulation response compatibility checks

**Finding**

Daily response construction assumes native arrays have matching lengths.

**DoD**

- Validate array-length invariants before indexing.
- Raise a clear compatibility/internal error if pinned backend output violates the expected shape.
- Add a mocked malformed-backend-response test.
- Document expected daily output lengths and units.

### FSRS-R16: Complete OSS package, CI, and contributor metadata

**Required updates**

- Add CI for pytest, Ruff, Pyright, package build, and pinned Anki 26.5 compatibility.
- Add changelog/release notes and select an appropriate version bump from `0.2.0` for the public API expansion.
- Add project URLs, issue tracker, authors/maintainers, and useful package classifiers.
- Add basic contributor guidance.
- Resolve or explain the `anki-sync-mcp` branding versus `anki-mcp` package/server naming.

**DoD**

- A clean checkout can run all documented checks.
- CI is green on every supported Python version.
- Package metadata links to real public project resources.
- Release notes enumerate the FSRS tool contracts and compatibility constraints.
- Naming is consistent or explicitly explained.

---

## Deferred/non-blocking strengths to preserve

The reviews agreed that these existing properties are strong and should not regress:

- Coherent FSRS tool namespace.
- Appropriate read/admin/destructive scope classification.
- Destructive feature gating.
- Strict input models and bounded simulator dimensions.
- Compact simulation output by default with opt-in daily data.
- One-time, expiring, operation/request-bound confirmation tokens.
- Serialized collection ownership and mutation execution.
- Durable mutation receipts and conflict detection.
- Parameterized SQL and translated native input/search errors.
- Real temporary Anki collection tests that verify native scheduling changes.
- Exact Anki dependency pinning.

## Suggested implementation order

1. FSRS-R1: bounded candidate discovery and batched impact query.
2. FSRS-R2: ordered-history/day-aware fingerprint on top of the bounded query.
3. FSRS-R3: fresh-backup prerequisite.
4. FSRS-R4 and FSRS-R5: optimizer and parameter validation.
5. FSRS-R11: fill safety/compatibility test gaps for items 1-4.
6. FSRS-R6, R7, R9, and R10: explicit agent-facing schemas, descriptions, and errors.
7. FSRS-R8 and R13: optimization preview/apply and sync ergonomics.
8. FSRS-R12 and R16: public documentation, CI, metadata, and release notes.
9. FSRS-R14 and R15: focused maintainability and compatibility hardening.

A new independent review should inspect the exact final commit after P0/P1 fixes and the full shared completion gates pass.
