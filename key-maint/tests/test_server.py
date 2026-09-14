import base64
import os
import time

import pytest
from fastapi.testclient import TestClient

from app.probes import TestResult
from app.server import create_app

AUTH = {"Authorization": "Basic " + base64.b64encode(b"u1:p1").decode()}


@pytest.fixture
def files(tmp_path):
    cred = tmp_path / "credentials.env"
    cred.write_text("EODHD_API_KEY=demo\nFMP_API_KEY=realkey99\n")
    auth = tmp_path / "api-auth.env"
    auth.write_text("OPENBB_API_USERNAME=u1\nOPENBB_API_PASSWORD=p1\n")
    return str(cred), str(auth)


def client(files, role):
    cred, auth = files
    return TestClient(create_app(role=role, cred_file=cred, auth_file=auth))


def keys(c, headers=None, params=None):
    h = dict(AUTH)
    h.update(headers or {})
    return c.get("/keys", headers=h, params=params or {})


class TestTiers:
    def test_admin_role_is_tier_3_with_values(self, files):
        body = keys(client(files, "admin")).json()
        assert body["tier"] == 3
        fmp = next(r for r in body["rows"] if r["env_var"] == "FMP_API_KEY")
        assert fmp["value"] == "realkey99"

    def test_network_without_xff_is_tier_1(self, files):
        body = keys(client(files, "network")).json()
        assert body["tier"] == 1
        assert all("value" not in r for r in body["rows"])

    def test_network_cgnat_xff_is_tier_2(self, files):
        body = keys(client(files, "network"), headers={"X-Forwarded-For": "100.100.100.100"}).json()
        assert body["tier"] == 2
        assert all("value" not in r for r in body["rows"])

    def test_network_public_xff_is_tier_1(self, files):
        body = keys(client(files, "network"), headers={"X-Forwarded-For": "96.225.78.180"}).json()
        assert body["tier"] == 1

    def test_garbage_xff_is_tier_1(self, files):
        body = keys(client(files, "network"), headers={"X-Forwarded-For": "not-an-ip"}).json()
        assert body["tier"] == 1


class TestRunTests:
    def test_tier_1_ignores_run_tests(self, files, monkeypatch):
        async def fail(*a, **k):
            raise AssertionError("probes must not run at tier 1")

        monkeypatch.setattr("app.server.run_probes", fail)
        r = keys(client(files, "network"), params={"run_tests": "true"})
        assert r.status_code == 200
        assert all("test" not in row for row in r.json()["rows"])

    def test_tier_2_runs_probes(self, files, monkeypatch):
        from app.probes import TestResult

        async def fake(values, client=None):
            return {"FMP_API_KEY": TestResult("ok", "HTTP 200")}

        monkeypatch.setattr("app.server.run_probes", fake)
        r = keys(
            client(files, "network"),
            headers={"X-Forwarded-For": "100.100.100.100"},
            params={"run_tests": "true"},
        )
        fmp = next(x for x in r.json()["rows"] if x["env_var"] == "FMP_API_KEY")
        assert fmp["test"]["result"] == "ok"


class TestRestartRequired:
    def test_false_when_file_unchanged_since_start(self, files):
        r = keys(client(files, "admin"))
        assert r.json()["restart_required"] is False

    def test_true_once_file_is_written_after_start(self, files):
        c = client(files, "admin")
        cred, _ = files
        # Comfortably after `c`'s startup timestamp regardless of the
        # filesystem's mtime resolution (some truncate to whole seconds).
        future = time.time() + 5
        os.utime(cred, (future, future))
        r = keys(c)
        assert r.json()["restart_required"] is True

    def test_false_when_cred_file_missing(self, files):
        _, auth = files
        c = TestClient(create_app(role="admin", cred_file="/nonexistent/x.env", auth_file=auth))
        r = keys(c)
        assert r.json()["restart_required"] is False


class TestContract:
    def test_auth_required(self, files):
        assert client(files, "network").get("/keys").status_code == 401

    def test_widgets_json_shape(self, files):
        w = client(files, "network").get("/widgets.json", headers=AUTH).json()
        entry = w["provider_api_keys"]
        assert entry["type"] == "table"
        assert entry["endpoint"] == "keys"
        names = [p["paramName"] for p in entry["params"]]
        assert names == ["run_tests"]

    def test_missing_cred_file_still_200(self, files, tmp_path):
        _, auth = files
        c = TestClient(create_app(role="admin", cred_file=str(tmp_path / "x.env"), auth_file=auth))
        r = keys(c)
        assert r.status_code == 200
        assert r.json()["rows"][0]["status"] == "unknown"

    def test_docs_and_openapi_disabled(self, files):
        c = client(files, "network")
        assert c.get("/docs").status_code == 404
        assert c.get("/openapi.json").status_code == 404
        assert c.get("/redoc").status_code == 404

    def test_unknown_role_rejected(self, files):
        cred, auth = files
        with pytest.raises(ValueError):
            create_app(role="bogus", cred_file=cred, auth_file=auth)

    def test_malformed_line_yields_warning_row(self, files, tmp_path):
        _, auth = files
        cred = tmp_path / "credentials.env"
        cred.write_text("EODHD_API_KEY=demo\nNOEQUALSSIGN\n")
        c = TestClient(create_app(role="admin", cred_file=str(cred), auth_file=auth))
        rows = keys(c).json()["rows"]
        assert rows[-1]["provider"] == "⚠ malformed line"


class TestSingleProviderTest:
    def test_tier_1_is_refused(self, files):
        c = client(files, "network")  # no XFF -> tier 1
        r = c.get("/keys/EODHD_API_KEY/test", headers=AUTH)
        assert r.status_code == 403

    def test_unknown_env_var_is_404(self, files):
        c = client(files, "admin")
        r = c.get("/keys/NOT_A_PROVIDER/test", headers=AUTH)
        assert r.status_code == 404

    def test_admin_gets_a_result_shape(self, files, monkeypatch):
        # Stub the probe so the test never touches a vendor's API.
        async def fake_probe(env_var, values):
            return TestResult("ok", "HTTP 200")
        monkeypatch.setattr("app.server.probe_one_provider", fake_probe)
        c = client(files, "admin")
        r = c.get("/keys/EODHD_API_KEY/test", headers=AUTH)
        assert r.status_code == 200
        assert r.json() == {"result": "ok", "detail": "HTTP 200"}

    def test_requires_auth(self, files):
        c = client(files, "admin")
        assert c.get("/keys/EODHD_API_KEY/test").status_code == 401


class TestWriteKey:
    def test_network_role_is_refused_even_from_the_tailnet(self, files):
        c = client(files, "network")
        r = c.put(
            "/keys/FMP_API_KEY",
            headers={**AUTH, "X-Forwarded-For": "100.100.100.100"},  # tier 2
            json={"value": "nope"},
        )
        assert r.status_code == 403

    def test_admin_writes_and_reports_restart_required(self, files):
        cred, _ = files
        c = client(files, "admin")
        r = c.put("/keys/FMP_API_KEY", headers=AUTH, json={"value": "written123"})
        assert r.status_code == 200
        assert r.json() == {"status": "set", "restart_required": True}
        assert "FMP_API_KEY=written123" in open(cred).read()

    def test_response_never_echoes_the_value(self, files):
        c = client(files, "admin")
        r = c.put("/keys/FMP_API_KEY", headers=AUTH, json={"value": "supersecret999"})
        assert r.status_code == 200
        assert "supersecret999" not in r.text

    def test_a_rejected_write_never_echoes_the_value(self, files):
        # FastAPI's 422 body echoes the offending input, so the value must not
        # be validated by a Pydantic constraint. Send a wrong-typed value and
        # assert the secret-shaped sibling never appears in the response.
        c = client(files, "admin")
        r = c.put("/keys/NOT_A_PROVIDER", headers=AUTH, json={"value": "supersecret999"})
        assert r.status_code == 404
        assert "supersecret999" not in r.text

    def test_unknown_env_var_is_rejected(self, files):
        cred, _ = files
        c = client(files, "admin")
        r = c.put("/keys/EVIL_VAR", headers=AUTH, json={"value": "x"})
        assert r.status_code == 404
        assert "EVIL_VAR" not in open(cred).read()

    def test_empty_value_reports_empty(self, files):
        c = client(files, "admin")
        r = c.put("/keys/FMP_API_KEY", headers=AUTH, json={"value": ""})
        assert r.json()["status"] == "empty"

    def test_requires_auth(self, files):
        c = client(files, "admin")
        assert c.put("/keys/FMP_API_KEY", json={"value": "x"}).status_code == 401

    # --- Malformed-body branch (the reason this endpoint hand-parses the
    # request body instead of using a Pydantic model: FastAPI's 422 response
    # echoes the offending input, which would print the secret). Every test
    # above sends a well-formed {"value": <str>}, so without these the
    # 400 branch below is unexercised and a future Pydantic-model refactor
    # could reintroduce the leak with a green suite. Use the admin client so
    # the request reaches the body-parse branch rather than being refused
    # earlier by the tier gate.

    def test_wrong_typed_value_is_rejected(self, files):
        c = client(files, "admin")
        r = c.put(
            "/keys/FMP_API_KEY", headers=AUTH, json={"value": {"k": "supersecret999"}}
        )
        assert r.status_code == 400
        assert "supersecret999" not in r.text

    def test_missing_value_key_is_rejected(self, files):
        c = client(files, "admin")
        r = c.put("/keys/FMP_API_KEY", headers=AUTH, json={"other": "x"})
        assert r.status_code == 400

    def test_body_that_is_not_valid_json_is_rejected(self, files):
        c = client(files, "admin")
        r = c.put(
            "/keys/FMP_API_KEY",
            headers={**AUTH, "Content-Type": "application/json"},
            content=b"{",
        )
        assert r.status_code == 400

    def test_body_that_is_not_a_json_object_is_rejected(self, files):
        c = client(files, "admin")
        r = c.put("/keys/FMP_API_KEY", headers=AUTH, json=["supersecret999"])
        assert r.status_code == 400
        assert "supersecret999" not in r.text

    def test_lone_surrogate_reaches_set_value_and_is_rejected_safely(self, files):
        # A lone surrogate is accepted by json.loads and survives the
        # round-trip check (parse_text does not reject it), so it reaches
        # set_value's write -- which raises UnicodeEncodeError (a ValueError
        # subclass) because utf-8 cannot encode it. This is the case the
        # ValueError handler exists for; it is not unreachable defense in
        # depth.
        #
        # Sent as a raw body (not json=): httpx's json encoder refuses to
        # serialize a lone surrogate to utf-8 bytes, but \uXXXX-escaped text
        # is standard, valid JSON that json.loads decodes back to the same
        # surrogate character -- this is how a real client would send it.
        c = client(files, "admin")
        r = c.put(
            "/keys/FMP_API_KEY",
            headers={**AUTH, "Content-Type": "application/json"},
            content=b'{"value": "\\ud800"}',
        )
        assert r.status_code == 400
        assert "\\ud800" not in r.text

    # --- Round-trip safety (Task 4 hazard: parse_text does not round-trip
    # every string -- see app/credfile.py's set_value docstring). Decision:
    # reject with 400 rather than silently strip or accept, because a
    # silent rewrite would make the "set" response lie about what the
    # container loads on restart.

    def test_value_with_inline_comment_marker_is_rejected(self, files):
        cred, _ = files
        c = client(files, "admin")
        r = c.put("/keys/FMP_API_KEY", headers=AUTH, json={"value": "abc #not-a-comment"})
        assert r.status_code == 400
        assert "FMP_API_KEY=realkey99" in open(cred).read()

    def test_value_with_leading_or_trailing_whitespace_is_rejected(self, files):
        cred, _ = files
        c = client(files, "admin")
        r = c.put("/keys/FMP_API_KEY", headers=AUTH, json={"value": "  padded  "})
        assert r.status_code == 400
        assert "FMP_API_KEY=realkey99" in open(cred).read()

    def test_rejected_roundtrip_value_never_echoes(self, files):
        c = client(files, "admin")
        r = c.put("/keys/FMP_API_KEY", headers=AUTH, json={"value": "supersecret999 #x"})
        assert r.status_code == 400
        assert "supersecret999" not in r.text

    def test_successful_write_is_audit_logged_without_the_value(self, files, caplog):
        c = client(files, "admin")
        with caplog.at_level("INFO", logger="app.audit"):
            r = c.put("/keys/FMP_API_KEY", headers=AUTH, json={"value": "supersecret999"})
        assert r.status_code == 200
        assert any("FMP_API_KEY" in rec.message for rec in caplog.records)
        assert all("supersecret999" not in rec.message for rec in caplog.records)

    def test_rejected_write_is_not_audit_logged(self, files, caplog):
        c = client(files, "admin")
        with caplog.at_level("INFO", logger="app.audit"):
            r = c.put("/keys/NOT_A_PROVIDER", headers=AUTH, json={"value": "x"})
        assert r.status_code == 404
        assert caplog.records == []

    def test_value_starting_with_hash_round_trips_and_is_accepted(self, files):
        # Leading '#' with no preceding whitespace is part of the value per
        # parse_text's own rule, so it must NOT be rejected.
        cred, _ = files
        c = client(files, "admin")
        r = c.put("/keys/FMP_API_KEY", headers=AUTH, json={"value": "#notacomment"})
        assert r.status_code == 200
        assert "FMP_API_KEY=#notacomment" in open(cred).read()

    def test_line_break_that_reaches_set_value_is_rejected_safely(self, files, monkeypatch):
        # Defense in depth: if the round-trip pre-check were ever bypassed
        # (e.g. a future edge case not covered by parse_text), set_value's
        # own line-break guard must still fail safely -- caught and turned
        # into a 400, never a 500 with a traceback, and never echoing the
        # value. The sentinel is a printable string alongside the break (not
        # just "a\nb"): JSON-encoding a bare newline renders it as "\n", so
        # the raw substring could never appear in the response body even if
        # the endpoint did echo it -- that would make the no-echo assertion
        # vacuous.
        monkeypatch.setattr(
            "app.server.parse_text", lambda text: {"FMP_API_KEY": "supersecret999\nX"}
        )
        c = client(files, "admin")
        r = c.put("/keys/FMP_API_KEY", headers=AUTH, json={"value": "supersecret999\nX"})
        assert r.status_code == 400
        assert "supersecret999" not in r.text


class TestPanelWidget:
    def test_widgets_json_offers_both_table_and_keys(self, files):
        body = client(files, "admin").get("/widgets.json", headers=AUTH).json()
        assert body["provider_api_keys"]["type"] == "table"
        assert body["provider_api_keys_panel"]["type"] == "keys"
        # Same data source; the panel is a second view, not a fork.
        assert body["provider_api_keys_panel"]["endpoint"] == "keys"
        # Raw view would be a one-click path to every value the tier exposes.
        assert body["provider_api_keys_panel"].get("raw") is False


class TestMarkdownSummaryWidget:
    def test_widgets_json_declares_it_as_markdown_on_the_keys_endpoint(self, files):
        w = client(files, "network").get("/widgets.json", headers=AUTH).json()
        widget = w["provider_api_keys_summary"]
        assert widget["type"] == "markdown"
        # Third view of an endpoint that already exists -- no new route, and it
        # inherits /keys' auth and tier gate.
        assert widget["endpoint"] == "keys"
        assert widget["data"]["dataKey"] == "summary"

    def test_it_offers_no_raw_view(self, files):
        w = client(files, "network").get("/widgets.json", headers=AUTH).json()
        # tier 3 returns credential values in the rows this summary is built
        # from; a raw view would hand them to any dashboard.
        assert w["provider_api_keys_summary"]["raw"] is False

    def test_keys_response_carries_the_summary_the_widget_reads(self, files):
        body = client(files, "admin").get("/keys", headers=AUTH).json()
        assert isinstance(body["summary"], str) and body["summary"]
        assert "provider keys configured" in body["summary"]
