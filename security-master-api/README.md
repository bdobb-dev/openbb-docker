# Security Master API

This package is the service boundary for the temporal security master. The
initial contract suite contains two sourced calendar-correction scenarios:

- NSE Bakri Eid 2023: annual calendar, government correction, then final
  exchange and clearing treatment.
- DFM Eid Al Fitr 2024: conditional exchange notice, moon-sighting authority
  confirmation, then deterministic branch selection.

The fixtures keep `published_on` separate from `known_from`. Historical sources
provide the publication date; deterministic test instants use
`capture_time_semantics: synthetic_test_capture`. Production bronze ingestion
will replace those synthetic instants with immutable observed-at timestamps.

REST endpoints and any future MCP tools for Rita must call the same temporal
resolver and satisfy these fixtures. Neither adapter should infer directly from
MinIO objects or reimplement winning-assertion logic.

Run the package checks with:

```sh
uv run --extra dev pytest -q
uv run --extra dev ruff check .
```

