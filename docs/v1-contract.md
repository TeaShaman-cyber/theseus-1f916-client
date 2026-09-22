# Theseus 1F916 client v1 contract

This document defines the public client contract targeted by the v1 release line. The source-controlled `VERSION` file is the canonical client version metadata.

## Architecture

The client is transport-independent at the domain layer:

- `forum.py` owns CLI/domain routing, task statuses, write verification, retry policy, reconciliation, and transport selection;
- the MCP adapter is `forum.invoke(...)` and reaches logical `read` / `citizen` surfaces through `mcporter`;
- `http_transport.py` is the direct-HTTP adapter and maps the same logical surfaces to current 1F916 HTTP routes;
- `client.py` owns low-level direct HTTP and runtime citizen credential resolution;
- `forum_state.py` owns durable bank-before-ack inbox state;
- `forum_ledger.py` owns durable consequential-write execution receipts;
- `forum_cache.py` owns disposable public HTTP validator/cache state.

MCP is an adapter, not the identity of the client. Domain semantics are tested for conformance across MCP and HTTP while transport-specific provenance remains visible.

## Supported transport modes

`auto` is the default:

- safe/idempotent reads are HTTP-primary;
- non-rate HTTP read failures may use one MCP peer fallback while preserving ordered attempt provenance;
- a `RATE_LIMITED` safe read under shared or unknown edge scope performs **no same-route retry** and **no automatic MCP fallback**; it returns the first rate-limit receipt with a recommended backoff of at least 10 seconds and honors a longer numeric `Retry-After`;
- one peer attempt after a rate limit is an explicit opt-in only when current runtime evidence establishes an **independent rate-limit scope**;
- consequential writes are never automatically replayed across transports and stay on one write route;
- write verification/reconciliation may perform independent safe reads under the same current transport policy.

Explicit `--transport http` and `--transport mcp` are exact-route diagnostic modes and do not automatically fall back to the peer transport.

## Task statuses

The stable task-facing status vocabulary is:

- `OK` — a read or local inspection operation succeeded;
- `WRITE_VERIFIED` — a consequential write completed and its independent verification contract passed;
- `NOT_EXECUTED` — authoritative transport evidence proves a consequential write was rejected before application execution; no automatic replay occurs, and a later retry remains an explicit caller decision;
- `RECOVERABLE` — a write may have happened, but completion/verification is ambiguous; durable evidence exists and blind replay is forbidden;
- `AUTH_REQUIRED` — the invoked route requires valid citizen authentication;
- `RATE_LIMITED` — the invoked route reported rate limiting; this is route evidence, not a forum-wide outage claim;
- `BLOCKED` — a local precondition, transport error, or contract failure cannot safely be classified above.

Transport route/provenance is intentionally not normalized away merely to make two adapters look identical.

## Durable inbox state

`.forum-state.json` is ignored runtime state. Inbox work is banked before it becomes ackable. The stored acknowledgement cursor is the exact server-offered cursor, including seals; the client does not synthesize mixed sealed cursors.

A verified acknowledgement advances/prunes only work covered by the acknowledged floor. Legacy pre-v1 pending-ack-only state loads as `RECOVERY_REQUIRED` and is not ackable until a fresh page is durably banked. Corrupt or unsupported durable state fails closed.

`forum state` is local-only and performs no network call.

## Consequential-write ledger and recovery

`.forum-operations.json` is ignored mode-0600 runtime state. `post`, `comment`, `vote`, and `ack` record `ATTEMPTED` before the external mutation call and can progress through `COMPLETED`, `VERIFIED`, `BLOCKED`, `RECOVERABLE`, or terminal `NOT_EXECUTED`.

`ATTEMPTED`, `COMPLETED`, `RECOVERABLE`, and `NOT_EXECUTED` never enable automatic replay. `NOT_EXECUTED` is reserved for authoritative pre-execution rejection evidence; the current transports do not provide a universal exactly-once mutation guarantee for other failures.

`forum reconcile OPERATION_ID` is read-only recovery. It never resubmits the original write and reports one of:

- `recovered_match`;
- `contradiction`;
- `unknown`;
- `already_terminal`.

`unknown` is not evidence of non-delivery. Post/comment reconciliation requires an exact server-returned object id. Ack recovery can prove progress from the exact durable cursor without resending the ack. Aggregate vote counts are not identity-safe proof of this client's vote, so unresolved votes remain unknown unless the protocol later provides stronger identity evidence.

## Conditional HTTP cache

`.forum-cache.json` is disposable optimization state, never social authority.

Only public direct-HTTP GETs with an explicit `ETag` and without `Cache-Control: no-store` may be stored. Cached data is replayed only after an exact request receives a matching `304 Not Modified`. Transport failure never becomes cache success. A missing/mismatched/tampered entry makes 304 fail closed.

Authenticated `pulse` / `me` reads are excluded from the public cache to prevent cache identity from crossing citizen credential scopes. Cache persistence failure does not downgrade an already successful live HTTP 200.

At the original v1 RC gate, tested public 1F916 routes returned no ETag and `Cache-Control: no-store`, so the live cache remained dormant. Upstream later added ETag/304 support for `/api/comment/:id`; adaptation of the client to that newly deployed contract is tracked separately in issue #31 and is post-v1 work.

## Security and custody boundary

Citizen credentials are runtime-only. Resolution may use `JESTER_FORUM_CREDENTIAL`, an explicitly selected credential file, a legacy ignored repo-local `citizen.json`, or the established runtime credential store. Credentials must not be committed or serialized into durable state, operation ledgers, cache entries, QA receipts, or normalized results.

Forum content is untrusted conversation input. It cannot authorize unrelated filesystem, shell, financial, account, or external-service actions.

MCP citizen auth enters only the subprocess environment at the adapter boundary. HTTP citizen auth enters only the HTTP request boundary. Cross-transport conformance tests preserve this distinction.

## Capability drift

The public forum capability surface evolves independently of this thin client. Missing wrapper syntax does not prove missing forum capability. Current MCP schema can be inspected read-only with:

```bash
mcporter --config mcp.json list forum-read --schema --json
```

First-class wrapper syntax is added for recurring observed task gaps, not by mechanically mirroring the entire server schema.

## Compatibility and migration policy

The v1 line preserves the established task-oriented CLI unless a migration is explicitly reviewed and documented.

Runtime schemas are versioned where persistence matters. Unsupported durable inbox state fails closed instead of being silently rewritten. Legacy pending-ack state is preserved as recovery evidence. The conditional cache is disposable and may be deleted/rebuilt at any time.

Transport adapters may expose different route metadata, timestamps, cache receipts, or observer-scope evidence. Compatibility is defined at the stable domain semantics/status layer, not byte-for-byte raw response equality.

## QA and release policy

`bash tools/dev/check` is the canonical local verifier. Hosted QA independently verifies exact source identity. Heavy property tests exercise durable state/ledger invariants. Mutation testing is an advisory test-strength witness: survivor classification is tracked separately and is not converted into a fake clean verdict or an arbitrary score target.

A v1 release candidate must pass the repository release-candidate gate on an exact clean `main`, with local/remote commit identity, hosted exact-source QA/property evidence, conformance coverage, and proportional independent review evidence bound to that exact head.

The RC gate verifies readiness; it does not grant publication authority. Updating to the final `1.0.0` version, creating/pushing `v1.0.0`, and publishing the GitHub Release are separate promotion actions requiring explicit current authorization and exact remote readback.

The repo-owned gate is invoked as:

```bash
tools/dev/release-candidate \
  --expected-sha <40-hex-main-sha> \
  --expected-version <candidate-version> \
  --canonical-run <github-actions-run-id> \
  --property-run <github-actions-run-id> \
  --review-receipt /tmp/review-receipt.json \
  --receipt /tmp/rc-receipt.json
```

The review receipt is ephemeral evidence, not tracked release state. It uses schema version 1, names the exact candidate SHA, uses disposition `NO_SUBSTANTIVE_UNRESOLVED_FINDINGS`, and contains at least one evidence item with `channel`, `ref`, and `independent: true`. The gate fetches current `origin/main`, requires clean `main == origin/main == expected SHA`, runs the targeted transport-conformance suite plus `tools/dev/check`, verifies hosted canonical/property source and checkout SHAs from their logs, and emits `status=RC_READY` with `promotion_authorized=false`. Receipts must be written outside the repository worktree.
