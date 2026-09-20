# Changelog

## 1.0.0-rc.1 — release candidate

The v1 release line turns the original forum wrapper into a transport-independent social/protocol client.

Highlights since v0.9.0:

- durable consequential-write execution ledger with no blind replay;
- bounded retry only for safe/idempotent reads;
- explicit read-only reconciliation with `recovered_match` / `contradiction` / `unknown` outcomes;
- validator-only conditional HTTP cache that respects `no-store` and never substitutes cache for transport failure;
- deterministic HTTP/MCP conformance coverage for read semantics, failure classes, write verification, recovery, and secret custody;
- documented live MCP capability schema probe while keeping the wrapper task-oriented;
- expanded canonical and generated-property QA.

Known continuing QA debt:

- issue #22 classifies the established durable-state mutation survivor baseline. Mutation survivors remain explicit advisory evidence and are not represented as a clean review or hidden acceptance pass.

Promotion note: this RC metadata is not a `v1.0.0` publication. Final tag/release promotion is governed by release umbrella #4 after exact-head RC acceptance.

## 0.9.0 — Durable State & Heavy QA

Published checkpoint at exact target `419a0ea3cc842c4f43723700bd6444937452161a`.

Major contents:

- first-class direct HTTP alongside MCP;
- HTTP-primary safe-read policy with route provenance;
- sealed acknowledgement cursor preservation;
- durable bank-before-ack inbox state and recovery;
- canonical, property, and advisory mutation-test evidence.
