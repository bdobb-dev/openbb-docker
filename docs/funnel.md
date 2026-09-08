<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Funnel: publishing the API to the public internet — carefully

Companion doc for *Adventures in OpenBB, Ep. 2*. This is optional: the stack
is fully functional tailnet-only. Funnel is for reaching your backend from
devices that cannot run Tailscale (a locked-down work machine, any browser).

**The order of operations is the whole point: the lock goes on before the
door opens.**

## 1. The lock (already mandatory)

From v2.0.0 the compose file refuses to start the API without
`api-auth.env` — HTTP Basic auth with your username/password. Verify it
works tailnet-only *before* touching Funnel:

Test it on a **data** route. OpenBB wires Basic auth as a FastAPI dependency
of the `/api/v1` router, so that is the only thing it guards: `/widgets.json`,
`/docs` and `/openapi.json` are metadata and answer 200 to anyone who can
reach the port. That is by design upstream, not a misconfiguration here — but
it does mean a check against `widgets.json` proves nothing about the lock.

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://openbb.<your-tailnet>.ts.net/api/v1/equity/price/quote
# -> 401
curl -s -o /dev/null -w '%{http_code}\n' -u openbb:<password> https://openbb.<your-tailnet>.ts.net/api/v1/equity/price/quote
# -> 422   (credentials accepted; the request then fails validation because
#           `symbol` is required — which is exactly what you want to see, and
#           costs no provider quota)

curl -s -o /dev/null -w '%{http_code}\n' https://openbb.<your-tailnet>.ts.net/widgets.json
# -> 401   metadata is locked too — the image enforces Basic auth as middleware,
#          so funneling port 443 publishes nothing without the password
curl -s -o /dev/null -w '%{http_code}\n' -u openbb:<password> https://openbb.<your-tailnet>.ts.net/widgets.json
# -> 200
```

## 2. Permit Funnel in the tailnet policy

Funnel is off until the node is allowed to use it. In the Tailscale admin
console → Access controls, grant the `funnel` node attribute to the tag (or
target) your `openbb` node holds:

```jsonc
"nodeAttrs": [
  { "target": ["tag:server"], "attr": ["funnel"] }
]
```

## 3. The door

Swap the Serve config for the Funnel-enabled one and recreate the sidecar:

```bash
cp ts-config/serve-funnel.json ts-config/serve.json
docker compose up -d --force-recreate tailscale
```

Only port 443 is funneled. Anything else the node ever serves stays
tailnet-only.

(An earlier revision also funneled `:10000`, the key-maint widget backend.
That was deliberately removed; key status is tailnet-only, on `:10000` and
as the Tailscale Service `svc:openbb-keys`.)

## The serve config is applied wholesale

`serve.json` is the only durable serve state this node has. Two things
about it are load-bearing, and both were learned the hard way.

- **Every apply replaces the node's entire serve state.** containerboot
  re-applies the file at startup and on every edit, with no merge. Anything
  configured with the `tailscale serve` / `tailscale advertise` CLI that is
  not in the file is silently erased at that moment. The Tailscale Services
  in the `Services` section were first set up by CLI on the host; the next
  apply wiped them and their hostnames went dark until they were written
  into this file. That is why `Services` lives here, and why the rule is:
  nothing by CLI, everything in `serve.json`.
- **Service hostnames are literal.** `${TS_CERT_DOMAIN}` expands to the
  node's own name, so it cannot express a Service's hostname. Replace
  `openbb-api.<your-tailnet>.ts.net` (and the others) with your real
  tailnet name before deploying. Services also need admin-side setup — a
  tagged node, the service defined and approved in the admin console; until
  then the `${TS_CERT_DOMAIN}` handlers keep working on their own.

Edits live-apply with no restart, but a malformed file kills the sidecar.
Write to `serve.json.new`, run `jq empty` on it, then `mv` it over
`serve.json`.

## 4. Verify from OFF the tailnet

From a device that is not on your tailnet (a phone on cellular works):

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://openbb.<your-tailnet>.ts.net/api/v1/equity/price/quote   # 401
curl -s -o /dev/null -w '%{http_code}\n' -u openbb:<password> https://openbb.<your-tailnet>.ts.net/api/v1/equity/price/quote  # 422
```

**Know what you have just published.** Funnel puts port 443 on the public
internet, and `/widgets.json`, `/apps.json` and `/openapi.json` answer without
credentials — so the widget catalogue, every endpoint path and every parameter
schema is now readable by anyone, and will be crawled. No market data leaks:
the `/api/v1` routes above are the ones that carry it, and they are locked.

If that metadata is more than you want to publish, do not funnel this port —
keep it tailnet-only and reach it over Tailscale, or put a reverse proxy in
front that demands the same credentials on every path. OpenBB Workspace sends
its `Authorization` header on every request to the backend, so a proxy that
locks the metadata routes does not break the pro.openbb.co connection.

## Connecting OpenBB Workspace (pro.openbb.co)

Connections → Add backend:

| Field | Value |
|---|---|
| Endpoint URL | `https://openbb.<your-tailnet>.ts.net/` |
| Validate widgets | **No** (Yes live-fires every widget and fails on any keyless provider) |
| Auth Key | `Authorization` |
| Auth Value | `Basic <base64 of username:password>` |
| Auth Location | Header |

Generate the value with: `printf 'openbb:<password>' | base64`

## Gotchas (each one earned)

- **Connection test fails with a generic 500** → "Validate widgets" is set to
  Yes. Set it to No; the validation fires all widgets and any keyless
  provider sinks it.
- **Everything 401s** → the Authorization header was removed or the password
  rotated; re-derive the base64 of `username:password` (the *pair*, colon
  included).
- **Connection flips Inactive** → Workspace polled while the API container
  was restarting; refresh the connection row once it's back.
- **Blocked only on machines that run Tailscale** ("local address space" /
  local network permission): MagicDNS resolves the name to its private
  100.x address, which Chromium gates behind a per-site permission. One-time
  fix: padlock on pro.openbb.co → Permissions → Local network access →
  Allow. Devices without Tailscale are unaffected. Misleading symptom:
  pasting the widgets.json URL in the address bar works while the app's
  fetches fail (direct navigations are exempt).
- **An env value mysteriously equals your comment** → compose's dotenv parser
  takes an inline `# comment` after an empty value as the value. Comments on
  their own lines, always.
