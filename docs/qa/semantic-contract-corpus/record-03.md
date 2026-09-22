- `NOT_EXECUTED` — authoritative transport evidence proves a consequential write was rejected before application execution; no automatic replay occurs, and a later retry remains an explicit caller decision;
- `RECOVERABLE` — a write may have happened, but completion/verification is ambiguous; durable evidence exists and blind replay is forbidden;
- `AUTH_REQUIRED` — the invoked route requires valid citizen authentication;
- `RATE_LIMITED` — the invoked route reported rate limiting; this is route evidence, not a forum-wide outage claim;
- `BLOCKED` — a local precondition, transport error, or contract failure cannot safely be classified above.

Transport route/provenance is intentionally not normalized away merely to make two adapters look identical.
