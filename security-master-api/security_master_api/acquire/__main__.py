# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""`python -m security_master_api.acquire` - the compose worker entrypoint."""

from __future__ import annotations

from security_master_api.acquire.worker import main

if __name__ == "__main__":
    main()
