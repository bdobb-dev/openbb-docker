# Security Master API

This service owns the transport-neutral security-master domain contract for
calendar corrections. Its first contract is a pair of versioned temporal
fixtures for NSE Bakri Eid 2023 and DFM Eid Al Fitr 2024.

## Fixture semantics

Each fixture uses schema `security-master.temporal-fixture.v1` and declares
`capture_time_semantics: synthetic_test_capture`. `published_on` records the
historical source date, while `known_from` is a deterministic test-capture
instant rather than a claim about publication precision. Events retain their
source kind, authority, public source URL, and assertion. States retain all
known evidence IDs and are strictly time-ordered.

`state_at(fixture, known_at)` returns the latest state with `known_from <=
known_at`; a cutoff just before a transition returns the preceding state, and
the exact transition instant returns the new state. The loader rejects missing
contract fields, empty events or states, naïve timestamps, non-HTTP(S) source
URLs, unordered state times, and evidence IDs that do not name an event.

## Development

From this directory:

```sh
uv run --extra dev pytest -q
uv run --extra dev ruff check .
```

## Service boundary

The future REST service is the canonical browser API. A future Rita MCP server
will expose a small typed surface but delegate to this same domain resolver or
the REST client. Neither adapter may read MinIO objects directly or duplicate
temporal winning-assertion logic. REST routes, MCP transport, MinIO access,
Delta Lake tables, and SQL execution are intentionally outside this fixture
contract.
