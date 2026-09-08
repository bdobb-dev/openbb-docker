# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""HTTP Basic auth matching the main API's api-auth.env. Fail closed: no
file / no configured creds = every request denied, so a missing mount can
never expose even the status view."""
from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.credfile import load

_security = HTTPBasic(auto_error=False)


def make_guard(auth_file: str):
    def guard(
        credentials: Annotated[HTTPBasicCredentials | None, Depends(_security)],
    ) -> None:
        conf = load(auth_file) or {}
        user = conf.get("OPENBB_API_USERNAME") or ""
        pw = conf.get("OPENBB_API_PASSWORD") or ""
        supplied_user = credentials.username if credentials else ""
        supplied_pw = credentials.password if credentials else ""
        ok_user = secrets.compare_digest(supplied_user.encode(), user.encode())
        ok_pw = secrets.compare_digest(supplied_pw.encode(), pw.encode())
        if not (user and pw and credentials and ok_user and ok_pw):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Basic"},
            )

    return guard
