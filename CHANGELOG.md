# Changelog

## Unreleased

- expose server ETag evidence on `no-store` public comment reads without creating a persistent body cache;
- add explicit caller-held `If-None-Match` revalidation for public GETs, returning bodyless `NOT_MODIFIED` only on an authoritative live 304.

## 1.0.0 — stable release

Promoted from the verified `1.0.0-rc.1` release candidate after the repo-owned exact-head gate returned `RC_READY` and explicit release authority was granted under umbrella #4.

The stable v1 contract includes:

- transport-independent domain semantics with first-class HTTP and MCP adapters;
- durable bank-before-ack inbox recovery;
- consequential-write execution ledger with no blind replay;
- bounded retry only for safe reads;
- read-only reconciliation of ambiguous writes;
- validator-only conditional HTTP cache with fail-closed freshness semantics;
- deterministic cross-transport conformance coverage;
- canonical, generated-property, and advisory mutation-test evidence;
- exact-head release-candidate gate separating readiness evidence from promotion authority.

Known continuing advisory QA debt remains tracked in issue #22. The upstream conditional-revalidation adaptation watch remains tracked in #31 and is intentionally post-v1 work rather than a late release blocker.

## 1.0.0-rc.1 — release candidate

The v1 release line turns the original forum wrapper into a transport-independent social/protocol client.

Highlights since v0.9.0:

- durable consequential-write execution ledger with no blind replay;
- bounded retry only for safe/idempotent reads;
- explicit read-only reconciliation with `recovered_match` / `contradiction` / `unknown` outcomes;
- validator-only conditional HTTP cache that respects `no-store` and never substitutes cache for transport failure;
- deterministic HTTP/MCP conformance coverage for read semantics, failure classes, write verification, recovery, and secret custody;
- documented live MCP capability schema probe while keeping the wrapper task-oriented;
- expanded canonical and generated-property QA;
- repo-owned exact-head release-candidate gate binding clean `main`, local QA/conformance, hosted source receipts, and independent review evidence without granting promotion authority.

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
