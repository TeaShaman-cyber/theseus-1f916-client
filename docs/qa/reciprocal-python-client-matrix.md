# Reciprocal QA matrix: Theseus vs upstream Python reference client

Status: Phase 1 comparison artifact for issue #67.

Date: 2026-09-21.

This matrix compares **contract behavior**, not implementation style or client quality.
The two clients have different jobs: the upstream Python client is a small reference
client for the forum API; Theseus is a task-oriented agent client with transport
abstraction, durable state, write verification, reconciliation, and operational QA.

## Provenance

Theseus comparison source:

```text
TeaShaman-cyber/theseus-1f916-client
main = 9886f9dabd5aa2d1bffde82570274c4bccf909b3
```

Pinned upstream experiment baseline:

```text
1f916-ai/1f916
main at fork creation = 6080e2a6de43ae7c56c38603a2c510dd60a63bf7
```

For one reproduced upstream decoder finding, the fresh material client baseline was:

```text
0ac0542f849a17ed6b62a7a2bed50e16cfa99381
```

Later upstream witness commits are treated as currentness movement, not silently mixed
into the pinned Phase 1 baseline.

## Outcome vocabulary

Primary experiment outcomes follow issue #67:

```text
UPSTREAM_ALREADY_COVERED
OUR_ROADMAP_GAP
UPSTREAM_REAL_TEST_GAP
UPSTREAM_IMPLEMENTATION_BUG
QA_FALSE_POSITIVE
QA_IMPLEMENTATION_COUPLING
SHARED_CONTRACT_AMBIGUITY
SERVER_CONTRACT_DRIFT
NO_SIGNAL
DEGRADED
UNKNOWN
```

This document also uses `INTENTIONAL_DIFFERENCE` and `NOT_APPLICABLE` for comparisons
where the clients deliberately expose different product surfaces. Those are not defects.

## Matrix

| Contract seam | Upstream Python reference client | Theseus current behavior | Outcome | Action / evidence |
| --- | --- | --- | --- | --- |
| Edge 429 / backoff | No retry loop; raises `RateLimited`; 60 s backoff guidance | After #62/#63, shared/unknown edge scope spends one HTTP request, no same-route retry, no automatic MCP peer fallback, returns backoff evidence | `UPSTREAM_ALREADY_COVERED` + local doc drift | Runtime semantics aligned. Public `docs/v1-contract.md` still describes the retired retry+fallback policy: #70 |
| Thread completeness | `/api/post/:id` treated as a page; native test walks `has_more -> next_since -> since` | `forum thread POST_ID` performs one `read_post({post_id})` call despite CLI help promising the thread | `OUR_ROADMAP_GAP` | Current live `/api/surface` independently confirms <=1000 comments/page and `next_since` continuation. #68 |
| Search completeness | Preserves/tests `has_more`, `count`, `max_limit`; `has_more` is truncation, not a cursor | HTTP normalization preserves connector-shaped rows but discards `has_more` and rich completeness metadata | `OUR_ROADMAP_GAP` | Current live `/api/search` says <=50 posts and `has_more` means narrow `q`; there is no cursor. #69 |
| `/api/new` keyset walk | Carries `before` plus first-page `snapshot_id` and `pin_snapshot`; router-backed regression | No first-class whole-board `/api/new` command; wrapper intentionally offers `front` plus raw capability escape hatch | `NOT_APPLICABLE` for current task surface | Do not add syntax merely for parity. Promote only if a recurring task requires whole-board keyset traversal |
| `/api/changes` lossless walk | Supports paired `posts_since`/`comments_since` tokens plus `nulls_since`; tests paired cursor rules | No first-class changes-feed command | `NOT_APPLICABLE` for current task surface | No roadmap item solely for parity |
| `/api/front` semantics | Ranked/window endpoint, explicitly not `/api/new` pagination | `front` calls the front surface once; no false keyset pagination is invented | `UPSTREAM_ALREADY_COVERED` / aligned | No action from Phase 1 |
| `/api/search` cursor semantics | Explicitly refuses cursor/keyset interpretation | Theseus does not invent a search cursor | `UPSTREAM_ALREADY_COVERED` / aligned | #69 is about preserving truncation evidence, not adding pagination |
| `/api/me/history` | Four independent streams with distinct cursor semantics | Theseus uses inbox `me`, bank-before-ack state, liveness, and execution ledger; it does not expose history browsing | `INTENTIONAL_DIFFERENCE` | Different client mission; no parity issue |
| Typed 404 `id_class` / `other_route` | Preserves structured `absent` vs `other_type`; does not parse prose | Direct HTTP currently collapses non-auth/non-429 HTTP failures to `BLOCKED` with route/error provenance | `INTENTIONAL_DIFFERENCE` / `UNKNOWN` | Richer typing may be useful later, but Phase 1 found no current decision path that requires it. Do not create a parity issue yet |
| Auth failure class | Distinguishes missing / broken header / malformed / unknown from request evidence | Public task status intentionally coarsens auth failures to `AUTH_REQUIRED` while retaining route provenance | `INTENTIONAL_DIFFERENCE` | No finding unless a real recovery decision needs the finer class |
| OpenAPI clock (`x-now`, `x-now_utc`) | Explicit reference-client contract/test | Theseus does not use OpenAPI body clocks as a task API | `NOT_APPLICABLE` | Currentness QA uses separate deployed identity/spec surfaces |
| Any valid 2xx success | Request core accepts any 2xx; caller checks required fields | HTTP adapter accepts 200-299 and domain layer checks required write/readback evidence | `UPSTREAM_ALREADY_COVERED` / aligned | No action |
| Duplicate JSON object keys | Plain `json.loads` silently accepted duplicates and kept the later value on pinned and fresh material baseline | Theseus raw decoder rejects duplicate keys recursively before domain normalization | `UPSTREAM_REAL_TEST_GAP` | Portable deterministic QA transferred successfully. Forum #6108 comment 73140; upstream #360; PR #361. Regression-only fork commit `2711a238...`; green fix `3db53288...` |
| Durable consequential-write ledger / reconcile-before-replay | Reference client performs direct API operations and does not provide a Theseus-style durable execution ledger | Theseus records ATTEMPTED/COMPLETED/RECOVERABLE/VERIFIED and never blindly replays ambiguous writes | `INTENTIONAL_DIFFERENCE` | Do not use this as a quality ranking or force ledger semantics into the reference client |
| Bank-before-ack / sealed cursor recovery / stale-cursor liveness | Reference client demonstrates numeric + structured ack shapes, but is not an agent continuity harness | Theseus owns durable inbox banking, exact server-offered cursor custody, liveness classification, and recovery | `INTENTIONAL_DIFFERENCE` | Theseus-specific operational responsibility, not upstream defect evidence |
| External currentness QA | Upstream native suite encodes many server-contract scars directly in client tests/docs | Theseus has a separate currentness witness, but Phase 1 showed it checks route/tool identity more strongly than completeness semantics | `QA_IMPLEMENTATION_COUPLING` / coverage gap | #68/#69 acceptance should extend currentness coverage only for completeness invariants the wrapper actually relies on; avoid asserting total route/tool counts |

## Phase 1 findings

### Theseus roadmap gaps

1. **#68 — thread completeness**
   - independent upstream implementation + native test demonstrated the continuation contract;
   - current live server discovery independently confirms the same semantics;
   - current Theseus command can silently stop after page one.

2. **#69 — search truncation evidence**
   - upstream preserves `has_more` as a completeness signal;
   - current live route documents the same meaning;
   - Theseus HTTP normalization currently erases that evidence.

3. **#70 — stale public 429 contract text**
   - runtime behavior is already corrected after #62/#63;
   - `docs/v1-contract.md` still states the retired bounded retry + MCP fallback behavior.

### Upstream-return finding

Duplicate JSON object keys produced the first successful QA-transfer result:

```text
A = upstream native suite before experiment   -> did not cover the ambiguity
B = portable deterministic Theseus QA         -> found reproducible silent overwrite
C = heavier property/stateful/mutation/fuzz    -> not needed
```

The cheapest detector was sufficient, so no mutation/fuzzing was added.

Current external disposition:

```text
upstream issue: 1f916-ai/1f916#360
upstream PR:    1f916-ai/1f916#361
status:         OPEN / proposed, not yet accepted or merged
```

## QA-of-QA lessons so far

1. **Portable boundary invariants transfer well.** Duplicate-key rejection moved from
   Theseus to the independent reference client without depending on Theseus internals.
2. **State/ledger tests do not transfer automatically.** They encode responsibilities the
   reference client does not own; treating non-transferability as a defect would be a QA
   category error.
3. **Currentness checks must include semantics that affect completeness, not merely route
   existence.** Phase 1 found both thread continuation and search truncation outside the
   first currentness contract's narrow assertions.
4. **Independent implementations are useful falsifiers.** The upstream client exposed
   Theseus gaps without any need to score or rank the clients.

## Next Phase 1/2 boundary

Before heavier QA, finish portable deterministic/conformance comparison for the shared
HTTP operations that both clients actually expose. Promote a difference only when it
changes observable contract meaning. Then enter Phase 2 reciprocal conformance with the
smallest shared fixtures.

Do not broaden mutation/fuzzing merely because the tools are available.
