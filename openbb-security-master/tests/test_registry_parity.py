# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def test_registry_copy_matches_the_service():
    ours = json.loads((HERE / "openbb_security_master" / "odp_registry.json").read_text())
    theirs = json.loads(
        (HERE.parent / "security-master-api" / "security_master_api" / "odp_registry.json").read_text()
    )
    assert ours == theirs
