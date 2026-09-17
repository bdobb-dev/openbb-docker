# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""OpenBB router extension over security-master-api: the `obb.reference` namespace.

Deliberately empty of imports. Re-exporting `router` here would rebind the
`openbb_security_master.router` attribute from the submodule to the Router
object, and anything addressing the module by dotted path (monkeypatch, the
entry point's own `module:attr` form is fine) would get the object instead.
"""
