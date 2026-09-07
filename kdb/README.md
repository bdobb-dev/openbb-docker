<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Your q goes here

This repository ships **no** kdb+ software. KX's licence does not permit
redistributing their binary, so you supply it — either here, or as your own
container.

## Option A — drop q in this directory

Download kdb-x or kdb+ Personal Edition from KX and unpack it so the layout is:

```
kdb/
  bin/q          <- the executable
  l64/  or m64/  <- the architecture directory that came with it
```

Nothing else is needed. `openbb-api` finds it at `/opt/kx`, starts it bound to
`127.0.0.1:5000`, and every service in the stack shares it.

## Option B — run your own kdb container

Leave this directory empty and set `KDB_HOST` in `credentials.env` to reach it.
See the repository README.

## Your licence

Mount `kc.lic` into `kdb-license/`, not here. Nothing in either directory is
committed — both are git-ignored.
