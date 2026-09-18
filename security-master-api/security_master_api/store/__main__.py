# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""`python -m security_master_api.store [--root PATH]` seeds the fixtures and prints versions."""

from __future__ import annotations

import argparse
import json
import os

from security_master_api.config import settings_from_env
from security_master_api.store.seed import seed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", help="local directory instead of the DELTA_S3_* store")
    args = parser.parse_args()
    env = dict(os.environ)
    if args.root:
        env["SECURITY_MASTER_ROOT"] = args.root
    print(json.dumps(seed(settings_from_env(env)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
