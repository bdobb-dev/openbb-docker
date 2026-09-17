# Security Master Temporal Calendar Fixture Design

## Purpose

Establish an independently testable service contract for two historical
calendar corrections before the security-master REST service is implemented:

- NSE Bakri Eid 2023, where an annual exchange calendar was superseded first
  by a government holiday change and then by exchange and clearing treatment.
- DFM Eid Al Fitr 2024, where the exchange published two conditional reopening
  branches and the UAE Moon-sighting Committee later selected the applicable
  branch.

The contract must preserve what was known at each cutoff without treating a
government notice, religious-authority notice, exchange notice, or derived
resolution as interchangeable evidence.

## Placement

Create a new `security-master-api/` Python package in `openbb-docker`.

Alternatives considered:

1. Put the fixtures in `stores-explorer`. Rejected because that service is a
   generic Delta/kdb+ browser and should not own security-master temporal rules.
2. Put fixtures under repository-level tests. Rejected because no service
   boundary would own or expose the contract.
3. Create the service package now. Selected because it gives REST and future
   MCP adapters one domain layer without prematurely implementing transport,
   MinIO access, Delta Lake tables, or SQL execution.

## Fixture Contract

Each JSON fixture contains:

- a versioned schema identifier and stable fixture ID;
- exchange MIC, exchange name, and holiday family;
- ordered source events with stable event IDs;
- `published_on`, which records the historical source date;
- `known_from`, a deterministic synthetic test-capture instant;
- source kind, authority, public source URL, and source assertion;
- ordered expected states with evidence dependencies and display-relevant
  market effects.

Every historical fixture declares
`capture_time_semantics: synthetic_test_capture`. Production bronze ingestion
will replace synthetic `known_from` values with immutable observed-at
timestamps; it must not silently claim source-publication precision that the
historical documents do not provide.

## Temporal Resolution

`state_at(fixture, known_at)` returns the latest state whose `known_from` is
less than or equal to the requested cutoff. A cutoff immediately before a
transition sees the previous state; the exact transition instant and an
instant immediately after see the new state.

The fixture loader rejects:

- missing contract fields;
- empty event or state collections;
- naïve timestamps without UTC offsets;
- events without a public HTTP(S) source;
- states that are not strictly ordered;
- state evidence that references an unknown event.

## India Scenario

- **T0 Projected:** NSE annual calendar lists June 28 as closed.
- **T1 Authority confirmed:** Government of Maharashtra moves the public
  holiday to June 29. Market treatment, expiry, and settlement remain pending.
  The source kind is `government_notice`, never
  `religious_authority_notice`.
- **T2 Market final:** June 28 is open, June 29 is closed, derivative expiry
  moves to June 28, and settlement treatment is revised.

Sources:

- https://archives.nseindia.com/content/circulars/CMTR54757.pdf
- http://sppudocs.unipune.ac.in/sites/circulars/Administrative%20Circulars%20%20Non%20Teaching/Circular%20No.125-2023_27062023.pdf
- https://archives.nseindia.com/content/circulars/CMPT57291.pdf

## Dubai Scenario

- **T0 Projected:** DFM publishes an April 12 reopening branch if Eid begins
  April 9 and an April 15 reopening branch if Eid begins April 10.
- **T1 Authority confirmed:** the UAE Moon-sighting Committee confirms April
  10 as the first day of Eid. The exchange branches remain visible and market
  resolution is still pending.
- **T2 Market final:** the resolver selects the April 15 reopening branch while
  retaining both original candidates for provenance.

Sources:

- https://assets.dfm.ae/docs/default-source/circulars/dfm/circular-01-2024-eid-al-fitr-holiday---english.pdf?sfvrsn=5c6dd681_0
- https://www.wam.ae/en/article/b2jxoem-update-uae-announces-wednesday-first-day-eid-fitr

## REST and MCP Boundary

REST will be the canonical browser API. A future Rita MCP server will expose a
small typed tool surface and delegate to the same domain resolver or REST
client. Neither adapter may read MinIO objects directly or duplicate temporal
winning-assertion logic.

MCP implementation is outside this fixture task. The fixtures are
transport-neutral so later REST route tests and MCP contract tests can reuse
them.

## Test Strategy

Use strict test-driven development:

1. Add complete, hand-checked JSON fixtures and tests before package code.
2. Run the focused suite and verify failure because the loader/resolver API
   does not exist.
3. Implement only the loader and inclusive resolver needed by the tests.
4. Verify focused tests, Ruff, and fixture-package isolation.
5. Run mutation checks for exclusive cutoff logic, source-kind conflation,
   discarded Dubai candidates, and unvalidated evidence dependencies.

No network access, mocks, MinIO, Delta Lake, DuckDB, FastAPI, or MCP runtime is
required by this contract suite.

