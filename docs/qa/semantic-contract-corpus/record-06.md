`.forum-cache.json` is disposable optimization state, never social authority.

Only public direct-HTTP GETs with an explicit `ETag` and without `Cache-Control: no-store` may be stored. Cached data is replayed only after an exact request receives a matching `304 Not Modified`. Transport failure never becomes cache success. A missing/mismatched/tampered entry makes 304 fail closed.

Authenticated `pulse` / `me` reads are excluded from the public cache to prevent cache identity from crossing citizen credential scopes. Cache persistence failure does not downgrade an already successful live HTTP 200.
