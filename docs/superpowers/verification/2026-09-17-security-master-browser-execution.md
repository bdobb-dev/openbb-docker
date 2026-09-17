<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Security Master Browser — execution record (openbb-docker half)

Branch `claude/security-master-api`, cut from `origin/release/v11.2.x`. Covers
Task 23's openbb-docker gates, verification, push and PR (Part A of the
Security Master Browser plan). Part B (bdobb-v2 renderer) is recorded
separately on `release/v11`.

## Branch and commits

`git log --oneline origin/release/v11.2.x..HEAD` (40 commits, HEAD `adac755`):

```
adac755 docs(security-master): the two remaining spec lines name the shipped entry points
ffb8e7d docs(security-master): spec names the shipped entry points and fixture path
fe0e67d fix(security-master): registry-declared session labels; knowledge-filtered interruptions; bounded cursors; catalog once per request
deb80d4 fix(openbb-security-master): unwrap the EODHD credential; typed temporal parameters; guarded response shapes
bfc67e2 feat(openbb-security-master): the reference router extension over security-master-api
296e046 docs(security-master): the env example documents the worker poll interval and matches the budget default
b08f871 docs(security-master): scrub NAS paths from the spec and plan
fe6fe0d feat(security-master): image, compose services, Serve routes, CI jobs and README
e09ffa2 fix(security-master): a malformed provider body is validation_failed; only an empty list is partial
181b360 fix(security-master): atomic promotion, honest capture hashes, one call per listing, lookup jobs refused
24b4d1a feat(security-master): the acquisition worker over openbb-api with Bronze retention and promotion
2fb400d fix(security-master): ops.jobs carries the summary; stable event order; refresh preflights the whole range; async SSE
74e07ed fix(security-master): deterministic claim order, and malformed preflight requests answer as refusals
cbea4d3 feat(security-master): durable Delta-backed jobs, signed preflight and the acquisition routes
7d28f63 fix(security-master): query credential scoped to SSE; enveloped 500s; catalog built once per request; reproducible contract fixtures
e705954 feat(security-master): the FastAPI service with auth, error envelope and read-only routes
321aec2 fix(security-master): scan budget releases only what it acquired; ILIKE filters escape wildcards
5c20f40 feat(security-master): executions with opaque cursors, budgets and the structured preview builder
4b5e083 fix(security-master): ODP receipts name identity relations; ambiguity refused; MarketCalendar parameters honoured
a7f4065 test(security-master): cover the EquitySearch query and the include_closed bind
5eaf8eb feat(security-master): ODP registry and projections, calendar adapter, receipts
0f65376 fix(security-master): deterministic compare with effective-time classes; strict identity resolution
085c3b8 refactor(security-master): resolve reports "known" from the same filtered lookup
aef59c2 feat(security-master): identity resolution, lineage chains and temporal comparison
40a1e37 fix(security-master): open sessions, package source_kind, explicit effective-filter exemptions
f017bc3 feat(security-master): context-bound Gold views proven on the seven golden fixtures
70e96f5 fix(security-master): window functions obey the allowlist; cancel and timeout stay distinct; bind errors are QUERY_REJECTED
6b29de1 feat(security-master): isolated DuckDB sessions and the AST-level SQL policy
758f1cc feat(security-master): temporal context, catalog and dependency manifest
dd95241 fix(security-master): India T0 holds a holiday row and a separate market-session row
1644955 feat(security-master): the seven golden fixtures and the seed command
1edb8dc fix(security-master): store contract — reject unknown relations, UTC history timestamps
267565f feat(security-master): relation schemas and append-only Delta tables
9772dce feat(security-master): settings, domain errors and the service pyproject
60cf929 docs(plan): the Security Master Browser implementation plan, 23 tasks across both repositories
d29e95a docs(design): the Security Master Browser service, extension and renderer
fe9ad48 fix: validate temporal fixture evidence causality
87e49e8 test: add temporal holiday correction fixtures
46274a6 docs: plan temporal calendar fixtures
ef0afce docs: design temporal calendar fixtures
```

## Gates

All commands run from the openbb-docker worktree root
(`claude/security-master-api` at `adac755`), host under heavy load
(`uptime` reported load averages 86.29 / 63.07 / 67.68; no `OSError` fixture
retries were needed).

| # | Command | Result |
|---|---|---|
| 1 | `(cd security-master-api && uv run --extra dev pytest -q && uv run --extra dev ruff check .)` | `238 passed, 4 warnings in 276.75s (0:04:36)`; ruff: `All checks passed!` |
| 1a | `git status --porcelain` (after gate 1) | empty — the contract-fixture re-record (`tests/contract/*.json`) reproduced byte-identical, tree stayed clean |
| 2 | `python3 -m venv /tmp/sm-ext && . /tmp/sm-ext/bin/activate && pip install -q -e ./openbb-deltalake -e ./security-master-api -e './openbb-security-master[dev]' && (cd openbb-security-master && pytest -q && ruff check .) && deactivate && rm -rf /tmp/sm-ext` | `14 passed, 5 warnings in 22.13s`; ruff: `All checks passed!`; `/tmp/sm-ext` removed on exit |
| 3 | `python3 -m pytest -q tests/test_compose_contract.py tests/test_api_app_auth.py` | `16 passed, 1 warning in 0.44s` (deps `pyyaml pytest fastapi httpx` were already present, no install needed) |
| 4 | `bash scripts/scrub-check.sh` | `Scrub check passed.` (exit 0) |
| 4 | `bash scripts/check-serve-config.sh` | `serve configs: valid JSON, Services declared` (exit 0) |
| 5 | `docker info` | Client present (Docker Desktop v29.7.2); Server section: `ERROR: Error response from daemon: Docker Desktop is unable to start` — daemon unavailable, as expected |

Warnings across gates 1–3 are pre-existing upstream deprecations (Starlette
`TestClient`/httpx2, anyio `BlockingPortal` alias, `openbb_core` aiohttp
`ClientSession` subclassing, Pydantic `@model_validator` on a classmethod,
`websockets.legacy`) — none originate in this branch's code and none were
treated as failures.

## Not verified

- **Docker image builds.** Docker Desktop's daemon could not start in this
  environment (`docker info` above), so neither
  `docker build -f security-master-api/Dockerfile -t openbb-security-master:11.4.0 .`
  nor the root `docker build -t openbb-local:ci .` (with the router assertion)
  was run. Per Task 13's own record, the image build was already unverified
  locally through Part A and rests on the release workflow / a manual build
  before any tag — this record does not change that. Both builds must run at
  least once before `openbb-security-master:11.4.0` or `openbb-local:11.4.0`
  is tagged or pushed to the NAS.
- **NAS deployment.** No compose file, Serve config, or service on the NAS
  was touched. The deployment procedure below is documentation only, to be
  executed on the user's explicit go after 4 PM ET, per the plan.

## Rulings

Copied verbatim from `.superpowers/sdd/2026-09-17-security-master-browser/progress.md`:

- Ruling: T3 seeds one `silver.exchanges` row per calendar (cal_xnse, cal_xdfm, cal_tadawul, cal_xhkg, cal_xtae) — the Gold view LEFT JOINs exchanges — cost if wrong: a missing timezone/name column in calendar rows
- Ruling: T5 implements `list | dict | None` from the start — cost if wrong: none
- Ruling: T3 writes `row_date` on every MarketCalendar expectation now — cost if wrong: T8 test edits
- Ruling: initialize `receipt = None`, guard the finally — cost if wrong: none
- Ruling: preflight identities carry `issuer_id` from the candidate row (`gold.security_master.issuer_id`) — cost if wrong: shares facts keyed by listing
- Ruling: use 11.4.0 as the next minor on the line; the tag is not cut here — cost if wrong: one compose line and one test constant
- Ruling: push both feature branches and open PRs (no merge, no NAS deploy) — cost if wrong: two PRs to close
- Ruling: implementer drops the dead line — cost if wrong: none
- Task 2: Ruling: three plan-mandated Important findings (append() KeyError on unknown relation; latest_version() swallows QUERY_REJECTED; history() labels local time as Z) — the plan's Interfaces block and the spec both demand QUERY_REJECTED and UTC receipts, so the sample code loses and the contract wins — cost if wrong: none, the fixes are strictly narrower behaviour
- Task 3: Ruling: expectations use `params` + top-level `row_date` + optional `absent_row_dates` (list of session dates that must have no holiday/market row), never `where` — Task 8's test must honour all three — cost if wrong: Task 8 test edits
- Task 3: Ruling: the Gold calendar view merges evidence_status with the market_session row outranking the holiday_date row on the same date (coalesce(m, h)), as the plan's SQL already does — cost if wrong: Eid T2 expectation
- Task 4: Ruling: the Important "resolve_manifest raises QUERY_REJECTED not NO_LOCAL_EVIDENCE for external relations" is a plan-note mismatch, not a defect to fix here — Task 5's `physical_path` makes external tables open and version through the same path, which is the designed correction; Task 5's dispatch carries the accurate pre-fix code and must add a test that an external relation pins in a manifest — cost if wrong: one Task 5 test
- Task 5: Ruling: a policy-clean statement that fails to bind (duckdb.BinderException/CatalogException/ParserException) must reach the client as QUERY_REJECTED — the mapping lands in Task 10 app error handlers (duckdb.Error -> QUERY_REJECTED with the message) — cost if wrong: a 500 instead of a 422 for bad column names
- Task 5: Ruling (supersedes the earlier binder ruling): bind/catalog errors are mapped inside QuerySession.run/describe to QUERY_REJECTED with the DuckDB text confined to details — the leak is generated at this layer, so it is contained here rather than in Task 10 — cost if wrong: none
- Task 6: Ruling: the brief's `coalesce(m.source_kind, h.source_kind, s.source_version) AS source_kind` is a plan typo — package sessions have source_kind 'pandas_rule'; fix lands in Task 6's fix round if the reviewer opens one, else in Task 8 when MarketCalendar exposes the column — cost if wrong: a mislabeled source_kind on package-generated sessions
- Task 6: Ruling: the effective filter applies to interval-shaped state relations (issuers, instruments, securities, listings, identifiers, exchanges, universe_membership) and is exempt for event/period-shaped relations whose own date column is the effective axis (prices_normalized, fundamental_facts, corporate_actions, calendar_exceptions, market_sessions, session_interruptions); the exemption is explicit in views.py via `_a(relation, ctx, alias, effective=False)` — cost if wrong: an event dated after effective_at shows in an effective_on query
- Task 6: Ruling: session_status gains `WHEN m.market_effect = 'open' THEN 'open'`; source_kind falls back to the literal 'pandas_rule' for package sessions; captured_by knowledge filter is `observed_at <= known_at` per spec — cost if wrong: none
- Task 7: Ruling: in compare._classify, when both contexts are effective_on (knowledge identical) and a field differs across two assertion_ids, the class is became_effective (or expired when the right side lost the row); corrected/superseded stay reserved for knowledge-time differences — cost if wrong: a mislabeled classification in the compare table
- Task 7: Ruling: the CUSIP fixture has no listing row; the compare test seeds lst_042 itself — accepted, the fixture gap is deferred to the final review (a silver.listings row for iss_042 would let ReferenceSecurity resolve through gold.security_master) — cost if wrong: Task 8 ReferenceSecurity projection may need the same seeding
- Task 7: Ruling: compare selects one row per declared key per side deterministically — order by keys, then effective_from DESC NULLS LAST, system_from DESC, assertion_id DESC, and keep the first — and a self-compare must yield zero differences (test) — cost if wrong: a compare over a multi-row key shows the latest interval rather than every interval
- Task 7: Ruling: _classify receives both contexts; when the two contexts differ only in effective time (both effective_on, or same knowledge cutoff), field differences across assertion_ids are became_effective / expired; identity fields still win identity_successor; knowledge-time differences keep corrected / newly_known / expired / superseded — cost if wrong: a mislabeled class
- Task 7: Ruling: an identifier that exists but is not effective under the context raises IDENTITY_UNRESOLVED with details.status = "not_effective" and details.effective_from; only include_historical=False may return an empty list — cost if wrong: none
- Task 7: Ruling: stable-id lookups (security_id, issuer_id, instrument_id) fall back to the current row of silver.securities / silver.issuers / silver.instruments when gold.security_master has no listing for them — cost if wrong: none
- Task 8: Ruling: ODP receipts merge identity.resolve's manifest into dependencies — a receipt must name every relation that chose the row — cost if wrong: none
- Task 8: Ruling: an ODP projection refuses (IDENTITY_AMBIGUOUS) when identity resolution leaves more than one distinct (listing_id, security_id), even under effective_on — never candidates[0] — cost if wrong: a reused ticker needs a listing_id parameter
- Task 8: Ruling (plan text overridden): an empty known_at result is NO_LOCAL_EVIDENCE only when the backing relation holds no row for the resolved identity (or calendar) under the context before user filters; a filter that matches nothing returns []. IDENTITY_UNRESOLVED becomes NO_LOCAL_EVIDENCE only when the same identifier resolves under current_corrected — cost if wrong: a 404 where a [] belongs or vice versa
- Task 8: Ruling (plan text overridden): MarketCalendar's as_of/knowledge_at map onto the context (effective_on / known_at) when the request context is the default, and are QUERY_REJECTED when they disagree with an explicit context; provider must be local_security_master or absent; include_breaks=false nulls break columns; session_label=trade_date labels rows by trade_date; timezone other than UTC is QUERY_REJECTED ("output timezone conversion is not available in this release") — cost if wrong: a caller expecting exchange-local instants must convert
- Task 9: Ruling: two plan-mandated Importants are fixed (the scan semaphore is released only when it was acquired; ILIKE filter values escape %/_ with ESCAPE '\'); rulings 2 and 3 get permanent tests — cost if wrong: none
- Task 10: Ruling: the query-string credential is honoured only on paths ending in /events (the SSE route Task 11 adds); Task 13's uvicorn command adds --no-access-log — cost if wrong: an SSE client that cannot set headers on another path
- Task 10: Ruling: STATUS_BY_CODE gains INTERNAL_ERROR (500) and a catch-all handler envelopes unhandled exceptions with the request id (Task 1's exact-set test grows by one) — cost if wrong: none
- Task 10: Ruling: _relations_in excludes external_delta relations; catalog_index(settings) is computed once per request and threaded through expand/check_modes/resolve_manifest — cost if wrong: none
- Task 10: Ruling (spec budget adjusted): per_principal_queries defaults to 8 because the stack has one credential (all callers hash alike); the spec's "2 per user" was an implementation target — cost if wrong: more concurrent scans than intended
- Task 10: Ruling: record() scrubs volatile fields (execution_id -> qry_contract, request_id -> req_contract, created_at -> 2026-09-17T00:00:00Z, sql_fingerprint -> contract) so test runs are reproducible and the tree stays clean — cost if wrong: none
- Task 10: Ruling: widgets.json and apps.json move into the package (security_master_api/) as package data and are read from Path(security_master_api.__file__).parent; Task 13's Dockerfile/contract test follow — cost if wrong: one COPY line
- Task 11: Ruling: append_event also appends an ops.jobs summary row (seq+1, state, updated_at, summary) so ops.jobs holds the summary the spec assigns it — cost if wrong: none
- Task 11: Ruling: events order by (seq, at, event_id) and the SSE stream de-duplicates by event_id, not by index — cost if wrong: none
- Task 11: Ruling: policy=refresh preflights the whole requested range (every stored day is re-pulled and superseded); force is the same range and additionally ignores calendar warnings; missing_only stays the gap scan — cost if wrong: an over-counted request estimate
- Task 11: Ruling: the SSE generator is async (anyio.sleep, DB reads in run_in_threadpool) so it holds no threadpool token while idle — cost if wrong: none
- Task 12: Ruling: close rows and new rows are appended in ONE Delta commit; content_hash is computed over the stored payload with a `truncated` marker in the capture's request_log.error when the body exceeded 2 MB; each listing's missing ranges collapse to one min..max call so the worker never exceeds the signed expected_requests; exchange_calendar requests carry provider=eodhd; POST /acquisitions refuses a security_lookup token with QUERY_REJECTED ("security_lookup is preflight-only in this release"); an empty provider answer ends at `partial` (Bronze retained) rather than validation_failed — cost if wrong: a wider price range fetched than strictly missing; lookup jobs deferred
- Task 12: Ruling: a 200 body that does not parse as JSON (or whose results is not a list) is validation_failed with problem "malformed payload"; only a well-formed empty list ends partial — the brief's test_malformed_payload_fails_cleanly expectation stands — cost if wrong: none
- Task 13: Ruling: the env example documents SECURITY_MASTER_WORKER_POLL_S and sets SECURITY_MASTER_PER_PRINCIPAL_QUERIES=8 to match the code default — cost if wrong: none
- Task 14: Ruling: the extension's entry point is named `reference` with an empty router prefix (RouterLoader mounts under the entry-point name), so the commands are REST-only (`obb.reference` stays OpenBB's reference dict); the registry's consumer string `obb.reference.market_calendar` is aspirational and left as-is — cost if wrong: a Python caller must use the REST route
- Task 14: Ruling: fix round — credentials read with model_dump(mode="json") so the EODHD key is a str; security_master_resolve's as_of/knowledge_at typed like market_calendar; a naive knowledge_at is treated as UTC; an unexpected 2xx shape is OpenBBError — cost if wrong: none
- Part A: Ruling: ONE fix wave covers Critical 1, Important 2-4, Minor 5 (catalog once per request from the route layer) and Minor 10 (spec entry-point/fixture path lines); the rest stays deferred — cost if wrong: a second wave the process forbids

## Final review and fix wave

- Part A final review (deb80d4): 1 Critical (extension session_label vocabulary), 3 Important (interruptions bypass knowledge filter; negative cursor/limit; --no-access-log not in Dockerfile CMD nor pinned), 10 Minor; deferred triage: fix-before-merge = spec §2 tests/fixtures path, interruptions/branches ODP tests, session_label vocabulary; Docker build must run once before any tag.
- Part A fix wave: re-review clean (commits deb80d4..adac755). Deferred residuals for the PR body: registry values tuple not fully pinned in the extension Literal; include_interruptions current_corrected path untested; ODP path builds its catalog inside resolve_manifest; Docker image unbuilt locally.

## Deferred findings

Copied verbatim from the ledger's `minor (deferred)` lines:

- Task 1: minor (deferred): tests/test_config.py:5 is 107 chars (line specified verbatim in the plan; E501 not enabled)
- Task 2: minor (deferred): dataset(version=None) default beyond the contract; _instant imported from temporal_fixture (plan-mandated); paths.split derives before validating
- Task 3: minor (deferred): golden_fixtures() does not validate row_date / exactly-one-of expect|error; seed rescans per fixture; ticker_change 2022-06-10 price available_at precedes the session (plan-mandated); __main__ raw ValueError; spec §2 still names tests/fixtures/ as the location
- Task 4: minor (deferred): E501 never selected in ruff (repo-wide), four brief-inherited lines >100; parse_context accepts bool in versions; _when message imprecise
- Task 5: minor (deferred): describe() lacks the entered-session assert and takes no params/timeout; CTE names share one scope set; physical_path does not validate the symbol segment; view_sql interpolated unvalidated (server-authored); 7 brief-inherited lines >100 chars; BASE_TABLE catalog_name ignored
- Task 5: minor (deferred): dead `timed_out` event in run(); policy falls open for a FUNCTION/WINDOW node with empty function_name (unreachable on duckdb 1.5.5)
- Task 6: minor (deferred): holiday/market CTE tie-break lacks assertion_id DESC
- Task 6: minor (deferred): depends_on not asserted by test_gold_sql_names_only_declared_dependencies; corporate_actions/universe_membership views untested by fixtures; exchanges join fans out if two exchanges share a calendar_id; __enter__ leaks the connection when a view fails to bind
- Task 6: minor (deferred): session_status classifies a pending market_effect as open when a package session row also exists for the date (untested combination)
- Task 7: minor (deferred): compare selection keys are quoted not validated (validate against relation columns in Task 10); manifest resolved four times; lineage request/run legs unexercised by fixtures; iso_utc applied per emitter not in session.run
- Task 7: minor (deferred): stable-id lookups do not raise not_effective (asymmetry with identifier lookups); resolve issues one enrichment query per candidate
- Task 8: minor (deferred): $placeholder detection by substring; datetime coerce branch missing; LIKE wildcards pass through EquitySearch; SELECT * returns 40 columns vs 34 declared fields; columns vocabulary differs for the resolver model; registry projection_version unused; _calendar_id's second manifest not pinned; no interruptions/branches ODP tests; unguarded json.loads; fixture loop skip count unasserted
- Task 8: minor (deferred): include_breaks defaults false on a bare request (nulls break columns); ReferenceSecurity returns one row per effective interval; session_label rejects unknown labels (extra, harmless)
- Task 9: minor (deferred): page()/cancel() take no principal (brief interface; Task 10 notes it); in/between element types validated only at execution; two brief-inherited lines >100 chars; _acquire's blocking semaphore wait inside FastAPI handlers is noted for Task 10 (run handlers in the threadpool: sync def routes already do)
- Task 10: minor (deferred): versions_compare row_diff materializes both tables; GET .../versions on a view should say views have no versions; executions.page shadows the module-level fingerprint name
- Task 11: minor (deferred): create_job read-then-write idempotence race; fingerprint over the raw request dict; _missing_ranges ignores gold.market_calendar; append_event accepts an unknown job_id; expected_requests == 0 still mints a token; SSE frames carry no id:; unknown lookup_policy treated as cache_only (fix in this round as a one-liner: QUERY_REJECTED)
- Task 11: minor (deferred): two Delta commits per event without a cross-table transaction; ops.jobs grows one row per event and queued() scans it
- Task 12: minor (deferred): request_log.error only for status 0; classify maps 5xx to transport; observed_at taken before the fetch; _supersede loads the whole relation; two job-table scans per poll; sweep race yields event noise; exchange_calendar request without codes raises KeyError; no 5xx/404 tests; four lines >100 chars
- Task 12: minor (deferred): truncation note wins over the status snippet in request_log.error when both apply; two extra pytest warnings are openbb_core import-time deprecations
- Task 13: minor (deferred): root README version table still ends at v11.1.1 (pre-existing); the Docker image build is unverified locally (Docker Desktop unavailable) and rests on the release workflow
- Task 14: minor (deferred): `MarketCalendarQueryParams(**locals())` relies on statement order; an empty-bodied 401 reads as "without JSON"; README overstates registry-drift landing in extra; `provider` accepted and inert

## Deployment procedure

Copied verbatim from Task 23 Step 4 of the plan. Documentation only — not
executed as part of this record; run only on the user's explicit go, after
4 PM ET, from a merged `release/v11.2.x` checkout.

```bash
# on the Mac, from the merged release/v11.2.x checkout
docker build --platform linux/amd64 -f security-master-api/Dockerfile -t openbb-security-master:11.4.0 .
docker build --platform linux/amd64 --build-arg OPENBB_VERSION=4.7.2 -t openbb-local:11.4.0 .
docker save openbb-security-master:11.4.0 | gzip -1 | ssh nas 'gunzip | docker load'
docker save openbb-local:11.4.0 | gzip -1 | ssh nas 'gunzip | docker load'
# on the NAS, in the live compose project directory
cp docker-compose.yml docker-compose.yml.pre-security-master
# copy the two new service blocks and the openbb-api SECURITY_MASTER_URL line into docker-compose.yml;
# write security-master.env from security-master.env.example with a real secret;
# add the Serve routes to ts-config/serve.json and serve-funnel.json
docker compose up -d --no-deps security-master-api security-master-worker
docker compose run --rm security-master-api python -m security_master_api.store    # seed once
docker compose up -d --no-deps openbb-api tailscale                                 # the router extension and the Serve routes
curl -u "$USER:$PASS" https://openbb-security-master.<tailnet>.ts.net/security-master/v1/health
```

Rollback: `cp docker-compose.yml.pre-security-master docker-compose.yml && docker compose up -d --no-deps openbb-api tailscale && docker compose rm -sf security-master-api security-master-worker`.
