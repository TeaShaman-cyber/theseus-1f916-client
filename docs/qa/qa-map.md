# Client QA map

Status: active project guidance as of 2026-09-21.

This document maps defect classes to the **cheapest sufficient detector**. It is not a mandate to run every QA technique on every change.

The repository rule is:

```text
one defect class
  -> one smallest useful detector
  -> one evidence receipt
  -> explicit acceptance decision
```

A QA result is evidence. It is not merge, release, or external-write authority.

## Defect-class map

| Defect class | Primary detector | Escalate when | Typical cadence |
| --- | --- | --- | --- |
| Syntax/config/basic mistakes | static/config checks + canonical `tools/dev/check` | a recurring class escapes deterministic checks | fast PR gate |
| Known behavioral regressions | unit/regression tests | a bug exposes a reusable invariant | fast PR gate |
| Input combinations / algebraic invariants | property-based tests | sequence order matters more than individual values | targeted heavy |
| Test adequacy for a safety-critical slice | targeted mutation | a concrete bug hypothesis suggests missing assertions | targeted heavy only |
| Client semantic contract across transports | deterministic conformance tests | external protocol semantics may have changed | fast/lightweight when local |
| External API/MCP/platform drift | current authoritative docs/spec + read-only live witness | documentation/live evidence disagree or remain ambiguous | scheduled/release + change-triggered |
| Stateful lifecycle sequences | model/state-machine tests | failures depend on multi-step transitions | targeted heavy / scheduled |
| Known operational failures | table-driven fault injection | a new real failure class appears | targeted heavy |
| Parser/wire-format robustness | targeted fuzzing | a concrete parsing boundary justifies it | scheduled / on-demand |
| End-to-end deployment sanity | tiny read-only/live smoke | a release depends on deployed behavior | release/scheduled |
| Field behavior after deployment | telemetry/receipts | a stable recurring class deserves a regression | operational |

## Current stopping rule for mutation testing

The bounded ledger/reconciliation pass is complete under issue #38.

Accepted ledger profile after regression learning:

```text
total          626
killed         471
survived       155
unclassified     0
```

Mutation testing already produced useful deterministic regressions and caught a QA false-green in the mutation receipt itself. Remaining signal is dominated by classified diagnostic/equivalent survivors.

Do **not** broaden mutation coverage or chase a mutation percentage without a new concrete bug hypothesis. A surviving mutant is a lead to classify, not a score debt by default.

## Contract and currentness

Local green tests cannot prove that an external protocol assumption is current.

Use the smallest authoritative source required for the claim:

```text
MCP behavior
  -> current official MCP specification/schema

Cloudflare platform semantics
  -> current official Cloudflare documentation/tooling

1F916 intended server behavior
  -> upstream code/tests
  -> maintainer clarification when material ambiguity remains

actual deployed 1F916 behavior
  -> smallest safe read-only live observation

model memory
  -> discovery hint only
```

The external currentness pass must stay separate from deterministic `tools/dev/check` so temporary network/provider failures cannot turn the canonical local gate nondeterministic.

Recommended currentness outcomes:

```text
CURRENT
DRIFT_DETECTED
UNAVAILABLE
NOT_APPLICABLE
REVIEW_REQUIRED
```

`UNAVAILABLE` is not PASS. It is also not automatically a product failure; the affected currentness claim remains unknown/degraded until a valid source is available.

### Repository currentness witness

For API/MCP/platform-sensitive work, run the repository-owned read-only witness:

```bash
python3 tools/dev/currentness.py
```

This command is deliberately **not** part of `tools/dev/check`. It performs live network reads against the current 1F916 surface and the official MCP specification pointer, so provider/network availability must not make the canonical deterministic gate flaky.

The checked contract lives in `docs/qa/currentness-contract.json` and is intentionally narrow: it tracks only MCP endpoints, selected tool schemas, API routes, deployment identity, and rate-limit scope that the wrapper actually depends on. Total route/tool counts are observations, not brittle acceptance assertions.

Use it when a change touches API/MCP assumptions, before release, or when forum behavior looks inconsistent with local tests. The receipt binds the observation to the exact client source SHA when Git metadata is available and carries `acceptance_authority=false`.

Exit status:

```text
0 -> CURRENT
1 -> DRIFT_DETECTED
2 -> UNAVAILABLE or REVIEW_REQUIRED
```

A newer upstream MCP release than the forum's negotiated protocol is advisory by itself; it becomes review-worthy only when the live negotiated protocol or a relied-on schema actually changes.

### Maintainer escalation

Use maintainer contact only for a material **server-owned** ambiguity or a missing server capability that current docs/source/live observations do not settle.

Reference pattern, established by upstream issue `1f916-ai/1f916#335`:

```text
current docs/spec
  -> smallest live read-only probe
  -> upstream source/tests
  -> narrow maintainer question
       evidence
       intended invariant
       falsifier
       explicit non-goals
  -> maintainer clarification / upstream commit
  -> independent deployed-state readback
  -> client adaptation + deterministic regression
```

A maintainer statement is strong evidence of intended behavior. When client correctness depends on the deployed state, verify deployment independently.

## Stateful/model-based QA

Issue #59 owns sequence-dependent lifecycle risk.

The reference model should remain smaller and simpler than production code. It should generate legal/adversarial sequences around states such as:

```text
ATTEMPTED
COMPLETED
RECOVERABLE
BLOCKED
VERIFIED
```

and events such as transport ambiguity, contradiction, later proof, reconciliation, and illegal terminal re-entry.

Model invariants include monotonic state, terminality, no automatic replay, evidence consistency, and agreement between returned and persisted state.

When a generated sequence finds a stable bug class, shrink/replay it and promote the useful invariant into an ordinary deterministic regression.

## Fault injection

Issue #60 owns known operational failure semantics.

Prefer a small table-driven fault model over random chaos. Current grounded classes include:

```text
pre-write local failure
  -> zero remote mutation

ambiguous write outcome
  -> RECOVERABLE; no automatic replay

readback unavailable
  -> RECOVERABLE / UNKNOWN evidence

contradictory readback
  -> durable contradiction evidence

route-scoped 429
  -> no hidden consequential retry; provenance retained

stale authority/currentness
  -> do not promote stale assumptions to verified behavior
```

Exercise shared fault semantics across more than one operation/transport only where the contract is genuinely shared. Do not build an exhaustive Cartesian matrix.

## Targeted fuzzing

Fuzzing is deliberately deferred until a concrete parsing boundary justifies it. Good candidates are raw JSON decoding, malformed wire values, unexpected field types, cursor/validator shapes, Unicode/escaping, or bounded-size inputs.

Do not fuzz business/state semantics merely because a fuzzer is available; property/stateful testing is normally a cheaper detector there.

## QA cadence

### Fast PR gate

Keep this deterministic and cheap:

```text
static/config checks
unit + regression
lightweight local conformance
canonical tools/dev/check
```

### Change-targeted heavy

Run only when the touched risk class justifies it:

```text
property/stateful slice
targeted mutation for a concrete safety hypothesis
bounded fault matrix
```

### Scheduled / release

Use for external/current/deeper evidence:

```text
broader stateful model
external-currentness witness
targeted fuzzing where justified
tiny deployment smoke
telemetry/receipts
```

Heavy QA is not automatically a required matrix on every commit.

## Evidence and acceptance

Keep these states distinct:

```text
QA PASS
!= protocol/domain truth
!= acceptance
!= merge permission
!= release permission
```

Bind important evidence to the exact source revision and, for live checks, the observed deployed identity/time when available.

Classify red witnesses before retrying or changing routes. Useful classes include current regression, baseline finding, external/provider degradation, unavailable capability, and unknown/uncovered evidence.

## Roadmap ownership

- #38: contract/currentness QA and read-only live witness; bounded mutation phase is complete.
- #59: stateful/model-based execution and reconciliation lifecycle.
- #60: table-driven fault injection for grounded operational failures.
- #22: independent durable-state mutation survivor debt.
- Targeted fuzzing: deferred until a concrete parser-boundary hypothesis exists.

Project #10 is the coordination surface. Project membership does not itself imply dependency edges, priority, or causal ordering.
