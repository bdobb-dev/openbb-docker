"""Shared helpers for the widget smoke-test scripts.

Reads the same two env files docker-compose feeds into openbb-api
(api-auth.env for Basic auth, credentials.env for provider keys) so this
tool never needs its own copy of a secret. Talks to the API the same way
scripts/smoke.sh documents: point OPENBB_URL at the stack (Tailscale
Funnel/Serve hostname, or a docker-exec'd loopback) and it hits it over
plain HTTPS/HTTP.
"""
import os

import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _parse_env_file(path):
    values = {}
    if not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def load_auth():
    """(username, password) from api-auth.env, or (None, None) if auth is off."""
    env = _parse_env_file(os.path.join(REPO_ROOT, "api-auth.env"))
    if env.get("OPENBB_API_AUTH", "").lower() not in ("true", "1"):
        return None, None
    return env.get("OPENBB_API_USERNAME"), env.get("OPENBB_API_PASSWORD")


def load_credentials():
    """dict of provider env-var name -> value, from credentials.env."""
    return _parse_env_file(os.path.join(REPO_ROOT, "credentials.env"))


def base_url():
    url = os.environ.get("OPENBB_URL")
    if not url:
        raise SystemExit("set OPENBB_URL to the base URL to test (see scripts/smoke.sh)")
    return url.rstrip("/")


def make_session():
    session = requests.Session()
    username, password = load_auth()
    if username and password:
        session.auth = (username, password)
    return session


def fetch_widgets(session=None):
    session = session or make_session()
    resp = session.get(f"{base_url()}/widgets.json", timeout=30)
    resp.raise_for_status()
    return resp.json()


def widget_sources(widget):
    source = widget.get("source")
    if isinstance(source, list):
        return source
    return [source] if source else []


# Providers that need a key, mapped to the credentials.env var that supplies
# it. Derived from each installed openbb-<provider> package's `credentials=`
# declaration (checked 2026-08-27 against this image's pip freeze) -- update
# this if a new provider extension is added to the Dockerfile.
PROVIDERS_REQUIRING_KEY = {
    "FMP": "FMP_API_KEY",
    "FRED": "FRED_API_KEY",
    "EIA": "EIA_API_KEY",
    "BLS": "BLS_API_KEY",
    "CFTC": "CFTC_APP_TOKEN",
    "Congress Gov": "CONGRESS_GOV_API_KEY",
    "Intrinio": "INTRINIO_API_KEY",
    "Benzinga": "BENZINGA_API_KEY",
    "Tiingo": "TIINGO_TOKEN",
    "Trading Economics": "TRADINGECONOMICS_API_KEY",
}


def testability(widget, credentials):
    """(testable: bool, skip_reason: str | None) for one widget entry."""
    for source in widget_sources(widget):
        env_var = PROVIDERS_REQUIRING_KEY.get(source)
        if env_var and not credentials.get(env_var):
            return False, f"needs {env_var} (source: {source})"
    return True, None
