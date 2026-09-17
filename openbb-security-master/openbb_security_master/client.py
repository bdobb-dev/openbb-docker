# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""HTTP client for security-master-api. The extension never opens Delta or MinIO itself."""

from __future__ import annotations

import os

import requests
from openbb_core.app.model.abstract.error import OpenBBError

V1 = "/security-master/v1"


class SecurityMasterClient:
    def __init__(self, base_url: str, username: str, password: str, timeout_s: int = 30):
        self.base_url = base_url.rstrip("/")
        self._auth = (username, password)
        self.timeout_s = timeout_s

    @classmethod
    def from_env(cls) -> "SecurityMasterClient":
        return cls(os.environ.get("SECURITY_MASTER_URL", "http://security-master-api:6905"),
                   os.environ.get("OPENBB_API_USERNAME", ""),
                   os.environ.get("OPENBB_API_PASSWORD", ""))

    @staticmethod
    def context_for(as_of: str | None, knowledge_at: str | None) -> dict:
        if knowledge_at:
            ctx = {"mode": "known_at", "known_at": knowledge_at}
            if as_of:
                ctx["effective_at"] = f"{as_of}T00:00:00Z"
            return ctx
        if as_of:
            return {"mode": "effective_on", "effective_at": f"{as_of}T00:00:00Z"}
        return {"mode": "current_corrected"}

    def _post(self, path: str, body: dict) -> dict:
        try:
            r = requests.post(f"{self.base_url}{V1}{path}", json=body, auth=self._auth,
                              timeout=self.timeout_s)
        except requests.RequestException as exc:
            raise OpenBBError(f"security-master-api unreachable: {exc}") from exc
        try:
            payload = r.json()
        except ValueError as exc:
            raise OpenBBError(f"security-master-api answered {r.status_code} without JSON") from exc
        if r.status_code >= 400:
            err = payload.get("error", {}) if isinstance(payload, dict) else {}
            raise OpenBBError(f"{err.get('code', 'ERROR')}: {err.get('message', r.text[:200])}")
        return payload

    def odp_query(self, model_id: str, parameters: dict, context: dict) -> dict:
        return self._post(f"/odp/models/{model_id}/query",
                          {"parameters": parameters, "context": context})

    def resolve(self, identifier: str, context: dict, identifier_type: str | None = None) -> dict:
        return self._post("/resolve", {"identifier": identifier,
                                       "identifier_type": identifier_type,
                                       "context": context})
