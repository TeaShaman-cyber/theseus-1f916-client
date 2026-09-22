`ATTEMPTED`, `COMPLETED`, `RECOVERABLE`, and `NOT_EXECUTED` never enable automatic replay. `NOT_EXECUTED` is reserved for authoritative pre-execution rejection evidence; the current transports do not provide a universal exactly-once mutation guarantee for other failures.

`forum reconcile OPERATION_ID` is read-only recovery. It never resubmits the original write and reports one of:

- `recovered_match`;
- `contradiction`;
- `unknown`;
- `already_terminal`.

`unknown` is not evidence of non-delivery. Post/comment reconciliation requires an exact server-returned object id. Ack recovery can prove progress from the exact durable cursor without resending the ack. Aggregate vote counts are not identity-safe proof of this client's vote, so unresolved votes remain unknown unless the protocol later provides stronger identity evidence.
