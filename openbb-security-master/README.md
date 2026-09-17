<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
# openbb-security-master

An OpenBB **router** extension: three reference commands that delegate to
[security-master-api](../security-master-api) over HTTP. It opens no Delta
table and no MinIO bucket of its own — the service owns the store, the
temporal context and the receipt, and this package is the shape OpenBB
clients see them in.

## Commands

| Route | What it answers |
| --- | --- |
| `GET /api/v1/reference/market_calendar` | Exchange sessions with holiday, authority and market-effect evidence, under an explicit temporal context |
| `GET /api/v1/reference/exchange_details` | EODHD exchange details (trading hours, holidays) — maintained-calendar input, borrowed from `openbb-eodhd` |
| `GET /api/v1/reference/security_master_resolve` | A ticker, CUSIP, ISIN, FIGI or internal id resolved to listing/instrument/security ids |

The namespace comes from the entry-point **name** (`reference`), because
OpenBB mounts a core extension under `/{entry-point name}`. Note that
`obb.reference` in the *Python* interface is OpenBB's own property (the static
package's reference dict), so these commands are reachable over REST — which
is what the stack and the bdobb-v2 browser use — rather than as
`obb.reference.market_calendar` attributes.

## Temporal context

`as_of` and `knowledge_at` are not filters, they are the question's
as-of-when. The client turns them into the service's `context` block and they
never travel in `parameters`:

| Given | Context sent |
| --- | --- |
| neither | `{"mode": "current_corrected"}` |
| `as_of` | `{"mode": "effective_on", "effective_at": "<as_of>T00:00:00Z"}` |
| `knowledge_at` | `{"mode": "known_at", "known_at": ..., "effective_at": ...}` |

Every answer carries the service's receipt through untouched, in
`OBBject.extra["security_master_receipt"]`.

## Configuration

| Variable | Default |
| --- | --- |
| `SECURITY_MASTER_URL` | `http://security-master-api:6905` |
| `OPENBB_API_USERNAME` / `OPENBB_API_PASSWORD` | empty — the stack's Basic credential, shared with the service |

## The registry copy

`openbb_security_master/odp_registry.json` is a byte-for-byte copy of the
service's registry, and `tests/test_registry_parity.py` fails if the two ever
differ. The models here are typed from it; a field added there and not here
would ride along untyped in `extra` instead of being surfaced.

## Development

```bash
pip install -e '.[dev]'    # openbb-eodhd is a sibling, not a dependency: the image installs it
pytest -q
ruff check .
```

Licensed under the Apache License, Version 2.0.
