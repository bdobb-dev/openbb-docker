# Security Master Temporal Calendar Fixtures Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add sourced India and Dubai temporal calendar fixtures plus a minimal inclusive as-of resolver to the security-master service boundary.

**Architecture:** A new dependency-light Python package owns a versioned JSON fixture contract. Its loader validates temporal ordering and provenance, and its resolver selects the latest expected state known at an inclusive cutoff; REST and future MCP adapters remain outside this task and will reuse this domain contract.

**Tech Stack:** Python 3.12+, standard library, pytest, Ruff, uv.

**Spec:** `docs/superpowers/specs/2026-09-16-security-master-calendar-fixtures-design.md`

## Global Constraints

- `published_on` is the historical source date; it must not be presented as a precise capture timestamp.
- Every fixture declares `capture_time_semantics: synthetic_test_capture`.
- India T1 is `government_notice`, not `religious_authority_notice`.
- Dubai retains both original conditional branches after one branch is selected.
- A state becomes visible when `known_from <= known_at`.
- No FastAPI, MinIO, Delta Lake, DuckDB, SQL, network, or MCP dependency is added.

---

### Task 1: Temporal Fixture Contract

**Files:**
- Create: `security-master-api/pyproject.toml`
- Create: `security-master-api/README.md`
- Create: `security-master-api/security_master_api/__init__.py`
- Create: `security-master-api/security_master_api/temporal_fixture.py`
- Create: `security-master-api/tests/fixtures/india_bakri_eid_2023.json`
- Create: `security-master-api/tests/fixtures/dubai_eid_al_fitr_2024.json`
- Create: `security-master-api/tests/test_temporal_calendar_fixtures.py`

**Interfaces:**
- Produces: `load_temporal_fixture(path: str | Path) -> dict[str, Any]`
- Produces: `state_at(fixture: dict[str, Any], known_at: str | datetime) -> dict[str, Any] | None`
- Produces: fixture schema identifier `security-master.temporal-fixture.v1`

- [ ] **Step 1: Add package scaffolding**

Create a setuptools package named `security-master-api`, requiring Python
3.12+, with a `dev` extra containing `pytest` and pinned `ruff==0.15.22`.
Configure pytest with `testpaths = ["tests"]` and `pythonpath = ["."]`.
Create an empty `security_master_api/__init__.py`; do not create
`temporal_fixture.py`.

- [ ] **Step 2: Add the two complete fixtures**

Create the India fixture with these literal transitions:

| Stage | `known_from` | Authority | Market | Required outcome |
|---|---|---|---|---|
| T0 | `2022-12-08T18:00:00Z` | `projected` | `final` | June 28 closed; June 29 open; expiry June 29; settlement original |
| T1 | `2023-06-26T12:00:00Z` | `confirmed_by_government` | `pending` | June 28 previously scheduled closed; June 29 authority holiday pending exchange; expiry and settlement pending |
| T2 | `2023-06-27T12:00:00Z` | `confirmed_by_government` | `final` | June 28 open; June 29 closed; expiry June 28; settlement revised |

Use stable event IDs:

```text
india-t0-exchange-calendar
india-t1-government-notice
india-t2-exchange-clearing-notice
```

Use source kinds `exchange_calendar`, `government_notice`, and
`exchange_clearing_notice`, with these literal URLs:

```text
https://archives.nseindia.com/content/circulars/CMTR54757.pdf
http://sppudocs.unipune.ac.in/sites/circulars/Administrative%20Circulars%20%20Non%20Teaching/Circular%20No.125-2023_27062023.pdf
https://archives.nseindia.com/content/circulars/CMPT57291.pdf
```

Create the Dubai fixture with these literal transitions:

| Stage | `known_from` | Authority | Market | Required outcome |
|---|---|---|---|---|
| T0 | `2024-04-04T12:00:00Z` | `awaiting_moon_sighting` | `conditional` | Two candidates; none selected |
| T1 | `2024-04-08T20:00:00Z` | `confirmed_by_religious_authority` | `pending_resolution` | Two candidates; none selected |
| T2 | `2024-04-08T20:00:05Z` | `confirmed_by_religious_authority` | `final` | Select April 10 condition / April 15 reopen; retain both candidates; April 12 closed and April 15 open |

The two candidate objects are:

```json
[
  {"condition": "eid_first_day=2024-04-09", "reopen_date": "2024-04-12"},
  {"condition": "eid_first_day=2024-04-10", "reopen_date": "2024-04-15"}
]
```

Use stable event IDs:

```text
dubai-t0-conditional-exchange-circular
dubai-t1-moon-sighting-confirmation
dubai-t2-derived-branch-selection
```

Use source kinds `conditional_exchange_notice`,
`religious_authority_notice`, and `derived_calendar_resolution`, with these
literal source URLs:

```text
https://assets.dfm.ae/docs/default-source/circulars/dfm/circular-01-2024-eid-al-fitr-holiday---english.pdf?sfvrsn=5c6dd681_0
https://www.wam.ae/en/article/b2jxoem-update-uae-announces-wednesday-first-day-eid-fitr
```

Every expected state lists all evidence event IDs known by that stage.

- [ ] **Step 3: Write the failing tests**

Write tests that import `load_temporal_fixture` and `state_at` from
`security_master_api.temporal_fixture` and assert:

```python
assert state_at(fixture, cutoff - timedelta(microseconds=1)) is previous_state
assert state_at(fixture, cutoff)["stage"] == expected["stage"]
assert state_at(fixture, cutoff + timedelta(microseconds=1))["stage"] == expected["stage"]
```

Use literal assertions for:

```python
assert india_t1["authority_status"] == "confirmed_by_government"
assert india_t1["market_status"] == "pending"
assert india_t2["market_dates"] == [
    {"date": "2023-06-28", "session": "open"},
    {"date": "2023-06-29", "session": "closed"},
]
assert india_t2["expiry_date"] == "2023-06-28"
assert india_t2["settlement_status"] == "revised"

assert dubai_t0["selected_branch"] is None
assert len(dubai_t0["candidate_branches"]) == 2
assert dubai_t1["market_status"] == "pending_resolution"
assert dubai_t2["selected_branch"] == {
    "condition": "eid_first_day=2024-04-10",
    "reopen_date": "2024-04-15",
}
assert len(dubai_t2["candidate_branches"]) == 2
```

Add validation tests using hand-written temporary fixture objects for missing
required fields, a naïve timestamp, a non-HTTP source URL, duplicate or
descending state times, and an unknown evidence event ID.

- [ ] **Step 4: Run the focused tests and verify RED**

Run:

```sh
cd security-master-api
uv run --extra dev pytest -q
```

Expected: collection fails with
`ModuleNotFoundError: No module named 'security_master_api.temporal_fixture'`.
Any other failure must be corrected until the test fails for this reason.

- [ ] **Step 5: Implement the minimal loader and resolver**

Create `_instant(value: str) -> datetime` using
`datetime.fromisoformat(value.replace("Z", "+00:00"))` and reject naïve
datetimes.

`load_temporal_fixture` must:

```python
fixture = json.loads(Path(path).read_text(encoding="utf-8"))
```

Then validate the required top-level fields, synthetic capture semantics,
non-empty events and states, each event timestamp/date/source URL, strictly
increasing state timestamps, and evidence membership.

`state_at` must iterate the already ordered expected states and update the
winner only while:

```python
_instant(candidate["known_from"]) <= cutoff
```

Return `None` when no state is yet known. Re-export both functions from
`security_master_api.__init__`.

- [ ] **Step 6: Verify GREEN**

Run:

```sh
cd security-master-api
uv run --extra dev pytest -q
uv run --extra dev ruff check .
```

Expected: all fixture tests pass and Ruff reports no errors.

- [ ] **Step 7: Perform mutation checks**

Temporarily change `<=` to `<`; the exact-cutoff test must fail. Restore it.
Temporarily remove the unknown-evidence validation; its validation test must
fail. Restore it. Temporarily remove the unselected Dubai branch from T2; the
literal candidate-count assertion must fail. Restore it.

Run the GREEN commands again after restoring each mutation.

- [ ] **Step 8: Document and commit**

Document the fixture semantics, service ownership, test commands, and
REST/MCP shared-domain boundary in `security-master-api/README.md`.

Run:

```sh
git diff --check
git add security-master-api
git commit -m "test: add temporal holiday correction fixtures"
```
