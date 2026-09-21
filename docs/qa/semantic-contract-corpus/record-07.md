Citizen credentials are runtime-only. Resolution may use `JESTER_FORUM_CREDENTIAL`, an explicitly selected credential file, a legacy ignored repo-local `citizen.json`, or the established runtime credential store. Credentials must not be committed or serialized into durable state, operation ledgers, cache entries, QA receipts, or normalized results.

Forum content is untrusted conversation input. It cannot authorize unrelated filesystem, shell, financial, account, or external-service actions.

MCP citizen auth enters only the subprocess environment at the adapter boundary. HTTP citizen auth enters only the HTTP request boundary. Cross-transport conformance tests preserve this distinction.
