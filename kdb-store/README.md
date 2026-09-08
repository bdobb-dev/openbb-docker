<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# kdb-store

The shared kdb+ plumbing: the `q` child process, its IPC connection, and the
store built on top. Used by both `openbb-kdb` (the read-through cache provider)
and `live-grid` (the tick recorder).

**Why this is its own package.** PyKX aborts the process when touched from more
than one thread — not merely under concurrency, but on strictly sequential
calls from different threads (`free(): invalid size`). `KdbSession` solves that
by marshalling every PyKX call onto one owner thread. Two consumers need that
guarantee, and there must be exactly one implementation of it.

## Test

    pip install -e .[dev] && pytest    # no kdb licence needed
